"""Collector Platform: database access for policies, host state, fetch cache, items and dedupe candidates.

Functions that take an AsyncSession never commit. DbHostState opens its own short transactions because robots/cooldown
state is public and shared across tenants and must not ride on a job's page transaction.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.library import (
    LIBRARY_SCOPE,
    WORK_POOL,
    in_library,
    job_visible,
    normalize_pool,
)
from ttakkari.collector.policy import SourcePolicySpec
from ttakkari.collector.polite import CachedRobots
from ttakkari.collector_models import (
    CollectorDedupeBand,
    CollectorFetchCache,
    CollectorHostState,
    CollectorItem,
    CollectorJob,
    CollectorSourcePolicy,
)
from ttakkari.db import sessionmaker


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


async def _lock(db: AsyncSession, *parts: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": "\x1f".join(parts)})


# ---- source policy -----------------------------------------------------------------------------------------

async def policy_lock(db: AsyncSession, tenant_id: str, source_key: str) -> None:
    """정책 교체와 delivery commit 이 같은 잠금을 쓴다. 도서관이 하나이므로 키에 scope 가 없다."""
    await _lock(db, "policy", tenant_id, source_key)


async def register_policy(
    db: AsyncSession, tenant_id: str, scope: str, source_key: str, spec: SourcePolicySpec
) -> CollectorSourcePolicy:
    """Append a new immutable policy version. Re-registering identical content returns the current row.

    C2: `scope` 인자는 옛 호출부 호환용이며 무시한다. 정책은 출처(source_key)의 권리라 도서관 전체에 하나뿐이다.
    새 행은 단일 scope 로 쓰고, 버전은 옛 scope 이력까지 포함한 최대값 다음 번호라 기존 행·policy_version 라벨을 바꾸지 않는다.
    """
    await policy_lock(db, tenant_id, source_key)
    current = await _latest_policy_row(db, tenant_id, scope, source_key)
    digest = spec.spec_hash()
    if current is not None and current.spec_hash == digest:
        return current
    top = await db.scalar(
        select(func.max(CollectorSourcePolicy.version)).where(
            CollectorSourcePolicy.tenant_id == tenant_id, in_library(CollectorSourcePolicy.scope),
            CollectorSourcePolicy.source_key == source_key,
        )
    )
    row = CollectorSourcePolicy(
        tenant_id=tenant_id, scope=LIBRARY_SCOPE, source_key=source_key, version=(top or 0) + 1,
        spec=spec.to_dict(), spec_hash=digest,
    )
    db.add(row)
    await db.flush()
    return row


async def _latest_policy_row(db: AsyncSession, tenant_id: str, scope: str, source_key: str):
    """옛 scope 이력을 포함해 가장 늦게 기록한 정책이 현재 정책이다(scope 인자는 무시)."""
    return (
        await db.execute(
            select(CollectorSourcePolicy)
            .where(
                CollectorSourcePolicy.tenant_id == tenant_id,
                in_library(CollectorSourcePolicy.scope),
                CollectorSourcePolicy.source_key == source_key,
            )
            .order_by(CollectorSourcePolicy.created_at.desc(), CollectorSourcePolicy.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def active_policy(
    db: AsyncSession, tenant_id: str, scope: str, source_key: str
) -> tuple[SourcePolicySpec, int, str] | None:
    row = await _latest_policy_row(db, tenant_id, scope, source_key)
    if row is None:
        return None
    return SourcePolicySpec.from_dict(row.spec), row.version, row.spec_hash


# ---- host state (robots cache, cooldown, pacing) -----------------------------------------------------------

class DbHostState:
    async def get_robots(self, origin: str) -> CachedRobots | None:
        async with sessionmaker()() as db:
            row = await db.get(CollectorHostState, origin)
            if row is None or row.robots_status is None or row.robots_fetched_at is None or row.robots_expires_at is None:
                return None
            return CachedRobots(row.robots_status, row.robots_body, row.robots_fetched_at, row.robots_expires_at)

    async def put_robots(self, origin: str, cached: CachedRobots) -> None:
        async with sessionmaker()() as db:
            await _ensure_host(db, origin)
            row = await db.get(CollectorHostState, origin, with_for_update=True)
            row.robots_status, row.robots_body = cached.status, cached.body
            row.robots_fetched_at, row.robots_expires_at = cached.fetched_at, cached.expires_at
            await db.commit()

    async def get_cooldown(self, origin: str) -> datetime | None:
        async with sessionmaker()() as db:
            row = await db.get(CollectorHostState, origin)
            return row.cooldown_until if row else None

    async def set_cooldown(self, origin: str, until: datetime, reason: str) -> None:
        async with sessionmaker()() as db:
            await _ensure_host(db, origin)
            row = await db.get(CollectorHostState, origin, with_for_update=True)
            if row.cooldown_until is None or until > row.cooldown_until:
                row.cooldown_until, row.cooldown_reason = until, reason
            await db.commit()

    async def reserve_slot(self, origin: str, interval: float, now: datetime) -> float:
        async with sessionmaker()() as db:
            await _ensure_host(db, origin)
            row = await db.get(CollectorHostState, origin, with_for_update=True)
            slot = max(now, row.next_allowed_at or now)
            row.next_allowed_at = slot + timedelta(seconds=interval)
            await db.commit()
            return (slot - now).total_seconds()


async def _ensure_host(db: AsyncSession, origin: str) -> None:
    await db.execute(pg_insert(CollectorHostState).values(origin=origin, updated_at=datetime.now(UTC)).on_conflict_do_nothing())


# ---- fetch cache -------------------------------------------------------------------------------------------

async def get_fetch_cache(db: AsyncSession, tenant_id: str, pool: str, source_key: str, key: str):
    """조건부 요청 검증값. 도서관 전체가 공유하되, 회사 엔진 pool 의 job 은 자기 pool 이 읽을 수 없는 job 이 남긴 검증값을
    쓰지 않는다. 쓰면 304(not_modified)가 되어 회사 엔진이 읽을 사본이 생기지 않는다."""
    row = (
        await db.execute(
            select(CollectorFetchCache).where(
                CollectorFetchCache.tenant_id == tenant_id, in_library(CollectorFetchCache.scope),
                CollectorFetchCache.source_key == source_key, CollectorFetchCache.url_key == key,
            ).order_by(CollectorFetchCache.last_fetched_at.desc()).limit(1)
        )
    ).scalar_one_or_none()
    if row is not None and row.last_item_id is not None and normalize_pool(pool) == WORK_POOL:
        visible = await db.scalar(
            select(CollectorItem.id).join(CollectorJob, CollectorJob.id == CollectorItem.job_id)
            .where(CollectorItem.id == row.last_item_id, job_visible(pool))
        )
        if visible is None:
            return None
    return row


async def put_fetch_cache(
    db: AsyncSession, tenant_id: str, scope: str, source_key: str, url: str, *, etag: str | None,
    last_modified: str | None, raw_sha256: str | None, item_id: uuid.UUID | None, at: datetime,
) -> None:
    stmt = pg_insert(CollectorFetchCache).values(
        id=uuid.uuid4(), tenant_id=tenant_id, scope=LIBRARY_SCOPE, source_key=source_key, url_key=url_key(url), url=url,
        etag=etag, last_modified=last_modified, last_raw_sha256=raw_sha256, last_item_id=item_id, last_fetched_at=at,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            constraint="uq_collector_fetch_cache_url",
            set_={
                "url": url, "etag": etag, "last_modified": last_modified, "last_raw_sha256": raw_sha256,
                "last_item_id": item_id, "last_fetched_at": at,
            },
        )
    )


async def touch_fetch_cache(
    db: AsyncSession, tenant_id: str, pool: str, source_key: str, key: str, etag: str | None,
    last_modified: str | None, at: datetime,
) -> None:
    row = await get_fetch_cache(db, tenant_id, pool, source_key, key)
    if row is not None:
        row.etag, row.last_modified, row.last_fetched_at = etag or row.etag, last_modified or row.last_modified, at


# ---- items, exact and near duplicates ----------------------------------------------------------------------

async def lock_dedupe(db: AsyncSession, tenant_id: str, scope: str) -> None:
    """Serialize dedupe decisions inside one tenant's library so two jobs cannot both become the canonical copy."""
    await _lock(db, "dedupe", tenant_id)


async def canonical_by_text(
    db: AsyncSession, tenant_id: str, pool: str, text_sha256: str, *, exclude_url_key: str
) -> CollectorItem | None:
    """The first-seen item with this exact text under a *different* URL. A new revision of the same URL is not a
    duplicate of its own history. Items that are themselves duplicates are never canonical.

    C2: 도서관 전체에서 찾되, 회사 엔진 pool 의 job 은 자기 pool 이 읽을 수 있는 item 만 대표로 삼는다.
    그렇지 않으면 개인 자료와 같은 본문이라는 이유로 회사 엔진이 읽을 사본이 생기지 않는다."""
    return (
        await db.execute(
            select(CollectorItem)
            .join(CollectorJob, CollectorJob.id == CollectorItem.job_id)
            .where(
                CollectorItem.tenant_id == tenant_id, in_library(CollectorItem.scope), job_visible(pool),
                CollectorItem.text_sha256 == text_sha256, CollectorItem.exact_of.is_(None),
                CollectorItem.kind == "document", CollectorItem.url_key != exclude_url_key,
                CollectorItem.envelope_id.is_not(None),
            )
            .order_by(CollectorItem.created_at, CollectorItem.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def near_candidates(
    db: AsyncSession, tenant_id: str, pool: str, bands: list[str], *, exclude_text_sha256: str, limit: int = 20
) -> list[CollectorItem]:
    if not bands:
        return []
    ids = (
        await db.execute(
            select(CollectorDedupeBand.item_id, func.count().label("hits"))
            .join(CollectorItem, CollectorItem.id == CollectorDedupeBand.item_id)
            .join(CollectorJob, CollectorJob.id == CollectorItem.job_id)
            .where(
                CollectorDedupeBand.tenant_id == tenant_id, in_library(CollectorDedupeBand.scope), job_visible(pool),
                tuple_(CollectorDedupeBand.band, CollectorDedupeBand.band_hash).in_(list(enumerate(bands))),
            )
            .group_by(CollectorDedupeBand.item_id)
            .order_by(func.count().desc())
            .limit(limit * 3)
        )
    ).all()
    if not ids:
        return []
    items = (
        await db.scalars(
            select(CollectorItem).where(
                CollectorItem.id.in_([i for i, _ in ids]), CollectorItem.text_sha256 != exclude_text_sha256,
                CollectorItem.minhash.is_not(None),
            )
        )
    ).all()
    return list(items)[:limit]


def add_bands(db: AsyncSession, item_id: uuid.UUID, tenant_id: str, scope: str, bands: list[str]) -> None:
    for i, h in enumerate(bands):
        db.add(CollectorDedupeBand(item_id=item_id, band=i, tenant_id=tenant_id, scope=LIBRARY_SCOPE, band_hash=h))


async def blob_bytes_used(db: AsyncSession) -> int:
    """Distinct DB-referenced raw bytes; this is accounting, not physical blob-root usage."""
    sub = (
        select(CollectorItem.raw_sha256, func.max(CollectorItem.raw_size).label("size"))
        .where(CollectorItem.raw_handle.like("cblob:%"))
        .group_by(CollectorItem.raw_sha256)
        .subquery()
    )
    return int((await db.execute(select(func.coalesce(func.sum(sub.c.size), 0)))).scalar_one())
