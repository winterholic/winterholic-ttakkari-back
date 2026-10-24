"""A closed route or zero counters do not acknowledge old transport completion."""
import asyncio

import pytest
from test_tts_first_transition_failure_recovery import Slots

from ttakkari import generation_deployment as gd
from ttakkari.deployment import JobStore


@pytest.mark.parametrize("receipt", [None, False, 0, "true"])
async def test_zero_counters_without_transport_completion_refuse_activation(tmp_path, receipt):
    slots = Slots()

    async def incomplete(_slot):
        return receipt

    slots.wait_tts_transport_drained = incomplete
    controller = gd.GenerationController(JobStore(tmp_path), slots=slots)
    reads = []

    async def count(_):
        reads.append("DB")
        return 0

    controller._previous_tts_queued = count
    with pytest.raises(gd.StageError, match="transport completion"):
        await controller.drain_previous_tts(slots.job(), timeout=0.1, poll=0)
    assert reads == []
    assert slots.closed is False and slots.restored == 1


async def test_transport_ack_precedes_http_and_job_observations(tmp_path):
    slots = Slots()
    entered, completed = asyncio.Event(), asyncio.Event()
    events = []
    snapshot = slots.snapshot

    async def transport(_):
        entered.set()
        await completed.wait()
        events.append("transport-complete")
        return True

    async def observed(slot):
        events.append("HTTP")
        return await snapshot(slot)

    async def count(_):
        events.append("DB")
        return 0

    slots.wait_tts_transport_drained = transport
    slots.snapshot = observed
    controller = gd.GenerationController(JobStore(tmp_path), slots=slots)
    controller._previous_tts_queued = count
    task = asyncio.create_task(controller.drain_previous_tts(slots.job(), timeout=1, poll=0))
    try:
        await asyncio.wait_for(entered.wait(), 0.5)
        assert not task.done() and events == ["HTTP"]  # initial identity/mode probe
        completed.set()
        await asyncio.wait_for(task, 0.5)
        assert events == ["HTTP", "transport-complete", "HTTP", "DB"]
        assert slots.closed is True
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_transport_ack_timeout_reopens_ingress(tmp_path):
    slots = Slots()

    async def never(_):
        await asyncio.Event().wait()

    slots.wait_tts_transport_drained = never
    controller = gd.GenerationController(JobStore(tmp_path), slots=slots)
    async def count(_):
        return 0
    controller._previous_tts_queued = count
    with pytest.raises(gd.StageError, match="transport completion"):
        await controller.drain_previous_tts(slots.job(), timeout=0.02, poll=0)
    assert not slots.closed and slots.restored == 1


async def test_stock_caddy_does_not_claim_a_shutdown_ack(tmp_path):
    with pytest.raises(gd.StageError, match="transport completion"):
        await gd.Slots(tmp_path).wait_tts_transport_drained(0)


async def test_confirmed_remote_api_does_not_require_legacy_transport_drain(tmp_path):
    slots = Slots()

    async def remote(_):
        return {"generation": str(slots.old_id), "execution_mode": "remote", "executor_protocol": 2}

    async def must_not_close(_):
        pytest.fail("remote API jobs belong to the durable executor queue, not the old API")

    slots.snapshot = remote
    slots.close_tts_ingress = must_not_close
    controller = gd.GenerationController(JobStore(tmp_path), slots=slots)
    await controller.drain_previous_tts(slots.job(), poll=0)


async def test_other_generation_cannot_skip_legacy_drain_by_advertising_remote(tmp_path):
    slots = Slots()

    async def remote(_):
        return {"generation": "wrong-owner", "execution_mode": "remote", "executor_protocol": 2}

    slots.snapshot = remote
    controller = gd.GenerationController(JobStore(tmp_path), slots=slots)
    with pytest.raises(gd.StageError, match="previous generation"):
        await controller.drain_previous_tts(slots.job(), poll=0)
    assert slots.restored == 0, "an identity failure before close must not change the route"


async def test_cancellation_during_transport_wait_restores_ingress(tmp_path):
    slots = Slots()
    entered = asyncio.Event()

    async def wait(_):
        entered.set()
        await asyncio.Event().wait()

    slots.wait_tts_transport_drained = wait
    controller = gd.GenerationController(JobStore(tmp_path), slots=slots)
    task = asyncio.create_task(controller.drain_previous_tts(slots.job(), poll=0))
    await asyncio.wait_for(entered.wait(), 0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not slots.closed and slots.restored == 1
