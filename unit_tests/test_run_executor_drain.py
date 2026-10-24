"""Unit tests for Step 2 RunExecutor drain, API remote mode, and queue isolation without Postgres."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ttakkari import generation
from ttakkari.executor import ExecutorRuntime, drain
from ttakkari.generation_service import coordinate_runs, start_api_services
from ttakkari.models import RunStatus, RuntimeCommand
from ttakkari.runs.manager import RunManager, _Handle


def _mock_session_factory(get_return=None):
    session = AsyncMock()
    session.get = AsyncMock(return_value=get_return)
    session.scalar = AsyncMock(return_value=None)
    session.scalars = AsyncMock(return_value=[])
    session.commit = AsyncMock()
    session.rollback = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)
    return lambda: cm


@pytest.mark.asyncio
async def test_drain_waits_for_active_handles_without_killing(monkeypatch):
    """SIGTERM / drain must not kill active processes; it must wait for handles to complete."""
    home = SimpleNamespace()
    runtime = ExecutorRuntime(home, revision="r1" * 20)
    runtime.state = "active"
    manager = RunManager(runtime=runtime)

    rid = uuid.uuid4()
    completed = asyncio.Event()

    # Create a fake long-running task representing an in-flight Run
    async def fake_run_task():
        await asyncio.sleep(0.05)
        completed.set()
        manager._handles.pop(rid, None)

    task = asyncio.create_task(fake_run_task())
    fake_proc = SimpleNamespace(returncode=None, pid=12345)
    manager._handles[rid] = _Handle(task=task, proc=fake_proc, owner=runtime)

    mock_row = MagicMock()
    mock_row.state = "active"
    monkeypatch.setattr("ttakkari.executor.sessionmaker", lambda: _mock_session_factory(mock_row))
    monkeypatch.setattr(runtime, "refresh", AsyncMock(return_value="draining"))
    runtime.commands = AsyncMock(return_value=[])

    # Execute drain
    drained = await drain(runtime, manager, poll_interval=0.01, timeout=2.0)

    assert drained is True
    assert completed.is_set()
    assert manager._handles == {}
    assert runtime.state == "draining"
    assert runtime.draining is True


@pytest.mark.asyncio
async def test_drain_stops_new_claims_and_dispatch(monkeypatch):
    """When executor is draining, coordinate_runs and dispatch must refuse new claims."""
    home = SimpleNamespace()
    runtime = ExecutorRuntime(home, revision="r1" * 20)
    runtime.state = "draining"
    runtime._draining = True
    manager = RunManager(runtime=runtime)

    resume_mock = AsyncMock()
    monkeypatch.setattr("ttakkari.runs.manager.resume_queued", resume_mock)
    runtime.commands = AsyncMock(return_value=[])

    # coordinate_runs must not call resume_queued when draining
    await coordinate_runs(runtime, manager, "draining")
    resume_mock.assert_not_awaited()

    # Even if state parameter is erroneously "active", runtime.draining guards it
    await coordinate_runs(runtime, manager, "active")
    resume_mock.assert_not_awaited()

    # manager.start must return False immediately
    assert manager.start(uuid.uuid4()) is False

    # manager.dispatch must return None immediately
    assert await manager.dispatch(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_drain_waits_for_inflight_stt_and_preview(monkeypatch):
    """Drain must wait for in-flight STT and preview work locks before terminating."""
    home = SimpleNamespace()
    runtime = ExecutorRuntime(home, revision="r1" * 20)
    runtime.state = "active"
    manager = RunManager(runtime=runtime)

    # Simulate in-flight STT and preview work locks
    runtime._work.add("stt")
    runtime._work.add("preview")

    mock_row = MagicMock()
    monkeypatch.setattr("ttakkari.executor.sessionmaker", lambda: _mock_session_factory(mock_row))
    monkeypatch.setattr(runtime, "refresh", AsyncMock(return_value="draining"))
    runtime.commands = AsyncMock(return_value=[])

    # Background task simulating STT and preview finishing after a brief delay
    async def finish_work():
        await asyncio.sleep(0.04)
        runtime._work.discard("stt")
        await asyncio.sleep(0.04)
        runtime._work.discard("preview")

    worker_task = asyncio.create_task(finish_work())

    drained = await drain(runtime, manager, poll_interval=0.01, timeout=2.0)
    await worker_task

    assert drained is True
    assert runtime._work == set()


@pytest.mark.asyncio
async def test_drain_processes_incoming_cancel_command(monkeypatch):
    """During drain, runtime commands like cancel must still be processed and acknowledged."""
    home = SimpleNamespace()
    runtime = ExecutorRuntime(home, revision="r1" * 20)
    runtime.state = "active"
    manager = RunManager(runtime=runtime)

    rid = uuid.uuid4()
    cancelled = asyncio.Event()

    async def fake_cancel(target_rid):
        if target_rid == rid:
            cancelled.set()
            manager._handles.pop(rid, None)
            return True
        return False

    manager.cancel = fake_cancel
    manager._handles[rid] = _Handle(task=asyncio.current_task())

    cmd = RuntimeCommand(id=uuid.uuid4(), key="test-cmd", kind="cancel", run_id=rid)
    runtime.commands = AsyncMock(side_effect=[[cmd], []])
    runtime.ack = AsyncMock()

    mock_run = MagicMock()
    mock_run.status = "cancelled"
    factory = _mock_session_factory(mock_run)
    monkeypatch.setattr("ttakkari.executor.sessionmaker", lambda: factory)
    monkeypatch.setattr("ttakkari.generation_service.sessionmaker", lambda: factory)
    monkeypatch.setattr(runtime, "refresh", AsyncMock(return_value="draining"))

    drained = await drain(runtime, manager, poll_interval=0.01, timeout=2.0)

    assert drained is True
    assert cancelled.is_set()
    runtime.ack.assert_awaited_once_with(cmd.id)


@pytest.mark.asyncio
async def test_drain_processes_reset_command(monkeypatch):
    """During drain, reset commands must be applied to session and acknowledged."""
    home = SimpleNamespace()
    runtime = ExecutorRuntime(home, revision="r1" * 20)
    runtime.state = "active"
    manager = RunManager(runtime=runtime)

    sess_id = uuid.uuid4()
    cmd = RuntimeCommand(id=uuid.uuid4(), key="reset-test", kind="reset", session_id=sess_id)
    runtime.commands = AsyncMock(side_effect=[[cmd], []])
    runtime.ack = AsyncMock()

    mock_sess = MagicMock()
    mock_sess.id = sess_id
    mock_sess.engine_sessions = {"claude:test": "state"}

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_sess)
    session.scalar = AsyncMock(return_value=None)
    session.scalars = AsyncMock(return_value=[cmd])
    session.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("ttakkari.executor.sessionmaker", lambda: lambda: cm)
    monkeypatch.setattr("ttakkari.generation_service.sessionmaker", lambda: lambda: cm)
    monkeypatch.setattr(runtime, "refresh", AsyncMock(return_value="draining"))

    drained = await drain(runtime, manager, poll_interval=0.01, timeout=2.0)

    assert drained is True
    assert sess_id in manager._reset_requests
    assert cmd.acknowledged_at is not None


@pytest.mark.asyncio
async def test_remote_api_does_not_start_stt_preview_maintenance():
    """In remote mode, API must not start STT, preview, or maintenance jobs."""
    with patch("ttakkari.artifacts.preview.start_worker", new_callable=AsyncMock) as preview_mock, \
         patch("ttakkari.meetings.local_stt.jobs.start", new_callable=AsyncMock) as stt_mock, \
         patch("ttakkari.tts.jobs.start", new_callable=AsyncMock) as tts_mock, \
         patch("ttakkari.runs.desk_qwen_service.service.start", new_callable=AsyncMock) as qwen_mock:

        await start_api_services("remote")

        preview_mock.assert_not_awaited()
        stt_mock.assert_not_awaited()
        tts_mock.assert_not_awaited()
        qwen_mock.assert_awaited_once_with(owner=False)


@pytest.mark.asyncio
async def test_generation_work_rejects_new_jobs_when_draining(monkeypatch):
    """generation.work must return False when runtime state is 'draining' without allow_draining."""
    home = SimpleNamespace()
    runtime = ExecutorRuntime(home, revision="r1" * 20)
    runtime.state = "draining"

    monkeypatch.setattr(runtime, "refresh", AsyncMock(return_value="draining"))
    monkeypatch.setattr("ttakkari.generation.current", runtime)

    async with generation.work("stt") as acquired:
        assert acquired is False

    async with generation.work("preview") as acquired:
        assert acquired is False

    async with generation.work("retention") as acquired:
        assert acquired is False


@pytest.mark.asyncio
async def test_remote_sse_streaming_from_db_events(monkeypatch):
    """Remote API SSE streaming must yield run events written by executor to DB and terminate on finish."""
    from ttakkari.api.sessions import stream_run
    from ttakkari.models import RunEvent, utcnow

    rid = uuid.uuid4()
    mock_run = MagicMock()
    mock_run.status = RunStatus.succeeded
    mock_run.last_seq = 2

    now = utcnow()
    ev1 = RunEvent(run_id=rid, seq=1, type="agent.thought", payload={"text": "thinking"}, created_at=now)
    ev2 = RunEvent(run_id=rid, seq=2, type="agent.response", payload={"text": "done"}, created_at=now)

    session = AsyncMock()
    session.get = AsyncMock(return_value=mock_run)
    session.scalars = AsyncMock(side_effect=[[ev1, ev2], []])
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    monkeypatch.setattr("ttakkari.api.sessions.sessionmaker", lambda: lambda: cm)
    fake_user = MagicMock()
    fake_request = MagicMock()
    fake_request.is_disconnected = AsyncMock(return_value=False)

    response = await stream_run(rid, fake_user, session, fake_request, after=0)
    assert response.status_code == 200

    # Consume the generator inside EventSourceResponse
    events_yielded = []
    async for item in response.body_iterator:
        events_yielded.append(str(item) if not isinstance(item, bytes) else item.decode())

    # Must contain both events and end event
    assert any("agent.thought" in ev for ev in events_yielded)
    assert any("agent.response" in ev for ev in events_yielded)
    assert any("end" in ev for ev in events_yielded), f"Yielded: {events_yielded}"
