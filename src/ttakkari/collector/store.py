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

from ttakkari.collector.policy import SourcePolicySpec
from ttakkari.collector.polite import CachedRobots
from ttakkari.collector_models import (
    CollectorDedupeBand,
    CollectorFetchCache,
    CollectorHostState,
    CollectorItem,
    CollectorSourcePolicy,
)
from ttakkari.db import sessionmaker


def url_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


async def _lock(db: AsyncSession, *parts: str) -> None:
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": "\x1f".join(parts)})


# ---- source policy -----------------------------------------------------------------------------------------

async def register_policy(
    db: AsyncSession, tenant_id: str, scope: str, source_key: str, spec: SourcePolicySpec
) -> CollectorSourcePolicy:
    """Append a new immutable policy version. Re-registering identical content returns the current row."""
    await _lock(db, "policy", tenant_id, scope, source_key)
    current = await _latest_policy_row(db, tenant_id, scope, source_key)
    digest = spec.spec_hash()
    if current is not None and current.spec_hash == digest:
        return current
    row = CollectorSourcePolicy(
        tenant_id=tenant_id, scope=scope, source_key=source_key, version=(current.version + 1) if current else 1,
        spec=spec.to_dict(), spec_hash=digest,
    )
    db.add(row)
    await db.flush()
    return row


async def _latest_policy_row(db: AsyncSession, tenant_id: str, scope: str, source_key: str):
    return (
        await db.execute(
            select(CollectorSourcePolicy)
            .where(
                CollectorSourcePolicy.tenant_id == tenant_id,
                CollectorSourcePolicy.scope == scope,
                CollectorSourcePolicy.source_key == source_key,
            )
            .order_by(CollectorSourcePolicy.version.desc())
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

async def get_fetch_cache(db: AsyncSession, tenant_id: str, scope: str, source_key: str, key: str):
    return (
        await db.execute(
            select(CollectorFetchCache).where(
                CollectorFetchCache.tenant_id == tenant_id, CollectorFetchCache.scope == scope,
                CollectorFetchCache.source_key == source_key, CollectorFetchCache.url_key == key,
            )
        )
    ).scalar_one_or_none()


async def put_fetch_cache(
    db: AsyncSession, tenant_id: str, scope: str, source_key: str, url: str, *, etag: str | None,
    last_modified: str | None, raw_sha256: str | None, item_id: uuid.UUID | None, at: datetime,
) -> None:
    stmt = pg_insert(CollectorFetchCache).values(
        id=uuid.uuid4(), tenant_id=tenant_id, scope=scope, source_key=source_key, url_key=url_key(url), url=url,
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
    db: AsyncSession, tenant_id: str, scope: str, source_key: str, key: str, etag: str | None,
    last_modified: str | None, at: datetime,
) -> None:
    row = await get_fetch_cache(db, tenant_id, scope, source_key, key)
    if row is not None:
        row.etag, row.last_modified, row.last_fetched_at = etag or row.etag, last_modified or row.last_modified, at


# ---- items, exact and near duplicates ----------------------------------------------------------------------

async def lock_dedupe(db: AsyncSession, tenant_id: str, scope: str) -> None:
    """Serialize dedupe decisions inside one tenant/scope so two jobs cannot both become the canonical copy."""
    await _lock(db, "dedupe", tenant_id, scope)


async def canonical_by_text(
    db: AsyncSession, tenant_id: str, scope: str, text_sha256: str, *, exclude_url_key: str
) -> CollectorItem | None:
    """The first-seen item with this exact text under a *different* URL. A new revision of the same URL is not a
    duplicate of its own history. Items that are themselves duplicates are never canonical."""
    return (
        await db.execute(
            select(CollectorItem)
            .where(
                CollectorItem.tenant_id == tenant_id, CollectorItem.scope == scope,
                CollectorItem.text_sha256 == text_sha256, CollectorItem.exact_of.is_(None),
                CollectorItem.kind == "document", CollectorItem.url_key != exclude_url_key,
                CollectorItem.envelope_id.is_not(None),
            )
            .order_by(CollectorItem.created_at, CollectorItem.id)
            .limit(1)
        )
    ).scalar_one_or_none()


async def near_candidates(
    db: AsyncSession, tenant_id: str, scope: str, bands: list[str], *, exclude_text_sha256: str, limit: int = 20
) -> list[CollectorItem]:
    if not bands:
        return []
    ids = (
        await db.execute(
            select(CollectorDedupeBand.item_id, func.count().label("hits"))
            .where(
                CollectorDedupeBand.tenant_id == tenant_id, CollectorDedupeBand.scope == scope,
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
        db.add(CollectorDedupeBand(item_id=item_id, band=i, tenant_id=tenant_id, scope=scope, band_hash=h))


async def blob_bytes_used(db: AsyncSession) -> int:
    """Distinct preserved raw bytes across all tenants (identical bytes are stored once)."""
    sub = (
        select(CollectorItem.raw_sha256, func.max(CollectorItem.raw_size).label("size"))
        .where(CollectorItem.raw_handle.like("cblob:%"))
        .group_by(CollectorItem.raw_sha256)
        .subquery()
    )
    return int((await db.execute(select(func.coalesce(func.sum(sub.c.size), 0)))).scalar_one())
