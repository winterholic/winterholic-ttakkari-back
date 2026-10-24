"""Independent boundary probe; an old-model measurement, not live proxy evidence."""
import asyncio

from test_tts_first_transition import (
    _Slots,
    _controller,
    _job,
    _job_row,
    _old_accept,
    _old_guard_pass,
    _seed,
)

from ttakkari.generation import activate


async def test_request_forwarded_before_close_but_delivered_after_idle_observation(tmp_path):
    old_id, new_id = await _seed()
    slots = _Slots(old_id, inflight=0)
    controller = _controller(tmp_path, slots)
    # A request has passed the old proxy route but has not yet entered the API.
    # There is no upper bound or acknowledgment for this transport in the gate.
    await controller.drain_previous_tts(_job(old_id), timeout=1, poll=0.01)
    gate, checked = asyncio.Event(), asyncio.Event()
    post = asyncio.create_task(_old_accept(old_id, gate, checked, ["accepted original"]))
    await checked.wait()  # Delivered in the gap before activate; old API accepts.
    await activate(new_id, expected=old_id)
    gate.set()
    job_id = await post
    await _old_guard_pass(old_id)
    status, error, chunks = await _job_row(job_id)
    assert chunks == ["accepted original"]
    assert (status, error) != ("failed", "generation_draining")
