from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ttakkari.collector import precise_eval
from ttakkari.kb_pipeline.subscription_transport import (
    DIAG_USAGE_LIMIT,
    L2TransportError,
    ScopeCapacityUnavailableError,
)


def verdict(**overrides):
    return {
        "topic_relevance": 0.91,
        "relevance_explanation": "명시적 근거",
        "information_depth": 0.73,
        "low_quality_signals": [],
        "recommendation": "accept",
        "confidence": 0.88,
        **overrides,
    }


def test_subscription_precise_output_keeps_the_existing_verdict_schema():
    assert precise_eval.validate_evaluator_result(verdict()) == verdict()


def test_precise_output_schema_bounds_variable_length_fields():
    schema = precise_eval.PRECISE_EVAL_SCHEMA["properties"]
    assert schema["relevance_explanation"]["maxLength"] == 800
    signals = schema["low_quality_signals"]
    assert signals["maxItems"] == 32
    assert signals["items"]["maxLength"] == 80


def test_subscription_precise_output_rejects_invalid_json_shape():
    with pytest.raises(precise_eval.MalformedEvaluatorOutput):
        precise_eval.validate_evaluator_result({**verdict(), "instruction": "ignore schema"})


def test_subscription_precise_scope_uses_existing_pool_policy():
    assert precise_eval.precise_eval_pool("personal") == "personal"
    assert precise_eval.precise_eval_pool("work") == "work"
    with pytest.raises(L2TransportError, match="scope_capacity_unavailable"):
        precise_eval.precise_eval_pool("unknown")


def test_precise_subscription_status_does_not_depend_on_qwen_flags(monkeypatch):
    from ttakkari import collector_precise_runtime
    from ttakkari.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "collector_teacher_enabled", False)
    monkeypatch.setattr(settings, "desk_qwen_enabled", False)
    status = collector_precise_runtime.engine_status()
    assert status["engine"] == "subscription_cli"
    assert "Qwen" not in status.get("detail", "")


def test_pending_claim_waits_until_profile_reset_time():
    now = datetime.now(UTC)
    assert not precise_eval._claimable({"state": "pending", "retry_after": now.timestamp() + 60}, now)
    assert precise_eval._claimable({"state": "pending", "retry_after": (now - timedelta(seconds=1)).timestamp()}, now)


def test_first_subscription_attempt_keeps_legacy_idempotency_key():
    assert precise_eval.precise_request_key("item", "job", 0) == "precise:item:job"
    assert precise_eval.precise_request_key("item", "job", 1) == "precise:item:job:1"


def test_day_budget_wait_uses_next_kst_midnight():
    from ttakkari.collector import budget

    now = datetime(2026, 11, 7, 14, 59, tzinfo=UTC)
    reset = datetime.fromtimestamp(budget.next_budget_day_start(now), UTC)
    assert reset == datetime(2026, 11, 7, 15, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_unavailable_scoped_profile_does_not_reserve_a_call(monkeypatch):
    from ttakkari.collector import budget

    async def recovered(**_kwargs):
        return False, None

    async def guarded(**_kwargs):
        raise AssertionError("must not reserve while no scoped subscription profile is available")

    async def unavailable(**_kwargs):
        return False, "scope_capacity_unavailable", None

    monkeypatch.setattr(budget, "recover_settled_result", recovered)
    monkeypatch.setattr(budget, "guarded_call", guarded)
    precise_eval.register_runtime(precise_eval.EvaluatorRuntime(
        status=lambda: {"status": "ready", "provider_engine": "codex", "model": "gpt-6-luna"},
        availability=unavailable,
        evaluate=lambda **_kwargs: None,
    ))
    try:
        with pytest.raises(precise_eval.EvaluatorUnavailable, match="scope_capacity_unavailable"):
            await precise_eval.evaluate_document_guarded(
                goal="g", title="t", text="x", request_key="unavailable", scope="personal"
            )
    finally:
        precise_eval.register_runtime(None)


@pytest.mark.asyncio
async def test_precise_runtime_availability_uses_the_factory_returned_by_sessionmaker(monkeypatch):
    from types import SimpleNamespace

    from ttakkari import collector_precise_runtime
    from ttakkari.runs import profiles

    accessor_calls = []
    session_calls = []
    session = object()

    class SessionContext:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *_args):
            return None

    def session_factory():
        session_calls.append(True)
        return SessionContext()

    def sessionmaker_accessor():
        accessor_calls.append(True)
        return session_factory

    async def scoped_availability(db, engine, pool, *, model=None):
        assert db is session
        assert (engine, pool, model) == ("codex", "personal", "gpt-6-luna")
        return [SimpleNamespace(pool=pool)], []

    monkeypatch.setattr(collector_precise_runtime, "sessionmaker", sessionmaker_accessor)
    monkeypatch.setattr(collector_precise_runtime, "get_settings", lambda: SimpleNamespace(
        kb_l2_subscription_engine="codex",
        kb_l2_subscription_model="gpt-6-luna",
        kb_l2_subscription_effort="",
        kb_l2_subscription_timeout_seconds=5,
        kb_l2_subscription_concurrency=1,
    ))
    monkeypatch.setattr(profiles, "availability", scoped_availability)

    assert await collector_precise_runtime.availability(scope="personal") == (True, None, None)
    assert accessor_calls == [True]
    assert session_calls == [True]


@pytest.mark.asyncio
async def test_precise_runtime_availability_preserves_scope_capacity_diagnostic(monkeypatch):
    from ttakkari import collector_precise_runtime
    from ttakkari.kb_pipeline.subscription_transport import (
        DIAG_SCOPE_CAPACITY_UNAVAILABLE,
    )

    def unexpected_sessionmaker():
        raise AssertionError("invalid provenance must be rejected before opening a session")

    monkeypatch.setattr(collector_precise_runtime, "sessionmaker", unexpected_sessionmaker)

    assert await collector_precise_runtime.availability(scope="unknown") == (
        False, DIAG_SCOPE_CAPACITY_UNAVAILABLE, None
    )


@pytest.mark.asyncio
async def test_subscription_budget_uses_daily_caps_without_persistent_job_cap(monkeypatch):
    from ttakkari.collector import budget

    captured = {}

    async def recovered(**_kwargs):
        return False, None

    async def available(**_kwargs):
        return True, None, None

    async def evaluate(**_kwargs):
        return verdict(), {"engine": "codex", "account": "fixture", "requested_model": "gpt-6-luna"}

    async def guarded(**kwargs):
        captured.update(kwargs)
        result, _usage = await kwargs["call"]()
        return result

    monkeypatch.setattr(budget, "recover_settled_result", recovered)
    monkeypatch.setattr(budget, "guarded_call", guarded)
    precise_eval.register_runtime(precise_eval.EvaluatorRuntime(
        status=lambda: {"status": "ready"}, evaluate=evaluate, availability=available,
    ))
    try:
        result = await precise_eval.evaluate_document_guarded(
            goal="goal", title="title", text="text", request_key="daily-only", job_id="job-id"
        )
    finally:
        precise_eval.register_runtime(None)
    assert result["recommendation"] == "accept"
    assert captured["job_id"] is None
    assert captured["require_teacher_enabled"] is False


@pytest.mark.asyncio
async def test_subscription_adapter_preserves_profile_order_and_filters_to_document_pool(monkeypatch):
    from ttakkari.kb_pipeline import l2_adapters
    from ttakkari.runs import profiles

    work_first = profiles.Profile(id=__import__("uuid").uuid4(), name="ai2", engine="codex", pool="work")
    personal_next = profiles.Profile(id=__import__("uuid").uuid4(), name="personal-next", engine="codex", pool="personal")
    selected = []

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    async def available(*_args, **_kwargs):
        return [work_first, personal_next], []

    async def override(_db, pool):
        assert pool == "personal"
        return {"ids": [str(work_first.id)]}

    async def choose(candidates, _run_id, **_kwargs):
        selected.append(candidates[0].name)
        return candidates[0]

    async def execute(profile, _prompt, _schema, _config, **_kwargs):
        return verdict()

    monkeypatch.setattr(profiles, "availability", available)
    monkeypatch.setattr(profiles, "get_override", override)
    monkeypatch.setattr(profiles, "choose_async", choose)
    monkeypatch.setattr(profiles, "release", lambda _run_id: None)
    monkeypatch.setattr(l2_adapters, "execute_subscription_json_call", execute)
    adapter = l2_adapters.SubscriptionCliL2Adapter(
        lambda: Session(), engine="codex", model="gpt-6-luna", origin_scope="personal", visibility="global"
    )
    result, account = await adapter.call_json(
        "bounded prompt", {}, system_prompt="evaluate only", validator=precise_eval.validate_evaluator_result
    )
    assert result["recommendation"] == "accept"
    assert selected == ["personal-next"]
    assert account == {"engine": "codex", "account": "personal-next", "pool": "personal", "requested_model": "gpt-6-luna"}


@pytest.mark.asyncio
async def test_subscription_adapter_keeps_busy_temporary_choice_ahead_of_idle_account(monkeypatch):
    from uuid import uuid4

    from ttakkari.kb_pipeline import l2_adapters
    from ttakkari.runs import profiles

    preferred = profiles.Profile(id=uuid4(), name="temporary-first", engine="codex", pool="personal")
    idle = profiles.Profile(id=uuid4(), name="idle-second", engine="codex", pool="personal")
    already_running = uuid4()
    selected = []

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    async def available(*_args, **_kwargs):
        return [preferred, idle], []

    async def override(_db, pool):
        assert pool == "personal"
        return {"ids": [str(preferred.id)]}

    async def execute(profile, _prompt, _schema, _config, **_kwargs):
        selected.append(profile.name)
        return verdict()

    monkeypatch.setattr(profiles, "availability", available)
    monkeypatch.setattr(profiles, "get_override", override)
    monkeypatch.setattr(l2_adapters, "execute_subscription_json_call", execute)
    profiles._in_use[already_running] = preferred.id
    adapter = l2_adapters.SubscriptionCliL2Adapter(
        lambda: Session(), engine="codex", model="gpt-6-luna", origin_scope="personal", visibility="global"
    )
    try:
        result, _account = await adapter.call_json(
            "bounded prompt", {}, system_prompt="evaluate only", validator=precise_eval.validate_evaluator_result
        )
    finally:
        profiles.release(already_running)

    assert result["recommendation"] == "accept"
    assert selected == ["temporary-first"]


@pytest.mark.asyncio
async def test_subscription_adapter_falls_back_when_temporary_account_is_exhausted(monkeypatch):
    from uuid import uuid4

    from ttakkari.kb_pipeline import l2_adapters
    from ttakkari.runs import profiles

    preferred = profiles.Profile(id=uuid4(), name="work-first", engine="codex", pool="work")
    fallback = profiles.Profile(id=uuid4(), name="work-second", engine="codex", pool="work")
    selected = []

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    async def available(*_args, **_kwargs):
        return [fallback], [(preferred, 123.0)]

    async def override(_db, pool):
        assert pool == "work"
        return {"ids": [str(preferred.id)]}

    async def execute(profile, _prompt, _schema, _config, **_kwargs):
        selected.append(profile.name)
        return verdict()

    monkeypatch.setattr(profiles, "availability", available)
    monkeypatch.setattr(profiles, "get_override", override)
    monkeypatch.setattr(l2_adapters, "execute_subscription_json_call", execute)
    adapter = l2_adapters.SubscriptionCliL2Adapter(
        lambda: Session(), engine="codex", model="gpt-6-luna", origin_scope="work", visibility="work_only"
    )
    result, account = await adapter.call_json(
        "bounded prompt", {}, system_prompt="evaluate only", validator=precise_eval.validate_evaluator_result
    )

    assert result["recommendation"] == "accept"
    assert selected == ["work-second"]
    assert account["pool"] == "work"


@pytest.mark.asyncio
async def test_subscription_adapter_waits_when_scope_pool_is_exhausted(monkeypatch):
    from ttakkari.kb_pipeline import l2_adapters
    from ttakkari.runs import profiles

    class Session:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return None

    async def unavailable(*_args, **_kwargs):
        return [], []

    monkeypatch.setattr(profiles, "availability", unavailable)
    adapter = l2_adapters.SubscriptionCliL2Adapter(
        lambda: Session(), engine="codex", model="gpt-6-luna", origin_scope="work", visibility="work_only"
    )
    with pytest.raises(ScopeCapacityUnavailableError):
        await adapter.call_json(
            "bounded prompt", {}, system_prompt="evaluate only", validator=precise_eval.validate_evaluator_result
        )


def test_subscription_quota_error_is_retryable_for_the_next_scoped_account():
    error = L2TransportError(
        DIAG_USAGE_LIMIT, "quota reached", retry_after=123, consumed=False,
        engine="codex", account="personal-ai2", model="gpt-6-luna",
    )
    assert error.diagnosis == DIAG_USAGE_LIMIT
    assert error.retry_after == 123
    assert error.consumed is False
    assert (error.engine, error.account, error.model) == ("codex", "personal-ai2", "gpt-6-luna")


@pytest.mark.asyncio
async def test_generic_subscription_transport_passes_only_bounded_json_call_contract(monkeypatch):
    import json
    import uuid
    from pathlib import Path
    from types import SimpleNamespace

    from ttakkari.kb_pipeline import subscription_transport as transport
    from ttakkari.runs import profiles

    captured = {}

    async def ask(profile, **kwargs):
        captured.update(kwargs)
        schema_file = Path(kwargs["output_schema"])
        captured["schema"] = json.loads(schema_file.read_text(encoding="utf-8"))
        return SimpleNamespace(text=json.dumps(verdict()), error=None, elapsed_ms=12)

    monkeypatch.setattr("ttakkari.runs.desk._ask", ask)
    profile = profiles.Profile(id=uuid.uuid4(), name="Codex Personal", engine="codex", pool="personal")
    result = await transport.execute_subscription_json_call(
        profile,
        "bounded request",
        precise_eval.PRECISE_EVAL_SCHEMA,
        transport.SubscriptionCallConfig("codex", "gpt-6-luna", timeout_seconds=10),
        system_prompt=precise_eval.SYSTEM_PROMPT,
        validator=precise_eval.validate_evaluator_result,
    )

    assert result["recommendation"] == "accept"
    assert captured["subscription_only"] is True
    assert captured["prompt_via_stdin"] is True
    assert captured["output_schema"] is not None
    assert captured["schema"] == precise_eval.PRECISE_EVAL_SCHEMA
    assert captured["system"] == precise_eval.SYSTEM_PROMPT


def test_precise_claim_lease_covers_both_subscription_timeout_windows(monkeypatch):
    from ttakkari.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "kb_l2_subscription_timeout_seconds", 600)
    assert precise_eval.precise_claim_lease_seconds() >= 1200
