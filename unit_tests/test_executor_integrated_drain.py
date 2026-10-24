"""Cross-module shutdown contracts. No model servers, host signals or DB access."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ttakkari import background_consumers, executor, generation_service
from ttakkari.tts import jobs as tts_jobs


def runtime_and_db(tmp_path, monkeypatch):
    runtime = executor.ExecutorRuntime(tmp_path, revision="a" * 40)
    runtime.state = "active"
    runtime.commands = AsyncMock(return_value=[])
    runtime.refresh = AsyncMock(return_value="draining")
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(state="active")
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=db)
    context.__aexit__ = AsyncMock(return_value=None)
    monkeypatch.setattr(executor, "sessionmaker", lambda: lambda: context)
    return runtime


async def test_executor_keeps_heartbeating_while_other_consumers_drain(tmp_path, monkeypatch):
    runtime = runtime_and_db(tmp_path, monkeypatch)
    owner = asyncio.create_task(asyncio.sleep(0.035))
    try:
        assert await executor.drain(runtime, SimpleNamespace(_handles={}),
                                    pending=(owner,), poll_interval=0.003, timeout=1)
        assert owner.done()
        assert runtime.refresh.await_count > 1
    finally:
        await owner


async def test_executor_deadline_does_not_cancel_other_consumer_drain(tmp_path, monkeypatch):
    runtime = runtime_and_db(tmp_path, monkeypatch)
    owner = asyncio.create_task(asyncio.Event().wait())
    try:
        assert not await executor.drain(runtime, SimpleNamespace(_handles={}),
                                        pending=(owner,), poll_interval=0.001, timeout=0.01)
        assert not owner.done()
    finally:
        owner.cancel()
        await asyncio.gather(owner, return_exceptions=True)


async def test_executor_can_request_unbounded_background_drain_without_cancelling_work(monkeypatch):
    active = True
    monkeypatch.setattr(background_consumers, "inflight", lambda: int(active))
    cancel = AsyncMock()
    monkeypatch.setattr(background_consumers, "_cancel_inflight", cancel)
    consumer = background_consumers.BackgroundConsumers()

    async def finish():
        nonlocal active
        await asyncio.sleep(0.025)
        active = False

    task = asyncio.create_task(finish())
    try:
        await asyncio.wait_for(consumer.drain(drain_seconds=None), 1)
        assert task.done()
        cancel.assert_not_awaited()
    finally:
        await task


def test_tts_claims_can_be_closed_synchronously_before_waiting_for_runs(monkeypatch):
    monkeypatch.setattr(tts_jobs, "_draining", False)
    tts_jobs.stop_claiming()
    assert tts_jobs._draining is True


async def test_remote_qwen_internal_typeerror_does_not_retry_as_process_owner(monkeypatch):
    from ttakkari.runs.desk_qwen_service import service
    start = AsyncMock(side_effect=TypeError("internal service error"))
    monkeypatch.setattr(service, "start", start)
    with pytest.raises(TypeError, match="internal service error"):
        await generation_service.start_api_services("remote")
    assert start.await_count == 1
    start.assert_awaited_once_with(owner=False)
