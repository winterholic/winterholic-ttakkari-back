from __future__ import annotations

import asyncio
import os
import stat
import time
from types import SimpleNamespace

import httpx
import pytest

from ttakkari.config import Settings
from ttakkari.experiments import qwen35_observer, qwen35_shadow
from ttakkari.experiments.qwen35_shadow import (
    CandidateSnapshot,
    ShadowQueue,
    build_response_schema,
    validate_shadow_output,
)
from ttakkari.runs import desk as desk_module


def snapshot() -> CandidateSnapshot:
    return CandidateSnapshot(
        lead_ids=("lead-1",),
        model_ids=("gpt-6-luna", "sonnet"),
        tool_ids=(),
        permission_ids=(),
    )


def test_schema_enumerates_only_server_candidates_and_forbids_extra_fields():
    schema = build_response_schema(snapshot())

    assert schema["additionalProperties"] is False
    assert schema["properties"]["lead_id"]["enum"] == [None, "lead-1"]
    assert schema["properties"]["model_id"]["enum"] == [None, "gpt-6-luna", "sonnet"]
    assert schema["properties"]["tool_ids"]["maxItems"] == 0
    assert schema["properties"]["permission_ids"]["maxItems"] == 0


def test_shadow_observer_is_disabled_with_zero_sampling_by_default():
    assert Settings.model_fields["qwen35_shadow_enabled"].default is False
    assert Settings.model_fields["qwen35_shadow_sample_rate"].default == 0.0


def test_empty_candidate_sets_are_valid_and_strictly_empty():
    schema = build_response_schema(CandidateSnapshot((), (), (), ()))

    for name in ("lead_id", "model_id"):
        assert schema["properties"][name]["enum"] == [None]
    for name in ("tool_ids", "permission_ids"):
        assert schema["properties"][name]["maxItems"] == 0


def test_snapshot_copies_mutable_candidate_inputs_before_model_request():
    leads = ["lead-1"]
    snapshot_value = CandidateSnapshot(leads, (), (), ())
    leads.append("attacker-added")

    assert snapshot_value.lead_ids == ("lead-1",)
    assert build_response_schema(snapshot_value)["properties"]["lead_id"]["enum"] == [None, "lead-1"]


def test_server_validation_rejects_candidate_not_in_snapshot():
    raw = '{"action":"delegate","reply":"넘깁니다","lead_id":"invented","model_id":null,"tool_ids":[],"permission_ids":[],"abstain_reason":null}'

    with pytest.raises(ValueError, match="candidate"):
        validate_shadow_output(raw, snapshot())


def test_server_validation_rejects_tool_or_permission_when_none_are_available():
    raw = '{"action":"delegate","reply":"넘깁니다","lead_id":"lead-1","model_id":"gpt-6-luna","tool_ids":["delete_file"],"permission_ids":["write"],"abstain_reason":null}'

    with pytest.raises(ValueError, match="candidate"):
        validate_shadow_output(raw, snapshot())


def test_server_validation_rejects_answer_with_a_lead_target():
    raw = '{"action":"answer","reply":"답변","lead_id":"lead-1","model_id":null,"tool_ids":[],"permission_ids":[],"abstain_reason":null}'

    with pytest.raises(ValueError, match="answer cannot select a lead"):
        validate_shadow_output(raw, snapshot())


def test_abstain_is_valid_but_cannot_claim_a_route_target():
    raw = '{"action":"abstain","reply":"판단을 보류합니다","lead_id":null,"model_id":null,"tool_ids":[],"permission_ids":[],"abstain_reason":"ambiguous"}'

    decision = validate_shadow_output(raw, snapshot())

    assert decision.action == "abstain"
    assert decision.abstain_reason == "ambiguous"


def test_shadow_queue_is_bounded_and_nonblocking_when_full():
    async def scenario():
        queue = ShadowQueue(maxsize=1)
        assert queue.submit("safe prompt", snapshot(), run_key="run-a") is True
        assert queue.submit("another prompt", snapshot(), run_key="run-b") is False
        assert queue.dropped == 1
        item = await queue.get()
        assert item.run_key == "run-a"
        assert queue.submit("after drain", snapshot(), run_key="run-c") is True

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [httpx.TimeoutException("timeout"), httpx.ConnectError("unavailable")])
def test_shadow_inference_propagates_timeout_and_unavailable_without_a_decision(monkeypatch, failure):
    class FailingClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        async def post(self, *_args, **_kwargs):
            raise failure

    monkeypatch.setattr(qwen35_shadow.httpx, "AsyncClient", FailingClient)

    async def scenario():
        with pytest.raises(type(failure)):
            await qwen35_shadow.infer_once("http://127.0.0.1:18786", "synthetic request", snapshot())

    asyncio.run(scenario())


def test_shadow_inference_rejects_response_larger_than_bound(monkeypatch):
    class OversizeClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def raise_for_status(self):
            return None

        async def post(self, *_args, **_kwargs):
            class Response:
                content = b"x" * 16385

                def raise_for_status(self):
                    return None

                def json(self):
                    return {"choices": [{"message": {"content": '{"action":"answer","reply":"OK","lead_id":null,"model_id":null,"tool_ids":[],"permission_ids":[],"abstain_reason":null}'}}]}

            return Response()

    monkeypatch.setattr(qwen35_shadow.httpx, "AsyncClient", OversizeClient)

    async def scenario():
        with pytest.raises(ValueError, match="response_too_large"):
            await qwen35_shadow.infer_once("http://127.0.0.1:18786", "synthetic request", snapshot())

    asyncio.run(scenario())


def test_observer_disabled_does_not_create_queue_or_capture(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("disabled observer must have zero side effects")

    monkeypatch.setattr(qwen35_observer, "_get_queue", forbidden)
    assert qwen35_observer.observe_if_enabled(
        enabled=False,
        environment="dev",
        request_text="safe synthetic request",
        context_text="safe synthetic context",
        policy_version="adr-0013:test",
        scope="personal",
        candidates=snapshot(),
        sample_key="synthetic-1",
    ) is False


def test_desk_hook_passes_exact_server_input_and_frozen_scoped_candidates(monkeypatch):
    captured = {}
    settings = SimpleNamespace(
        qwen35_shadow_enabled=True,
        qwen35_shadow_sample_rate=0.05,
        env="staging",
        desk_models={"codex": {"model": "gpt-6-luna"}, "claude": {"model": "haiku"}},
    )
    run = SimpleNamespace(prompt="synthetic exact request", id="run-internal")
    sess = SimpleNamespace(pool="work")
    leads = [SimpleNamespace(id="lead-a")]
    pool = [SimpleNamespace(engine="codex"), SimpleNamespace(engine="claude")]
    prompt = "[작업판]\nlead-a\n[사용자 메시지]\nsynthetic exact request"
    monkeypatch.setattr(desk_module, "observe_if_enabled", lambda **kwargs: captured.update(kwargs))

    desk_module.observe_shadow_route(settings, run, sess, leads, pool, prompt, "ADR 0013 system")

    assert captured["request_text"] == run.prompt
    assert captured["context_text"] == prompt
    assert captured["scope"] == "work"
    assert captured["policy_version"].startswith("adr-0013:v1;desk-instructions-sha256:")
    assert captured["candidates"].lead_ids == ("lead-a",)
    assert captured["candidates"].model_ids == ("gpt-6-luna", "haiku")
    assert captured["candidates"].tool_ids == captured["candidates"].permission_ids == ()


def test_desk_hook_is_noop_when_disabled_and_isolates_observer_exception(monkeypatch):
    settings = SimpleNamespace(qwen35_shadow_enabled=False)
    args = (settings, SimpleNamespace(prompt="x", id="r"), SimpleNamespace(pool="personal"), [], [], "x", "system")

    def forbidden(**_kwargs):
        raise AssertionError("disabled hook invoked observer")

    monkeypatch.setattr(desk_module, "observe_if_enabled", forbidden)
    desk_module.observe_shadow_route(*args)

    settings.qwen35_shadow_enabled = True
    monkeypatch.setattr(desk_module, "record_drop", lambda _reason: None)
    desk_module.observe_shadow_route(*args)


def test_observer_redacts_and_freezes_same_input_and_candidates():
    leads = ["lead-1"]
    candidates = CandidateSnapshot(leads, ["gpt-6-luna"], [], [])
    queue = qwen35_observer.ShadowObservationQueue(maxsize=1)
    assert qwen35_observer.observe_if_enabled(
        enabled=True,
        environment="staging",
        request_text="메일 user@example.com 으로 보내줘",
        context_text="[요청] 메일 user@example.com 으로 보내줘",
        policy_version="adr-0013:v1",
        scope="personal",
        candidates=candidates,
        sample_key="synthetic-3-6",
        queue=queue,
        sample_rate=0.05,
    ) is True
    leads.append("lead-added-later")
    item = queue.get_nowait()
    assert item.request_text == "메일 [이메일 제거] 으로 보내줘"
    assert item.context_text == "[요청] 메일 [이메일 제거] 으로 보내줘"
    assert item.candidates.lead_ids == ("lead-1",)
    assert item.scope == "personal"
    assert item.redaction_counts == ("request:email:1", "context:email:1")
    assert item.label_status == "policyverified/manualpending"
    assert item.regression_candidate is False


@pytest.mark.parametrize("environment", ["prod", "production"])
def test_observer_refuses_production_even_when_enabled(environment):
    queue = qwen35_observer.ShadowObservationQueue(maxsize=1)
    assert qwen35_observer.observe_if_enabled(
        enabled=True,
        environment=environment,
        request_text="synthetic request",
        context_text="synthetic context",
        policy_version="adr-0013:v1",
        scope="personal",
        candidates=snapshot(),
        sample_key="synthetic-prod",
        queue=queue,
        sample_rate=0.05,
    ) is False
    assert queue.qsize == 0


def test_observer_refuses_dev_environment_even_when_enabled():
    queue = qwen35_observer.ShadowObservationQueue(maxsize=1)
    assert qwen35_observer.observe_if_enabled(
        enabled=True,
        environment="dev",
        request_text="synthetic request",
        context_text="synthetic context",
        policy_version="adr-0013:v1",
        scope="personal",
        candidates=snapshot(),
        sample_key="synthetic-3-6",
        queue=queue,
        sample_rate=0.05,
    ) is False
    assert queue.qsize == 0


def test_observer_drops_oversize_payload_and_queue_full_without_blocking():
    queue = qwen35_observer.ShadowObservationQueue(maxsize=1)
    assert qwen35_observer.observe_if_enabled(
        enabled=True,
        environment="staging",
        request_text="x" * 7000,
        context_text="x" * 7000,
        policy_version="adr-0013:v1",
        scope="work",
        candidates=snapshot(),
        sample_key="oversize-49",
        queue=queue,
        sample_rate=0.05,
    ) is False
    assert queue.dropped["redaction_or_size_rejected"] == 1
    assert queue.qsize == 0

    assert qwen35_observer.observe_if_enabled(
        enabled=True,
        environment="staging",
        request_text="small synthetic request",
        context_text="small synthetic context",
        policy_version="adr-0013:v1",
        scope="work",
        candidates=snapshot(),
        sample_key="synthetic-queue-full-a-49",
        queue=queue,
        sample_rate=0.05,
    ) is True
    assert qwen35_observer.observe_if_enabled(
        enabled=True,
        environment="staging",
        request_text="another synthetic request",
        context_text="another synthetic context",
        policy_version="adr-0013:v1",
        scope="work",
        candidates=snapshot(),
        sample_key="synthetic-queue-full-b-3",
        queue=queue,
        sample_rate=0.05,
    ) is False
    assert queue.dropped["queue_full"] == 1
    assert queue.dropped["redaction_or_size_rejected"] == 1
    assert queue.qsize == 1


def test_observation_storage_is_scoped_private_and_retention_bounded(tmp_path):
    observation = qwen35_observer.ShadowObservation(
        request_text="synthetic request",
        context_text="synthetic context",
        policy_version="adr-0013:v1",
        scope="work",
        candidates=snapshot(),
        redaction_counts=(),
    )
    path = qwen35_observer.persist_observation(observation, tmp_path / "router-shadow")

    assert path.parent.name == "work"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert "synthetic request" in path.read_text()

    old = tmp_path / "router-shadow" / "work" / "observation-old.json"
    old.write_text("{}"); os.chmod(old, 0o600)
    os.utime(old, (time.time() - qwen35_observer.RETENTION_SECONDS - 1,) * 2)
    assert qwen35_observer.prune_expired_observations(tmp_path / "router-shadow" / "work") == 1
    assert not old.exists()


def test_observation_storage_rejects_unredacted_sensitive_content(tmp_path):
    observation = qwen35_observer.ShadowObservation(
        request_text="user@example.com",
        context_text="user@example.com",
        policy_version="adr-0013:v1",
        scope="personal",
        candidates=snapshot(),
        redaction_counts=(),
    )
    with pytest.raises(ValueError, match="sensitive"):
        qwen35_observer.persist_observation(observation, tmp_path / "router-shadow")
    assert not (tmp_path / "router-shadow").exists()


def test_observation_queue_shutdown_drops_queued_samples_and_counts_only():
    queue = qwen35_observer.ShadowObservationQueue(maxsize=1)
    observation = qwen35_observer.ShadowObservation(
        request_text="synthetic request", context_text="synthetic context",
        policy_version="adr-0013:v1", scope="personal", candidates=snapshot(), redaction_counts=(),
    )
    assert queue.submit(observation)
    assert queue.close() == 1
    assert queue.qsize == 0
    assert queue.dropped["shutdown_drop"] == 1
