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
from ttakkari.runs import manager as manager_mod
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
    # ba4f0220: 일시 오류는 같은 Run 을 자동 재시도한다. 이 시험은 최종 실패 기록을 보므로 재시도 예산을 0 으로 둔다.
    monkeypatch.setattr(manager_mod, "MAX_EXECUTION_RECOVERY_ATTEMPTS", 0)
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


# ---- 접수(desk): 바깥 세션이 연결을 쥔 채 안쪽 연결을 여는 같은 교착

async def make_desk_runs(client, auth, make_dir, n: int) -> list[uuid.UUID]:
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    desk = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "desk"}, headers=auth)).json()
    ids: list[uuid.UUID] = []
    async with sessionmaker()() as db:
        for i in range(n):
            run = Run(session_id=uuid.UUID(desk["id"]), workspace_id=uuid.UUID(ws["id"]),
                      prompt=f"접수 {i}", engine="auto", status=RunStatus.queued)
            db.add(run)
            await db.flush()
            ids.append(run.id)
        await db.commit()
    return ids


@pytest.fixture
def desk_runtime_like(monkeypatch):
    """generation 모드의 접수 경로: 계정 선택이 안쪽 연결을 따로 열고, 답은 0.3초 뒤에 온다."""
    from ttakkari.runs import desk, profiles

    prof = profiles.Profile(id=uuid.uuid4(), name="fake-codex", engine="codex", model="m")

    async def availability(_db, _engine, _pool, **_kw):
        return [prof], []

    async def choose_async(pool, _run_id, **_kw):
        async with sessionmaker()() as nested:  # GenerationRuntime.choose_profile 이 하는 일
            await nested.execute(text("SELECT 1"))
        return pool[0]

    async def ask(_prof, _system, _prompt, **_kw):
        await asyncio.sleep(0.3)
        return desk.Attempt('{"action": "answer", "reply": "ok"}', None, 300)

    monkeypatch.setattr(profiles, "availability", availability)
    monkeypatch.setattr(profiles, "choose_async", choose_async)
    monkeypatch.setattr(desk, "_ask", ask)
    monkeypatch.setattr(desk.desk_qwen, "available", lambda: False)


async def test_burst_of_desk_runs_does_not_deadlock_the_pool(
        client, auth, make_dir, small_pool, isolated_manager, desk_runtime_like):
    m = isolated_manager
    run_ids = await make_desk_runs(client, auth, make_dir, 8)

    def launch_desk(run_id):
        task = asyncio.create_task(m._execute_desk(run_id))
        m._handles[run_id] = _Handle(task=task)
        return task

    await asyncio.wait_for(asyncio.gather(*(launch_desk(rid) for rid in run_ids)), 60)

    runs = [await load(rid) for rid in run_ids]
    assert [r.status for r in runs] == [RunStatus.succeeded] * len(run_ids), [(r.status, r.error) for r in runs]
    assert not m._handles


async def test_desk_pool_timeout_requeues_instead_of_failing(client, auth, make_dir, isolated_manager, monkeypatch):
    from ttakkari.runs import desk

    m = isolated_manager
    (run_id,) = await make_desk_runs(client, auth, make_dir, 1)

    async def exhausted(_db, _run, _sess):
        raise PoolTimeoutError(POOL_ERROR)

    monkeypatch.setattr(desk, "handle", exhausted)
    task = asyncio.create_task(m._execute_desk(run_id))
    m._handles[run_id] = _Handle(task=task)
    await task
    try:
        run = await load(run_id)
        assert run.status == RunStatus.queued, run.error  # 아직 아무것도 확정되지 않았으니 다시 접수한다
        assert run.resume_after is not None and m.is_waiting(run_id)
    finally:
        m._disarm(run_id)


# ---- 폴백 재선택 직전에 바깥 연결 반납

async def test_release_connection_ends_the_outer_transaction(new_session, isolated_manager):
    m = isolated_manager
    (run_id,) = await make_runs(new_session, 1, status=RunStatus.running)
    async with sessionmaker()() as db:
        run = await db.get(Run, run_id)
        await db.execute(text("SELECT 1"))
        assert db.in_transaction()  # available_profiles() 같은 조회가 연결을 쥔 상태
        await m._release_connection(db, run)
        assert not db.in_transaction()  # 안쪽 연결을 열기 전에 풀로 돌아갔다


# ---- 재큐잉 뒤 종결되는 Run 의 재시도 기록 정리

async def test_finishing_a_requeued_run_clears_its_retry_counter(new_session, isolated_manager, monkeypatch):
    m = isolated_manager
    (run_id,) = await make_runs(new_session, 1)

    async def exhausted(db, run_id):
        raise PoolTimeoutError(POOL_ERROR)

    monkeypatch.setattr(m, "_run", exhausted)
    await launch(m, run_id)
    assert m._pool_retries == {run_id: 1}
    try:
        async with sessionmaker()() as db:
            run = await db.get(Run, run_id)
            await m.cancel_queued(db, run)  # 재큐잉 뒤 사용자가 취소: _execute 를 다시 거치지 않는다
            await db.commit()
        assert run_id not in m._pool_retries
    finally:
        m._disarm(run_id)


# ---- 접수: 기획 Run 이 이미 확정된 뒤의 풀 timeout 은 다시 접수하면 안 된다

def _flaky_finish(monkeypatch, run_id: uuid.UUID) -> None:
    orig = RunManager._finish
    fired = {"n": 0}

    async def finish(self, db, run, status, error=None):
        if run.id == run_id and status == RunStatus.succeeded and fired["n"] == 0:
            fired["n"] += 1
            raise PoolTimeoutError(POOL_ERROR)  # _finish 의 첫 쿼리에서 풀이 말랐다
        return await orig(self, db, run, status, error)

    monkeypatch.setattr(RunManager, "_finish", finish)


async def _lead_runs(run_id: uuid.UUID) -> list[Run]:
    from ttakkari.models import ChatSession

    async with sessionmaker()() as db:
        desk_run = await db.get(Run, run_id)
        leads = list(await db.scalars(select(ChatSession.id).where(ChatSession.parent_id == desk_run.session_id)))
        return list(await db.scalars(select(Run).where(Run.session_id.in_(leads)))) if leads else []


async def _run_desk(m: RunManager, run_id: uuid.UUID) -> None:
    task = asyncio.create_task(m._execute_desk(run_id))
    m._handles[run_id] = _Handle(task=task)
    await task


async def test_desk_is_not_requeued_after_the_lead_run_was_committed(
        client, auth, make_dir, isolated_manager, desk_runtime_like, monkeypatch):
    from ttakkari.runs import desk
    from ttakkari.runs.manager import manager as global_manager

    m = isolated_manager

    async def no_dispatch(_session_id):
        return None

    monkeypatch.setattr(global_manager, "dispatch", no_dispatch)  # queue_run 이 쓰는 전역 manager

    async def delegate(_prof, _system, _prompt, **_kw):
        return desk.Attempt('{"action": "delegate", "reply": "넘겼어요", "title": "plan x"}', None, 1)

    monkeypatch.setattr(desk, "_ask", delegate)
    (run_id,) = await make_desk_runs(client, auth, make_dir, 1)
    _flaky_finish(monkeypatch, run_id)

    await _run_desk(m, run_id)
    try:
        if (await load(run_id)).status == RunStatus.queued:  # 교정 전: 다시 접수가 돈다
            await _run_desk(m, run_id)
        run = await load(run_id)
        assert len(await _lead_runs(run_id)) == 1, "duplicate planning"
        assert run.status == RunStatus.failed and "QueuePool" in (run.error or "")
        assert run_id not in m._pool_retries and not m.is_waiting(run_id)
    finally:
        m._disarm(run_id)


async def test_desk_is_still_requeued_when_nothing_was_committed_yet(
        client, auth, make_dir, isolated_manager, desk_runtime_like, monkeypatch):
    m = isolated_manager  # desk_runtime_like 의 답은 action=answer: 기획으로 넘기지 않는다
    (run_id,) = await make_desk_runs(client, auth, make_dir, 1)
    _flaky_finish(monkeypatch, run_id)

    await _run_desk(m, run_id)
    try:
        run = await load(run_id)
        assert run.status == RunStatus.queued and m.is_waiting(run_id), run.error
        assert await _lead_runs(run_id) == []
    finally:
        m._disarm(run_id)


# ---- 시작 전 풀 timeout 재큐잉이 재개 안내문(resume_handoff)을 잃지 않는다

@pytest.mark.parametrize("source", ["column", "waiting_event", "both"])
async def test_requeue_before_spawn_keeps_the_resume_handoff(new_session, monkeypatch, source):
    """재시작 재개는 resume_handoff 컬럼, 한도 대기는 컬럼과 run.waiting 이벤트에 안내문을 둔다.

    _run 이 첫 커밋에서 컬럼을 지우면, 엔진이 뜨기 전 풀 timeout 으로 다시 돌 때 컬럼 쪽 안내문이 사라졌다."""
    from conftest import agent_calls

    from ttakkari.models import utcnow
    from ttakkari.runs.manager import manager

    monkeypatch.setattr(manager_mod, "POOL_TIMEOUT_BACKOFF_SECONDS", 0.2)
    manager._routing = None
    ws, sess = await new_session()
    marker = f"[HANDOFF-{uuid.uuid4().hex[:8]}: files may already be changed; do not repeat finished work]"
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]), prompt="do the thing",
                  engine="claude", status=RunStatus.queued, started_at=utcnow(), resume_attempts=1,
                  resume_handoff=marker + "\n\n" if source in ("column", "both") else None,
                  last_seq=1 if source != "column" else 0)
        db.add(run)
        await db.flush()
        if source != "column":
            db.add(RunEvent(run_id=run.id, seq=1, type="run.waiting",
                            payload={"until": utcnow().isoformat(), "handoff": marker}))
        await db.commit()
        run_id = run.id

    orig = RunManager._attempt
    calls = {"n": 0}

    async def flaky_attempt(self, db, run, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:  # _run 의 첫 커밋 뒤, 엔진 프로세스가 뜨기 전
            raise PoolTimeoutError(POOL_ERROR)
        return await orig(self, db, run, *a, **k)

    monkeypatch.setattr(RunManager, "_attempt", flaky_attempt)
    assert manager.start(run_id)
    for _ in range(300):
        if (await load(run_id)).status in (RunStatus.succeeded, RunStatus.failed):
            break
        await asyncio.sleep(0.1)

    final = await load(run_id)
    prompts = [c["argv"][c["argv"].index("-p") + 1] for c in agent_calls() if "-p" in c["argv"]]
    assert calls["n"] == 2 and final.status == RunStatus.succeeded, (calls, final.status, final.error)
    assert any(marker in p for p in prompts), "resume handoff was lost by the pool-timeout requeue"
    assert final.resume_handoff is None  # 엔진이 뜬 뒤에는 지운다
