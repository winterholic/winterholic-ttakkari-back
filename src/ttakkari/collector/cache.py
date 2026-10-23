"""Scope-isolated result cache for scoring calls (CP-S2-05).

The key binds tenant, scope, exact text hash, the immutable model revision, preprocessing, rubric/prompt, policy and
the query/context. Changing any of them is a miss, so one scope or prompt can never read another's result. Negative
and unknown results are cached too, but only with a reason and a TTL; factuality results must be keyed on the
evidence revision (pass it as `context_hash`) so a changed revision invalidates them.
"""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from ttakkari.collector.budget_models import CollectorCacheEntry

TTL = {"positive": timedelta(days=30), "negative": timedelta(days=7), "unknown": timedelta(hours=6)}


def cache_key(*, tenant_id: str, scope: str, text_sha256: str, model_revision: str, preprocess: str,
              rubric: str, policy: str, query_hash: str | None = None, context_hash: str | None = None) -> str:
    parts = {"t": tenant_id, "s": scope, "x": text_sha256, "m": model_revision, "p": preprocess, "r": rubric,
             "l": policy, "q": query_hash, "c": context_hash}
    if not all(parts[k] for k in ("t", "s", "x", "m", "p", "r", "l")):
        raise ValueError("cache key needs tenant, scope, text hash, model revision, preprocess, rubric and policy")
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def get(db, key: str, *, now: datetime | None = None) -> CollectorCacheEntry | None:
    row = await db.get(CollectorCacheEntry, key, with_for_update=True)
    if row is None or row.expires_at <= (now or datetime.now(UTC)):
        return None
    row.hits += 1
    return row


async def put(db, key: str, *, tenant_id: str, scope: str, kind: str, result: dict, reason: str | None = None,
              now: datetime | None = None) -> None:
    if kind not in TTL:
        raise ValueError("kind must be positive, negative or unknown")
    if kind != "positive" and not reason:
        raise ValueError("negative and unknown cache entries need a reason")
    now = now or datetime.now(UTC)
    row = await db.get(CollectorCacheEntry, key, with_for_update=True)
    if row is None:
        db.add(CollectorCacheEntry(cache_key=key, tenant_id=tenant_id, scope=scope, kind=kind, result=result,
                                   reason=reason, hits=0, expires_at=now + TTL[kind]))
    else:
        row.kind, row.result, row.reason, row.expires_at = kind, result, reason, now + TTL[kind]
    await db.flush()


async def purge_expired(db, *, now: datetime | None = None) -> int:
    done = await db.execute(delete(CollectorCacheEntry).where(CollectorCacheEntry.expires_at <= (now or datetime.now(UTC))))
    return done.rowcount or 0
