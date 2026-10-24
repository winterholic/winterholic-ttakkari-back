"""A failed residual drain must remain supervised without extending its deadline."""
import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ttakkari import artcraft, generation_service
from ttakkari.google_account.login import manager as login_manager


class Runtime:
    def __init__(self):
        self.signal = asyncio.Event()
        self.states = asyncio.Queue()
        self.ticks = asyncio.Queue()
        self._listener = SimpleNamespace(is_closed=lambda: False)

    async def listen(self):
        pass

    async def refresh(self):
        return await self.states.get()

    async def tick(self, state="draining"):
        self.states.put_nowait(state)
        self.signal.set()
        await asyncio.wait_for(self.ticks.get(), 1)

    async def snapshot(self):
        return {"state": "draining"}

    async def can_retire(self):
        return True


@pytest.fixture
def coordinator(monkeypatch):
    runtime = Runtime()

    async def tick_done(*_):
        runtime.ticks.put_nowait(None)

    monkeypatch.setattr(generation_service, "coordinate_runs", tick_done)
    monkeypatch.setattr(generation_service, "get_settings", lambda: SimpleNamespace(runtime_poll_seconds=0.001))
    monkeypatch.setattr("ttakkari.runs.desk_pool.shutdown", AsyncMock())
    monkeypatch.setattr("ttakkari.runs.profiles.load_cooldowns", AsyncMock())
    monkeypatch.setattr("ttakkari.runs.usage_store.load_latest", AsyncMock())
    db = AsyncMock()
    monkeypatch.setattr(generation_service, "sessionmaker", lambda: lambda: db)
    return runtime


@pytest.mark.parametrize("service", ["artcraft", "google_login"])
@pytest.mark.parametrize("failure", ["exception", "false"])
async def test_retries_remaining_work_without_resetting_deadline(monkeypatch, coordinator, service, failure):
    clock = [100.0]
    monkeypatch.setattr(generation_service, "monotonic", lambda: clock[0], raising=False)
    owner = artcraft if service == "artcraft" else login_manager
    other = login_manager if service == "artcraft" else artcraft
    remaining = [1]
    monkeypatch.setattr(owner, "inflight", lambda: remaining[0])
    monkeypatch.setattr(other, "inflight", lambda: 0)
    calls = []
    first, recovered = asyncio.Event(), asyncio.Event()

    async def drain(*, timeout):
        calls.append(timeout)
        if len(calls) == 1:
            first.set()
            if failure == "exception":
                raise RuntimeError("transient cleanup failure")
            return False
        remaining[0] = 0
        recovered.set()
        return False  # Timeout cancellation can return False after all work has stopped.

    monkeypatch.setattr(owner, "drain", drain)
    task = asyncio.create_task(generation_service.coordinate(coordinator, SimpleNamespace(remote=True)))
    try:
        await coordinator.tick()
        await asyncio.wait_for(first.wait(), 1)
        await asyncio.sleep(0.01)  # Let gather publish the failed attempt.
        clock[0] = 150.0
        await coordinator.tick()
        await asyncio.wait_for(recovered.wait(), 0.2)
        assert calls[0] == (30.0 if service == "artcraft" else 20.0)
        assert calls[1] == 0.0
        assert remaining[0] == 0
        await asyncio.sleep(0.01)
        await coordinator.tick()
        await asyncio.sleep(0.01)
        assert len(calls) == 2  # False after successful cancellation must not start an endless drain.
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unexpected_watcher_exception_is_observed_and_retried(monkeypatch, coordinator, caplog):
    remaining = [1]
    monkeypatch.setattr(artcraft, "inflight", lambda: remaining[0])
    monkeypatch.setattr(login_manager, "inflight", lambda: 0)
    recovered = asyncio.Event()
    calls = []

    async def watcher(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("watcher failed outside gather")
        remaining[0] = 0
        recovered.set()
        return {"artcraft": True, "google_login": True}

    monkeypatch.setattr(generation_service, "drain_residual_jobs", watcher)
    task = asyncio.create_task(generation_service.coordinate(coordinator, SimpleNamespace(remote=True)))
    try:
        await coordinator.tick()
        await asyncio.sleep(0.01)
        await coordinator.tick()
        await asyncio.wait_for(recovered.wait(), 0.2)
        assert any("watcher failed" in record.message for record in caplog.records)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_rollback_awaits_watcher_cleanup_before_reentry(monkeypatch, coordinator):
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    starts, live = [], []
    clock = [100.0]
    monkeypatch.setattr(generation_service, "monotonic", lambda: clock[0])

    async def watcher(**kwargs):
        starts.append(kwargs)
        live.append(1)
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
            await release.wait()
            live.pop()

    monkeypatch.setattr(generation_service, "drain_residual_jobs", watcher)
    task = asyncio.create_task(generation_service.coordinate(coordinator, SimpleNamespace(remote=True)))
    rollback = None
    try:
        await coordinator.tick()
        await asyncio.wait_for(entered.wait(), 1)
        rollback = asyncio.create_task(coordinator.tick("active"))
        await asyncio.wait_for(cancelled.wait(), 1)
        await asyncio.sleep(0.01)
        assert not rollback.done(), "rollback must await cancellation cleanup before reopening admission"
        release.set()
        await rollback
        entered.clear()
        clock[0] = 200.0
        await coordinator.tick()
        await asyncio.wait_for(entered.wait(), 1)
        assert len(starts) == 2
        assert starts[0] == starts[1] == {"artcraft_timeout": 30.0, "login_timeout": 20.0}
        assert len(live) == 1
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if rollback:
            await asyncio.gather(rollback, return_exceptions=True)
    assert not live, "coordinator shutdown must join its watcher"


@pytest.mark.parametrize("service", ["artcraft", "google_login"])
async def test_real_job_exception_recovery_unblocks_controller_retirement(monkeypatch, coordinator, service):
    from ttakkari.generation_deployment import GenerationController

    monkeypatch.setattr(artcraft, "JOBS", {})
    monkeypatch.setattr(login_manager, "_tasks", {})
    release, started, failed = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def runner(job=None):
        started.set()
        await release.wait()

    if service == "artcraft":
        job = artcraft._start("smoke", "smoke-cli", [], None, runner)
        work = job.task
        owner = artcraft
    else:
        work = asyncio.create_task(runner())
        login_manager._tasks["test"] = work
        owner = login_manager
    await started.wait()
    original_drain = owner.drain
    attempts = []

    async def transient_drain(*, timeout):
        attempts.append(timeout)
        if len(attempts) == 1:
            failed.set()
            raise RuntimeError("one cleanup failure with real work")
        return await original_drain(timeout=timeout)

    monkeypatch.setattr(owner, "drain", transient_drain)
    monkeypatch.setattr(generation_service, "ARTCRAFT_DRAIN_TIMEOUT", 0.0)
    monkeypatch.setattr(generation_service, "GOOGLE_LOGIN_DRAIN_TIMEOUT", 0.0)
    old, new = uuid.uuid4(), uuid.uuid4()
    deployment = {"status": "draining", "previous_slot": "blue", "previous_generation": str(old),
                  "candidate_generation": str(new)}

    async def snapshot(slot):
        return {**await generation_service.ready(coordinator), "generation": str(old)}

    slots = SimpleNamespace(snapshot=snapshot, stop=AsyncMock())
    controller = SimpleNamespace(slots=slots, active=AsyncMock(return_value=new),
                                 phase=lambda job, status: job.update(status=status),
                                 succeed=lambda job: job.update(status="succeeded"))
    task = asyncio.create_task(generation_service.coordinate(coordinator, SimpleNamespace(remote=True)))
    try:
        await GenerationController.retire(controller, deployment)
        slots.stop.assert_not_awaited()
        await coordinator.tick()
        await asyncio.wait_for(failed.wait(), 1)
        await asyncio.sleep(0.01)
        assert (await generation_service.ready(coordinator))["drain_ready"] is False
        await coordinator.tick()
        await asyncio.wait_for(asyncio.gather(work, return_exceptions=True), 1)
        assert len(attempts) == 2
        assert (await generation_service.ready(coordinator))["api_residual"] == 0
        await GenerationController.retire(controller, deployment)
        slots.stop.assert_awaited_once_with("blue")
        assert deployment["status"] == "succeeded"
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def test_persistent_failure_stays_not_ready_and_retries_once_per_tick(monkeypatch, coordinator, caplog):
    monkeypatch.setattr(artcraft, "inflight", lambda: 1)
    monkeypatch.setattr(login_manager, "inflight", lambda: 0)
    drain = AsyncMock(side_effect=RuntimeError("persistent cleanup failure"))
    monkeypatch.setattr(artcraft, "drain", drain)
    task = asyncio.create_task(generation_service.coordinate(coordinator, SimpleNamespace(remote=True)))
    try:
        for i in range(3):
            await coordinator.tick()
            await asyncio.sleep(0.01)
            assert drain.await_count == i + 1
            assert (await generation_service.ready(coordinator))["drain_ready"] is False
        assert sum("persistent cleanup failure" in record.message for record in caplog.records) == 3
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
