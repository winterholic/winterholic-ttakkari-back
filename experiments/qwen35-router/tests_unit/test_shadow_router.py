from __future__ import annotations

import asyncio

import httpx
import pytest

from ttakkari.experiments import qwen35_shadow
from ttakkari.experiments.qwen35_shadow import (
    CandidateSnapshot,
    ShadowQueue,
    build_response_schema,
    validate_shadow_output,
)


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

        async def post(self, *_args, **_kwargs):
            raise failure

    monkeypatch.setattr(qwen35_shadow.httpx, "AsyncClient", FailingClient)

    async def scenario():
        with pytest.raises(type(failure)):
            await qwen35_shadow.infer_once("http://127.0.0.1:18786", "synthetic request", snapshot())

    asyncio.run(scenario())
