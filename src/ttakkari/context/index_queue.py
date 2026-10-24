"""Durable manual-index requests for context sources (platform v2 Step 6).

The API only records that a source wants indexing; the executor's consumer takes it and runs
``index_source``. The request/claim marks live on ``ctx_sources`` itself, so the existing index
state (``last_indexed_at``/``last_error``) stays the single source of truth for results.
Indexing is idempotent (sha-based), so a claim that is older than ``CLAIM_STALE_SECONDS`` is
treated as left behind by a dead process instead of blocking the source forever.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update

from ttakkari.context.indexer import index_source
from ttakkari.db import sessionmaker
from ttakkari.models import CtxSource, utcnow

log = logging.getLogger(__name__)
CLAIM_STALE_SECONDS = 2 * 3600
_tasks: set[asyncio.Task] = set()
_claimed_requests: dict[uuid.UUID, datetime] = {}


def _stale_before():
    return utcnow() - timedelta(seconds=CLAIM_STALE_SECONDS)


def is_active(src: CtxSource) -> bool:
    """요청이 대기 중이거나 누가 색인 중이다."""
    running = src.index_started_at is not None and src.index_started_at >= _stale_before()
    return src.index_requested_at is not None or running


async def request(source_id: uuid.UUID) -> bool:
    """색인을 요청해 둔다. 이미 대기·실행 중이면 겹쳐 쌓지 않는다."""
    async with sessionmaker()() as db:
        res = await db.execute(update(CtxSource).where(
            CtxSource.id == source_id, CtxSource.index_requested_at.is_(None),
            or_(CtxSource.index_started_at.is_(None), CtxSource.index_started_at < _stale_before()))
            .values(index_requested_at=utcnow()))
        await db.commit()
        return res.rowcount == 1


async def claim(source_id: uuid.UUID) -> bool:
    """색인 실행권을 한 곳만 갖는다. 소비자·직접 실행·주기 색인이 같은 출처를 겹쳐 돌리지 않게 한다."""
    async with sessionmaker()() as db:
        src = await db.scalar(select(CtxSource).where(
            CtxSource.id == source_id,
            or_(CtxSource.index_started_at.is_(None), CtxSource.index_started_at < _stale_before())))
        if src is None:
            return False
        req = src.index_requested_at
        prev_started = src.index_started_at
        started_cond = CtxSource.index_started_at.is_(None) if prev_started is None else (CtxSource.index_started_at == prev_started)
        req_cond = CtxSource.index_requested_at.is_(None) if req is None else (CtxSource.index_requested_at == req)
        res = await db.execute(update(CtxSource).where(
            CtxSource.id == source_id, started_cond, req_cond)
            .values(index_requested_at=None, index_started_at=utcnow()))
        if res.rowcount != 1:
            return False
        await db.commit()
        if req is not None:
            _claimed_requests[source_id] = req
        else:
            _claimed_requests.pop(source_id, None)
        return True


async def release(source_id: uuid.UUID, *, cancelled: bool = False, requested_at: datetime | None = None) -> None:
    cached_req = _claimed_requests.pop(source_id, None)
    values: dict[str, Any] = {"index_started_at": None}
    restore_req = (requested_at or cached_req) if cancelled else None
    if restore_req is not None:
        values["index_requested_at"] = restore_req
    async with sessionmaker()() as db:
        await db.execute(update(CtxSource).where(CtxSource.id == source_id).values(**values))
        await db.commit()


async def run_claimed(source_id: uuid.UUID, requested_at: datetime | None = None) -> None:
    """claim 한 출처를 색인하고 실행권을 돌려준다. 실패는 index_source 가 last_error 에 남긴다."""
    cancelled = False
    try:
        async with sessionmaker()() as db:
            src = await db.get(CtxSource, source_id)
            if src is not None:
                await index_source(db, src)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception:
        log.exception("indexing failed")
    finally:
        await asyncio.shield(release(source_id, cancelled=cancelled, requested_at=requested_at))


def inflight() -> int:
    return sum(not t.done() for t in _tasks)


async def pump() -> int:
    """실행부 소비자 한 번의 순회. 색인은 한 번에 한 출처만 순차로 돌린다."""
    if inflight():
        return 0
    async with sessionmaker()() as db:
        row = (await db.execute(select(CtxSource.id, CtxSource.index_requested_at).where(
            CtxSource.index_requested_at.is_not(None),
            or_(CtxSource.index_started_at.is_(None), CtxSource.index_started_at < _stale_before()))
            .order_by(CtxSource.index_requested_at).limit(1))).first()
    if row is None:
        return 0
    sid, req_at = row
    if not await claim(sid):
        return 0
    started = False
    try:
        task = asyncio.create_task(run_claimed(sid, requested_at=req_at))
        _tasks.add(task)
        task.add_done_callback(_tasks.discard)
        started = True
        return 1
    finally:
        if not started:
            await asyncio.shield(release(sid, cancelled=True, requested_at=req_at))
