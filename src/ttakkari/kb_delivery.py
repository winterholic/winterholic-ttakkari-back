"""Idempotent export receipts and the optional KB adapter, in the same DB transaction.

Raw bytes remain pinned by KbSource in its own content-addressed store. No collector-owned
file can disappear underneath a citation. A delivery receipt is not a quality/factuality approval.
"""
import asyncio
import hashlib
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from ttakkari.collector import store
from ttakkari.collector.blobs import default_blob_store
from ttakkari.collector.contracts import (
    compute_payload_hash,
    parse_iso_datetime,
    validate_envelope,
)
from ttakkari.collector.control_models import (
    CollectorDelivery,
    CollectorReview,
    CollectorSource,
    KbDocumentAccess,
    KbObservation,
)
from ttakkari.collector.library import (
    LEGACY_SCOPES,
    LIBRARY_SCOPE,
    collector_doc_keys,
    effective_pool,
    in_library,
    narrowest_pool,
)
from ttakkari.collector_models import CollectorItem, CollectorJob, CollectorOutbox
from ttakkari.db import sessionmaker
from ttakkari.kb_library import resolve_doc_key
from ttakkari.kb_store import ingest_source, segment_revision

log = logging.getLogger(__name__)


async def deliver_item(db, outbox, item):
    env = validate_envelope(item.envelope)
    if compute_payload_hash(env) != outbox.payload_hash or item.payload_hash != outbox.payload_hash:
        raise ValueError("delivery_payload_hash_mismatch")
    access = env["access"]
    # C2: scope 는 이름일 뿐이다. 서로 다른 옛 이름(envelope 의 work, 병합된 outbox 의 personal)을 불일치로 보지 않는다.
    if access["tenant_id"] != outbox.tenant_id or access["scope"] not in LEGACY_SCOPES or outbox.scope not in LEGACY_SCOPES:
        raise ValueError("delivery_scope_mismatch")
    if access["principal_ids"] != ["owner"] or outbox.tenant_id != "owner" or outbox.scope not in ("personal", "work"):
        raise ValueError("delivery_principal_unsupported")
    # Policy replacement and a delivery commit serialize on the same lock. A revocation then hides
    # all previously committed documents before its own transaction completes.
    await store.policy_lock(db, item.tenant_id, item.source_key)
    active = await store.active_policy(db, item.tenant_id, item.scope, item.source_key)
    rights = env["rights"]
    now = datetime.now(UTC)
    if active is None or not active[0].allows("store") or rights["permission"] != "allowed":
        raise ValueError("delivery_store_permission_missing")
    if rights["retain_until"] and parse_iso_datetime(rights["retain_until"]) <= now:
        raise ValueError("delivery_rights_expired")
    # Export is first: receipts describe the actual permitted outcome instead of calling a queue a delivery.
    source = await db.scalar(select(CollectorSource).where(
        CollectorSource.tenant_id == item.tenant_id, in_library(CollectorSource.scope),
        CollectorSource.source_key == item.source_key,
    ).order_by(CollectorSource.created_at).limit(1))
    review = await db.scalar(select(CollectorReview).where(CollectorReview.item_id == item.id).order_by(
        CollectorReview.created_at.desc(), CollectorReview.id.desc()).limit(1))
    manual = outbox.sink == "knowledge" and review is not None and review.decision == "accept" and review.text_sha256 == item.text_sha256
    can_import = (source is not None and (source.auto_import or manual) and active[0].allows("knowledge")
                  and "knowledge" in rights["purposes"] and (env["decision"]["state"] == "accept" or manual))
    if rights["retain_until"] is not None and can_import:
        # Immutable originals cannot yet honor a future deletion deadline; do not create an irreversible pin.
        raise ValueError("kb_import_requires_unlimited_retention_policy")
    if not can_import:
        if not active[0].allows("export") or "export" not in rights["purposes"]:
            raise ValueError("delivery_no_permitted_sink")
        return None, "exported", {"knowledge": "not_permitted_or_disabled"}
    normalized = env["normalized"]
    if normalized is None or normalized["truncated"]:
        raise ValueError("delivery_normalized_missing_or_truncated")
    raw = await asyncio.to_thread(default_blob_store().get, env["raw"]["blob_handle"])
    if len(raw) != env["raw"]["byte_size"] or hashlib.sha256(raw).hexdigest() != env["raw"]["sha256"]:
        raise ValueError("delivery_raw_mismatch")
    key = await resolve_doc_key(db, *collector_doc_keys(
        item.tenant_id, item.source_key, env["source"]["url"] or item.url))
    job = await db.get(CollectorJob, item.job_id)
    pool = effective_pool(job.pool if job else None, item.scope)
    collected = parse_iso_datetime(env["source"]["collected_at"])
    result = await ingest_source(
        db, source_key=key, data=raw, kind="web", locator=env["source"]["url"],
        media_type=env["raw"]["media_type"],
        title=((env.get("extensions") or {}).get("collector:page") or {}).get("title") or source.title, lang=normalized["language"],
        parser_version=normalized["parser_version"][:60], collected_at=collected,
        normalized_body=normalized["text"],
        origin_meta={"via": "collector", "tenant_id": item.tenant_id, "pool": pool,
                     "item_id": str(item.id), "raw_handle": env["raw"]["blob_handle"], "rights": rights,
                     "collector_source_key": item.source_key,
                     "mission": (env.get("extensions") or {}).get("collector:mission"),
                     "normalizer_version": normalized["normalizer_version"],
                     "raw_alignment_handle": normalized["raw_alignment_handle"]},
    )
    access_row = await db.get(KbDocumentAccess, result.document_id)
    if access_row is None:
        db.add(KbDocumentAccess(document_id=result.document_id, tenant_id=item.tenant_id, scope=LIBRARY_SCOPE, pool=pool))
    elif access_row.tenant_id != item.tenant_id:
        raise ValueError("delivery_existing_scope_mismatch")
    else:
        access_row.hidden = False
        # 같은 출처에 개인·회사 쪽 수집이 함께 닿으면 접근 범위는 좁은 쪽이다(회사 엔진이 개인 수집분을 새로 읽지 못한다).
        access_row.pool = narrowest_pool(effective_pool(access_row.pool, access_row.scope), pool)
    await segment_revision(db, result.revision_id)
    observed = await db.scalar(select(KbObservation.id).where(KbObservation.envelope_id == outbox.envelope_id))
    if observed is None:
        db.add(KbObservation(document_id=result.document_id, revision_id=result.revision_id,
                         envelope_id=outbox.envelope_id, event_time=collected,
                         provenance={"item_id": str(item.id), "job_id": str(item.job_id), "rights": rights,
                                     "review_id": str(review.id) if manual else None}))
    return result.revision_id, "imported", {"document_id": str(result.document_id), "created": result.created}


MAX_ATTEMPTS = 5


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(3600, 30 * 2 ** max(0, attempts - 1)))


async def deliver_once(*, limit: int = 20) -> int:
    count = 0
    now = datetime.now(UTC)
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(CollectorOutbox).where(
            CollectorOutbox.sink.in_(("export", "knowledge")), CollectorOutbox.status == "pending",
            or_(CollectorOutbox.not_before.is_(None), CollectorOutbox.not_before <= now),
        ).order_by(CollectorOutbox.created_at).limit(limit).with_for_update(skip_locked=True)))
        for out in rows:
            item = await db.scalar(select(CollectorItem).where(
                CollectorItem.envelope_id == out.envelope_id, CollectorItem.job_id == out.job_id,
                CollectorItem.tenant_id == out.tenant_id, in_library(CollectorItem.scope),
                CollectorItem.status.in_(("accepted", "held")),
            ))
            if item is None:
                out.status, out.last_error = "failed", "item_missing"
                log.error("collector delivery item missing: outbox=%s", out.id)
                continue
            try:
                async with db.begin_nested():
                    revision_id, outcome, detail = await deliver_item(db, out, item)
            except (OSError, SQLAlchemyError) as exc:
                # The savepoint rolled everything back, so the outcome is known: nothing was written. Retry later,
                # and stop after MAX_ATTEMPTS so a broken blob store is visible instead of spinning.
                out.attempts += 1
                out.last_error = f"transient:{type(exc).__name__}"[:300]
                if out.attempts >= MAX_ATTEMPTS:
                    out.status = "failed"
                else:
                    out.not_before = now + _backoff(out.attempts)
                log.warning("collector delivery transient failure: outbox=%s attempts=%s", out.id, out.attempts)
                count += 1
                continue
            except ValueError as exc:
                revision_id, outcome, detail = None, "failed", {"reason": str(exc)[:300]}
                log.warning("collector delivery failed: item=%s reason=%s", item.id, detail["reason"])
            db.add(CollectorDelivery(outbox_id=out.id, item_id=item.id, revision_id=revision_id,
                                     outcome=outcome, detail=detail))
            out.status = "failed" if outcome == "failed" else "delivered"
            out.last_error = detail.get("reason") if outcome == "failed" else None
            out.delivered_at = datetime.now(UTC) if outcome != "failed" else None
            count += 1
        await db.commit()
    return count


async def reconcile(db, out: CollectorOutbox) -> dict:
    """Owner-requested reconciliation. Never overwrites a recorded outcome; it re-checks it or re-opens a delivery
    that has no receipt (retry budget exhausted). A rights/hash/scope failure is terminal until the source is re-run."""
    receipt = await db.get(CollectorDelivery, out.id)
    if out.status == "delivered":
        if receipt is None:
            return {"state": "inconsistent", "detail": "delivered_without_receipt"}
        if receipt.outcome == "imported":
            seen = await db.scalar(select(KbObservation.id).where(KbObservation.envelope_id == out.envelope_id))
            if seen is None:
                return {"state": "inconsistent", "detail": "imported_without_observation"}
        return {"state": "consistent", "outcome": receipt.outcome}
    if out.status == "pending":
        return {"state": "pending", "attempts": out.attempts}
    if receipt is not None:
        return {"state": "terminal", "outcome": receipt.outcome, "detail": receipt.detail}
    out.status, out.attempts, out.not_before, out.last_error = "pending", 0, None, None
    return {"state": "reopened"}
