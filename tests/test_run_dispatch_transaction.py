"""Dispatch must release its transaction before the scheduled task can lock rows."""
from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunStatus, SessionKind, utcnow
from ttakkari.runs import manager as manager_mod


async def queued_run(new_session, *, desk=False):
    ws, sess = await new_session()
    sid = uuid.UUID(sess["id"])
    async with sessionmaker()() as db:
        if desk:
            chat = await db.get(ChatSession, sid)
            chat.kind = SessionKind.desk
        run = Run(session_id=sid, workspace_id=uuid.UUID(ws["id"]), prompt="resume",
                  engine="claude", status=RunStatus.queued,
                  resume_after=utcnow() - timedelta(seconds=1))
        db.add(run)
        await db.commit()
        return sid, run.id


async def test_dispatch_does_not_overlap_its_row_locks_with_the_started_run(new_session, monkeypatch):
    sid, rid = await queued_run(new_session)
    sm = sessionmaker()
    m = manager_mod.RunManager()
    worker_holds_run = asyncio.Event()
    tasks = []

    async def execute(run_id, lane="workers"):
        # The execution path locks Run and later writes/locks its ChatSession.
        # Keep these actual PG locks; only the engine subprocess is omitted.
        async with sm() as db:
            await db.execute(text("SET LOCAL statement_timeout = '5s'"))
            run = await db.scalar(select(Run).where(Run.id == run_id).with_for_update())
            worker_holds_run.set()
            await db.scalar(select(ChatSession).where(ChatSession.id == sid).with_for_update())
            run.status = RunStatus.running
            await db.commit()

    class DispatchSession(AsyncSession):
        async def commit(self):
            # Make the real race deterministic: an eagerly scheduled task gets
            # its Run lock before dispatch flushes the expired resume_after.
            if tasks:
                await asyncio.wait_for(worker_holds_run.wait(), 3)
            await super().commit()

    monkeypatch.setattr(m, "_execute", execute)
    original_start = m.start

    def tracked_start(run_id, **kwargs):
        started = original_start(run_id, **kwargs)
        if started:
            tasks.append(m._handles[run_id].task)
        return started

    monkeypatch.setattr(m, "start", tracked_start)
    monkeypatch.setattr(manager_mod, "sessionmaker", lambda: async_sessionmaker(
        sm.kw["bind"], class_=DispatchSession, expire_on_commit=False))
    try:
        dispatched = await asyncio.wait_for(m.dispatch(sid), 8)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert dispatched == rid
        assert results == [None], results
        async with sm() as db:
            run = await db.get(Run, rid)
            assert run.status == RunStatus.running
            assert run.resume_after is None
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("desk", [False, True])
async def test_failed_dispatch_commit_never_schedules_an_engine(new_session, monkeypatch, desk):
    sid, rid = await queued_run(new_session, desk=desk)
    sm = sessionmaker()
    m = manager_mod.RunManager()
    scheduled = []

    class DispatchSession(AsyncSession):
        async def commit(self):
            raise RuntimeError("dispatch commit rejected")

    monkeypatch.setattr(manager_mod, "sessionmaker", lambda: async_sessionmaker(
        sm.kw["bind"], class_=DispatchSession, expire_on_commit=False))
    monkeypatch.setattr(m, "start", lambda *args, **kwargs: scheduled.append(args[0]) or True)
    with pytest.raises(RuntimeError, match="dispatch commit rejected"):
        await m.dispatch(sid)
    assert scheduled == []
    async with sm() as db:
        run = await db.get(Run, rid)
        assert run.status == RunStatus.queued
        assert run.resume_after is not None


@pytest.mark.parametrize("desk", [False, True])
async def test_concurrent_dispatch_still_starts_one_task(new_session, monkeypatch, desk):
    sid, rid = await queued_run(new_session, desk=desk)
    m = manager_mod.RunManager()
    release = asyncio.Event()
    started = asyncio.Event()
    entered = []

    async def execute(run_id, lane="workers"):
        entered.append((run_id, lane))
        started.set()
        await release.wait()

    async def execute_desk(run_id):
        await execute(run_id, lane="desk")

    monkeypatch.setattr(m, "_execute", execute)
    monkeypatch.setattr(m, "_execute_desk", execute_desk)
    tasks = []
    try:
        await asyncio.gather(m.dispatch(sid), m.dispatch(sid))
        tasks = [handle.task for handle in m._handles.values()]
        assert len(tasks) == 1
        await asyncio.wait_for(started.wait(), 3)
        assert entered == [(rid, "desk" if desk else "workers")]
    finally:
        release.set()
        await asyncio.gather(*tasks)


async def test_targeted_dispatch_never_skips_an_older_queued_run(new_session, monkeypatch):
    sid, first = await queued_run(new_session)
    async with sessionmaker()() as db:
        chat = await db.get(ChatSession, sid)
        second = Run(session_id=sid, workspace_id=chat.workspace_id, prompt="second",
                     engine="claude", status=RunStatus.queued)
        db.add(second)
        await db.commit()
        second_id = second.id

    m = manager_mod.RunManager()
    started = []
    monkeypatch.setattr(m, "start", lambda run_id, **_: started.append(run_id) or True)

    result = await m.dispatch(sid, expected_run_id=second_id, expected_pool="personal")

    assert result is None
    assert started == []
    async with sessionmaker()() as db:
        assert (await db.get(Run, first)).status == RunStatus.queued
        assert (await db.get(Run, second_id)).status == RunStatus.queued
