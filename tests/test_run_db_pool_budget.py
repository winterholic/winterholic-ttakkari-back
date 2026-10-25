"""A burst of Runs starting together must not exhaust the DB pool.

2026-10-25 07:30:59 UTC: ten Runs that had been waiting for the same usage-limit reset
started at once and all failed with `QueuePool limit of size 5 overflow 3 reached`.
Each `_run` keeps its own session (one pooled connection) open while routing and, inside
that, checks out a second connection (choose_profile, running_counts_async, ...). With
more starters than the pool has connections, every connection is held by an outer session
waiting for an inner one: a hold-and-wait deadlock that only ends at the 30 s pool timeout.
"""
from __future__ import annotations

import asyncio
import types
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ttakkari import db as db_mod
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import Run, RunEvent, RunStatus, utcnow
from ttakkari.runs.manager import (
    MAX_POOL_TIMEOUT_RETRIES,
    RunManager,
    _Handle,
    attempt_handle,
    routing_slots,
)

POOL_ERROR = "QueuePool limit of size 5 overflow 3 reached, connection timed out, timeout 30.00"


@pytest.fixture
async def small_pool(monkeypatch):
    """Three connections in total, with a short checkout timeout so a deadlock shows up in seconds."""
    s = get_settings()
    monkeypatch.setattr(s, "database_pool_size", 2)
    monkeypatch.setattr(s, "database_pool_overflow", 1)
    engine = create_async_engine(str(s.database_url), pool_size=2, max_overflow=1, pool_timeout=1.5)
    monkeypatch.setattr(db_mod, "_engine", engine)
    monkeypatch.setattr(db_mod, "_sessionmaker", async_sessionmaker(engine, expire_on_commit=False))
    yield engine
    await engine.dispose()


@pytest.fixture
def isolated_manager(monkeypatch):
    """A fresh manager that never starts real Runs from its own follow-up dispatch."""
    m = RunManager()

    async def no_dispatch(_session_id):
        return None

    monkeypatch.setattr(m, "dispatch", no_dispatch)
    monkeypatch.setattr(m, "start", lambda *_a, **_k: False)
    return m


async def make_runs(new_session, n: int, *, status: str = RunStatus.queued) -> list[uuid.UUID]:
    ws, sess = await new_session()
    ids: list[uuid.UUID] = []
    async with sessionmaker()() as db:
        for _ in range(n):
            run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                      prompt="pool budget", engine="claude", status=status)
            db.add(run)
            await db.flush()
            ids.append(run.id)
        await db.commit()
    return ids


def launch(m: RunManager, run_id: uuid.UUID) -> asyncio.Task:
    task = asyncio.create_task(m._execute(run_id))
    m._handles[run_id] = _Handle(task=task)
    return task


async def load(run_id: uuid.UUID) -> Run:
    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        assert run is not None
        return run


def test_routing_slots_leave_room_for_the_nested_checkout_and_the_api(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "database_pool_size", 5)
    monkeypatch.setattr(s, "database_pool_overflow", 3)
    slots = routing_slots()
    # Each routing Run needs its own connection plus one nested; two stay free for the API.
    assert slots >= 1
    assert 2 * slots + 2 <= s.database_pool_size + s.database_pool_overflow
    monkeypatch.setattr(s, "database_pool_size", 1)
    monkeypatch.setattr(s, "database_pool_overflow", 0)
    assert routing_slots() == 1


async def test_burst_of_run_starts_does_not_deadlock_the_pool(new_session, small_pool, isolated_manager, monkeypatch):
    m = isolated_manager
    run_ids = await make_runs(new_session, 6)
    in_routing = 0
    peak = 0

    async def fake_run(db, run_id):
        nonlocal in_routing, peak
        in_routing += 1
        peak = max(peak, in_routing)
        await db.execute(text("SELECT 1"))  # _run's first db.get(Run) opens the outer transaction
        await asyncio.sleep(0.05)  # let sibling starters pile up behind us
        async with sessionmaker()() as nested:  # choose_profile / running_counts_async
            await nested.execute(text("SELECT 1"))
        await db.commit()  # _attempt's commit once the engine process is up
        in_routing -= 1
        attempt_handle.get().release_start_slot()
        await asyncio.sleep(0.2)  # the long engine phase holds no connection

    monkeypatch.setattr(m, "_run", fake_run)
    await asyncio.wait_for(asyncio.gather(*(launch(m, rid) for rid in run_ids)), 30)

    statuses = [(await load(rid)).status for rid in run_ids]
    assert statuses == [RunStatus.queued] * len(run_ids), statuses  # none failed with a pool timeout
    assert peak <= routing_slots()
    assert not m._handles


async def test_slot_is_released_when_run_returns_before_spawning(new_session, small_pool, isolated_manager, monkeypatch):
    m = isolated_manager
    run_ids = await make_runs(new_session, 3)

    async def waits_for_reset(db, run_id):
        await db.execute(text("SELECT 1"))  # e.g. _wait_for_reset: returns without ever spawning

    monkeypatch.setattr(m, "_run", waits_for_reset)
    await asyncio.wait_for(asyncio.gather(*(launch(m, rid) for rid in run_ids)), 10)
    assert m._routing is not None and m._routing._value == routing_slots()


async def test_pool_timeout_requeues_the_run_with_backoff_instead_of_failing(new_session, isolated_manager, monkeypatch):
    m = isolated_manager
    (run_id,) = await make_runs(new_session, 1)

    async def exhausted(db, run_id):
        raise PoolTimeoutError(POOL_ERROR)

    monkeypatch.setattr(m, "_run", exhausted)
    await launch(m, run_id)

    run = await load(run_id)
    try:
        assert run.status == RunStatus.queued, run.error
        assert run.error is None and run.finished_at is None
        assert run.resume_after is not None and run.resume_after > utcnow() + timedelta(seconds=1)
        assert m.is_waiting(run_id)  # re-armed, so a restart-free retry happens
        async with sessionmaker()() as db:
            events = list(await db.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                           .order_by(RunEvent.seq)))
        retry = [e for e in events if e.payload.get("pool_timeout_retry")]
        assert len(retry) == 1 and retry[0].payload["attempt"] == 1
        assert not any(e.type == "run.finished" for e in events)
    finally:
        m._disarm(run_id)


async def test_pool_timeout_after_status_running_goes_back_to_queued(new_session, isolated_manager, monkeypatch):
    m = isolated_manager
    (run_id,) = await make_runs(new_session, 1, status=RunStatus.running)

    async def exhausted(db, run_id):
        raise PoolTimeoutError(POOL_ERROR)

    monkeypatch.setattr(m, "_run", exhausted)
    await launch(m, run_id)
    try:
        run = await load(run_id)
        assert run.status == RunStatus.queued and run.pid is None
    finally:
        m._disarm(run_id)


async def test_pool_timeout_gives_up_after_the_retry_budget(new_session, isolated_manager, monkeypatch):
    m = isolated_manager
    (run_id,) = await make_runs(new_session, 1)

    async def exhausted(db, run_id):
        raise PoolTimeoutError(POOL_ERROR)

    monkeypatch.setattr(m, "_run", exhausted)
    for _ in range(MAX_POOL_TIMEOUT_RETRIES):
        await launch(m, run_id)
        assert (await load(run_id)).status == RunStatus.queued
    await launch(m, run_id)
    m._disarm(run_id)

    run = await load(run_id)
    assert run.status == RunStatus.failed
    assert "QueuePool" in (run.error or "")


async def test_pool_timeout_never_requeues_a_run_whose_engine_process_started(new_session, isolated_manager, monkeypatch):
    m = isolated_manager
    (run_id,) = await make_runs(new_session, 1, status=RunStatus.running)

    async def exhausted_after_spawn(db, run_id):
        attempt_handle.get().proc = types.SimpleNamespace(returncode=None, pid=1)
        raise PoolTimeoutError(POOL_ERROR)

    monkeypatch.setattr(m, "_run", exhausted_after_spawn)
    await launch(m, run_id)

    run = await load(run_id)
    assert run.status == RunStatus.failed  # a live process may already have produced side effects


async def test_other_errors_still_fail_the_run(new_session, isolated_manager, monkeypatch):
    m = isolated_manager
    (run_id,) = await make_runs(new_session, 1)

    async def boom(db, run_id):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "_run", boom)
    await launch(m, run_id)

    run = await load(run_id)
    assert run.status == RunStatus.failed and "boom" in (run.error or "")
