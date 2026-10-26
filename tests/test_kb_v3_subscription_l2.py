"""Bounded subscription CLI L2 transport contracts."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from ttakkari.kb_pipeline.subscription_transport import (
    L2TransportError,
    DIAG_USAGE_LIMIT,
    resolve_target_pool,
    retry_seconds_from_error,
    validate_l2_extraction_schema,
)
from ttakkari.kb_pipeline.extract_l2 import extract_l2_from_passages


EMPTY_RESULT = {"entities": [], "assertions": [], "relations": []}


def test_unknown_or_personal_restricted_provenance_never_routes_to_cloud_pool():
    assert resolve_target_pool("personal", "work_only") is None
    assert resolve_target_pool("unknown", "global") is None
    assert resolve_target_pool("work", "work_only") == "work"
    assert resolve_target_pool("personal", "global") == "personal"


def test_provider_quota_diagnostic_and_relative_reset_are_preserved():
    from ttakkari.kb_pipeline.subscription_transport import diagnose_cli_error

    message = "Individual quota reached. Resets in 3h9m27s."
    assert diagnose_cli_error(message) == DIAG_USAGE_LIMIT
    assert retry_seconds_from_error(message) == 3 * 3600 + 9 * 60 + 57


@pytest.mark.parametrize(
    "payload",
    [
        {**EMPTY_RESULT, "unexpected": True},
        {"entities": [], "assertions": [], "relations": [], "meta": "extra"},
    ],
)
def test_strict_l2_validation_rejects_unexpected_top_level_keys(payload):
    with pytest.raises(L2TransportError, match="schema_violation"):
        validate_l2_extraction_schema(payload)


def test_strict_l2_validation_rejects_invalid_nested_field_types():
    payload = {
        "entities": [{"name": 1, "aliases": [], "kind": "concept", "domain": "ai"}],
        "assertions": [],
        "relations": [],
    }
    with pytest.raises(L2TransportError, match="schema_violation"):
        validate_l2_extraction_schema(payload)


async def test_usage_limit_stops_remaining_passage_calls():
    called = 0

    async def limited_call(_prompt, _schema):
        nonlocal called
        called += 1
        raise L2TransportError(DIAG_USAGE_LIMIT, "bounded error")

    passages = [
        SimpleNamespace(id=str(i), body=f"passage {i}", heading=None, section_path=[])
        for i in range(4)
    ]
    result = await extract_l2_from_passages(passages, llm_caller=limited_call, max_calls=4)
    assert result.calls_made == 1
    assert called == 1
    assert result.degradation_reason == "usage_limit"
