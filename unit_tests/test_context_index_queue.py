"""Unit tests for context index queue request preservation on cancellation (DB-free)."""
import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from ttakkari.context import index_queue
from ttakkari.models import CtxSource, utcnow


@pytest.fixture
async def queue_db(monkeypatch):
    """DB-free in-memory SQLite schema for CtxSource testing."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(CtxSource.__table__.create)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(index_queue, "sessionmaker", lambda: sm)
    monkeypatch.setattr(index_queue, "_tasks", set())
    if hasattr(index_queue, "_claimed_requests"):
        monkeypatch.setattr(index_queue, "_claimed_requests", {})
    yield sm
    await engine.dispose()


async def _create_source(sm, *, requested_at=None, started_at=None) -> uuid.UUID:
    sid = uuid.uuid4()
    async with sm() as db:
        src = CtxSource(
            id=sid,
            path=f"/test/{sid}",
            pattern="*.md",
            index_requested_at=requested_at,
            index_started_at=started_at,
        )
        db.add(src)
        await db.commit()
    return sid


async def _get_source(sm, sid: uuid.UUID) -> CtxSource:
    async with sm() as db:
        return await db.get(CtxSource, sid)


async def test_claimed_manual_index_request_is_restored_to_queued_on_cancellation(queue_db, monkeypatch):
    """취소(CancelledError) 시 수동 색인 요청이 원래 요청 시각으로 재대기(queued) 복원되어야 한다."""
    t0 = utcnow() - timedelta(seconds=10)
    sid = await _create_source(queue_db, requested_at=t0)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_index(db, src):
        started.set()
        await release.wait()

    monkeypatch.setattr(index_queue, "index_source", slow_index)

    # 1. pump로 작업을 claim 하고 실행
    assert await index_queue.pump() == 1
    await asyncio.wait_for(started.wait(), 5)

    # 실행 중: index_requested_at은 비워지고 index_started_at이 설정됨
    running = await _get_source(queue_db, sid)
    assert running.index_requested_at is None
    assert running.index_started_at is not None

    # 2. 실행 중 작업 취소
    task = next(iter(index_queue._tasks))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    # 3. 취소 후: index_requested_at이 원래 시각 t0로 복원되고 index_started_at은 해제됨
    after = await _get_source(queue_db, sid)
    assert after.index_requested_at in (t0, t0.replace(tzinfo=None))
    assert after.index_started_at is None
    assert index_queue.is_active(after) is True


async def test_claimed_periodic_index_does_not_create_requested_at_on_cancellation(queue_db, monkeypatch):
    """수동 요청이 아니었던 주기 색인(index_requested_at=None) claim은 취소되어도 index_requested_at을 새로 만들지 않는다."""
    sid = await _create_source(queue_db, requested_at=None)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_index(db, src):
        started.set()
        await release.wait()

    monkeypatch.setattr(index_queue, "index_source", slow_index)

    # index_all 처럼 수동 요청 없이 claim
    assert await index_queue.claim(sid) is True

    claimed = await _get_source(queue_db, sid)
    assert claimed.index_requested_at is None
    assert claimed.index_started_at is not None

    task = asyncio.create_task(index_queue.run_claimed(sid))
    index_queue._tasks.add(task)
    task.add_done_callback(index_queue._tasks.discard)

    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    after = await _get_source(queue_db, sid)
    assert after.index_requested_at is None
    assert after.index_started_at is None
    assert index_queue.is_active(after) is False


async def test_claimed_manual_index_clears_requested_at_on_normal_completion(queue_db, monkeypatch):
    """정상 완료 시에는 index_requested_at이 복원되지 않고 None으로 유지된다."""
    t0 = utcnow()
    sid = await _create_source(queue_db, requested_at=t0)

    async def normal_index(db, src):
        src.last_indexed_at = utcnow()
        await db.commit()

    monkeypatch.setattr(index_queue, "index_source", normal_index)

    assert await index_queue.pump() == 1
    await asyncio.gather(*list(index_queue._tasks), return_exceptions=True)

    after = await _get_source(queue_db, sid)
    assert after.index_requested_at is None
    assert after.index_started_at is None
    assert index_queue.is_active(after) is False


async def test_claimed_manual_index_clears_requested_at_on_normal_exception(queue_db, monkeypatch):
    """일반 예외(Exception)로 실패한 경우 index_requested_at은 복원되지 않는다(last_error 기록)."""
    t0 = utcnow()
    sid = await _create_source(queue_db, requested_at=t0)

    async def failing_index(db, src):
        raise RuntimeError("indexing crashed")

    monkeypatch.setattr(index_queue, "index_source", failing_index)

    assert await index_queue.pump() == 1
    await asyncio.gather(*list(index_queue._tasks), return_exceptions=True)

    after = await _get_source(queue_db, sid)
    assert after.index_requested_at is None
    assert after.index_started_at is None
    assert index_queue.is_active(after) is False


async def test_index_all_restores_manual_index_request_on_cancellation(queue_db, monkeypatch):
    """주기 색인(indexer.index_all)이 수동 요청 출처를 claim한 뒤 취소되면 수동 요청 시각이 복원되어야 한다."""
    t0 = utcnow() - timedelta(seconds=10)
    sid = await _create_source(queue_db, requested_at=t0)

    started = asyncio.Event()
    hang = asyncio.Event()

    async def slow_index(db, src):
        started.set()
        await hang.wait()

    monkeypatch.setattr("ttakkari.context.indexer.index_source", slow_index)

    from ttakkari.context import indexer

    async def run():
        async with queue_db() as db:
            await indexer.index_all(db)

    task = asyncio.create_task(run())
    await asyncio.wait_for(started.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    after = await _get_source(queue_db, sid)
    assert after.index_requested_at in (t0, t0.replace(tzinfo=None))
    assert after.index_started_at is None
    assert index_queue.is_active(after) is True


async def test_claim_compare_and_set_guards_against_concurrent_request_race(queue_db, monkeypatch):
    """claim의 SELECT와 UPDATE 사이에 새 수동 요청이 등록되면 덮어쓰지 않고 claim이 실패하여 새 요청을 보존해야 한다."""
    sid = await _create_source(queue_db, requested_at=None)
    t_new = utcnow()

    real_sm = queue_db
    raced = False

    def wrapping_sm():
        session = real_sm()
        real_execute = session.execute

        async def hooked_execute(statement, *args, **kwargs):
            nonlocal raced
            if not raced and statement.__class__.__name__ == "Update":
                raced = True
                async with real_sm() as db2:
                    src = await db2.get(CtxSource, sid)
                    src.index_requested_at = t_new
                    await db2.commit()
            return await real_execute(statement, *args, **kwargs)

        session.execute = hooked_execute
        return session

    monkeypatch.setattr(index_queue, "sessionmaker", lambda: wrapping_sm)

    claimed = await index_queue.claim(sid)
    assert claimed is False

    after = await _get_source(queue_db, sid)
    assert after.index_requested_at in (t_new, t_new.replace(tzinfo=None))
    assert after.index_started_at is None
    assert index_queue.is_active(after) is True


async def test_pump_claim_cancellation_before_task_spawn_releases_started_marker(queue_db, monkeypatch):
    """claim 직후 task 생성 전에 취소(CancelledError)되면 index_started_at이 남아 stuck 되지 않고 release되어야 한다."""
    t0 = utcnow() - timedelta(seconds=10)
    sid = await _create_source(queue_db, requested_at=t0)

    orig_create_task = asyncio.create_task

    def fail_create_task(coro, *args, **kwargs):
        if getattr(coro, "__name__", None) == "run_claimed":
            coro.close()
            raise asyncio.CancelledError("cancelled before task spawn")
        return orig_create_task(coro, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_task", fail_create_task)

    with pytest.raises(asyncio.CancelledError):
        await index_queue.pump()

    after = await _get_source(queue_db, sid)
    assert after.index_started_at is None
    assert after.index_requested_at in (t0, t0.replace(tzinfo=None))
    assert index_queue.is_active(after) is True
