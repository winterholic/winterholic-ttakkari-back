"""Independent checks for the ArtCraft precise-evaluator composition boundary."""
from __future__ import annotations

import pytest

from ttakkari import collector_precise_runtime
from ttakkari.collector import precise_eval
from ttakkari.kb_pipeline.subscription_transport import L2TransportError


@pytest.mark.asyncio
async def test_actual_l2_transport_error_is_translated_with_all_fields_and_cause(monkeypatch):
    error = L2TransportError(
        "timeout", "synthetic boundary probe", retry_after=123.5, consumed=False,
        engine="codex", account="company-ai2", model="gpt-6-luna",
    )

    class Adapter:
        async def call_json(self, *_args, **_kwargs):
            raise error

    monkeypatch.setattr(collector_precise_runtime, "_adapter", lambda _scope: Adapter())
    with pytest.raises(precise_eval.PreciseEvaluatorTransportError) as raised:
        await collector_precise_runtime.evaluate(
            prompt="p", schema={}, system_prompt="s", scope="work"
        )
    translated = raised.value
    assert translated.diagnosis == "timeout"
    assert translated.consumed is False and translated.retry_after == 123.5
    assert (translated.engine, translated.account, translated.model) == (
        "codex", "company-ai2", "gpt-6-luna"
    )
    assert raised.value.__cause__ is error


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["evaluate", "availability"])
async def test_non_transport_exception_identity_is_preserved(monkeypatch, operation):
    sentinel = RuntimeError("synthetic unrelated failure")

    class Adapter:
        retry_after = None

        async def call_json(self, *_args, **_kwargs):
            raise sentinel

        async def availability(self):
            raise sentinel

    monkeypatch.setattr(collector_precise_runtime, "_adapter", lambda _scope: Adapter())
    with pytest.raises(RuntimeError) as raised:
        if operation == "evaluate":
            await collector_precise_runtime.evaluate(
                prompt="p", schema={}, system_prompt="s", scope="personal"
            )
        else:
            await collector_precise_runtime.availability(scope="personal")
    assert raised.value is sentinel


def test_scope_policy_and_malformed_response_boundaries():
    assert precise_eval.precise_eval_pool("personal") == "personal"
    assert precise_eval.precise_eval_pool("work") == "work"
    with pytest.raises(precise_eval.PreciseEvaluatorTransportError) as raised:
        precise_eval.precise_eval_pool("tenant-unknown")
    assert raised.value.diagnosis == "scope_capacity_unavailable"
    assert raised.value.consumed is False

    valid = {
        "topic_relevance": 0.8,
        "relevance_explanation": "evidence",
        "information_depth": 0.7,
        "low_quality_signals": [],
        "recommendation": "accept",
        "confidence": 0.9,
    }
    for malformed in (
        {**valid, "extra": "field"},
        {**valid, "confidence": True},
        {**valid, "recommendation": "maybe"},
        {**valid, "low_quality_signals": [""]},
    ):
        with pytest.raises(precise_eval.MalformedEvaluatorOutput):
            precise_eval.validate_evaluator_result(malformed)


@pytest.mark.asyncio
async def test_availability_translates_transport_to_waiting_tuple(monkeypatch):
    error = L2TransportError(
        "scope_capacity_unavailable", "pool busy", retry_after=88.0, consumed=False,
        engine="codex", account="company-ai2", model="gpt-6-luna",
    )

    class Adapter:
        retry_after = 99.0

        async def availability(self):
            raise error

    monkeypatch.setattr(collector_precise_runtime, "_adapter", lambda _scope: Adapter())
    assert await collector_precise_runtime.availability(scope="work") == (
        False, "scope_capacity_unavailable", 88.0
    )
