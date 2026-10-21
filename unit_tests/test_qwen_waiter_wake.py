"""Qwen slot release must only wake runs queued for that slot."""
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ttakkari.models import Run, RunEvent, RunStatus
from ttakkari.runs import manager as manager_module


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def __iter__(self):
        return iter(self.rows)


class _Session:
    def __init__(self, runs, latest_events):
        self.runs = runs
        self.latest_events = iter(latest_events)
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def scalars(self, _statement):
        return _Result(self.runs)

    async def scalar(self, _statement):
        return next(self.latest_events)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_slot_wake_preserves_restart_retry_backoff(monkeypatch):
    now = datetime.now(UTC)
    slot_run = Run(id=uuid.uuid4(), session_id=uuid.uuid4(), workspace_id=uuid.uuid4(),
                   prompt="slot waiter", engine="qwen-local", status=RunStatus.queued,
                   created_at=now, resume_after=now + timedelta(seconds=30))
    retry_run = Run(id=uuid.uuid4(), session_id=uuid.uuid4(), workspace_id=uuid.uuid4(),
                    prompt="restart retry", engine="qwen-local", status=RunStatus.queued,
                    created_at=now + timedelta(seconds=1), resume_after=now + timedelta(minutes=2))
    slot_event = RunEvent(run_id=slot_run.id, seq=2, type="qwen.queued", payload={})
    retry_event = RunEvent(run_id=retry_run.id, seq=3, type="run.status",
                           payload={"status": "queued", "restart_resume_retry": True})
    db = _Session([slot_run, retry_run], [slot_event, retry_event])
    monkeypatch.setattr(manager_module, "sessionmaker", lambda: (lambda: db))
    monkeypatch.setattr(manager_module.manager, "_disarm", lambda _run_id: None)
    dispatch = AsyncMock()
    monkeypatch.setattr(manager_module.manager, "dispatch", dispatch)

    await manager_module.manager._wake_qwen_waiters()

    assert slot_run.resume_after is None
    assert retry_run.resume_after == now + timedelta(minutes=2)
    assert dispatch.await_args_list == [((slot_run.session_id,),)]
    assert db.commits == 1
