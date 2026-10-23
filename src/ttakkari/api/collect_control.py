"""Scoped source management, collection requests and inspectable delivery results."""
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import String, func, select, update

from ttakkari.api.collect import _view, user_access
from ttakkari.api.deps import Db
from ttakkari.collector import control, jobs, quality_service, store
from ttakkari.collector.contracts import (
    AuthContext,
    parse_iso_datetime,
)
from ttakkari.collector.control_models import (
    CollectorDailyBudget,
    CollectorDelivery,
    CollectorReview,
    CollectorSource,
    KbDocumentAccess,
)
from ttakkari.collector.netguard import normalize_url
from ttakkari.collector.policy import CollectRequest, SourcePolicySpec
from ttakkari.collector_models import CollectorItem, CollectorJob, CollectorOutbox
from ttakkari.config import get_settings
from ttakkari.kb_models import KbRevision, KbSource

router = APIRouter(prefix="/api/assistant/collect", tags=["collect"])
# The default sink row is the intake registration of the job itself, never a delivery.
DELIVERY_SINKS = ("export", "knowledge")
Access = Annotated[AuthContext, Depends(user_access)]


def valid_strings(value):
    if isinstance(value, str):
        if "\x00" in value:
            raise ValueError("unsupported_nul")
        value.encode("utf-8")
    elif isinstance(value, dict):
        for key, item in value.items():
            valid_strings(key)
            valid_strings(item)
    elif isinstance(value, list):
        for item in value:
            valid_strings(item)
    return value


class SourceIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_invalid_strings(cls, value):
        return valid_strings(value)
    source_key: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=500)
    seeds: list[str] = Field(min_length=1, max_length=50)
    policy: dict[str, Any]
    interval_minutes: int | None = Field(default=None, ge=15, le=525600)
    max_pages: int = Field(default=10, ge=1, le=500)
    max_depth: int = Field(default=0, ge=0, le=3)
    profile: str = "general"
    auto_import: bool = True

    @field_validator("source_key", "title")
    @classmethod
    def non_blank(cls, value):
        if not value.strip() or "\x00" in value:
            raise ValueError("blank or NUL text")
        return value.strip()


class SourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_invalid_strings(cls, value):
        return valid_strings(value)
    enabled: bool | None = None
    auto_import: bool | None = None
    expected_version: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    seeds: list[str] | None = Field(default=None, min_length=1, max_length=50)
    policy: dict[str, Any] | None = None
    interval_minutes: int | None = Field(default=None, ge=15, le=525600)
    max_pages: int | None = Field(default=None, ge=1, le=500)


class ReviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*", mode="before")
    @classmethod
    def reject_invalid_strings(cls, value):
        return valid_strings(value)
    decision: Literal["accept", "reject", "hold"]
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/items/{item_id}/reviews", status_code=201)
async def review_item(item_id: uuid.UUID, body: ReviewIn, ctx: Access, db: Db):
    item = await db.get(CollectorItem, item_id)
    if item is None or item.tenant_id != ctx.tenant_id or item.scope != ctx.scope:
        raise HTTPException(404, "item_not_found")
    if not item.text_sha256 or not body.reason.strip():
        raise HTTPException(422, "review_requires_text_and_reason")
    row = CollectorReview(item_id=item.id, decision=body.decision, reason=body.reason.strip(),
                          text_sha256=item.text_sha256, actor="owner")
    db.add(row)
    await db.commit()
    return {"id": str(row.id), "decision": row.decision, "actor": row.actor,
            "meaning": "document_utility_review_only", "factuality": "unverified"}


@router.get("/quality")
async def quality_status(ctx: Access, db: Db):
    reviews = await quality_service.reviewed_items(db, ctx.tenant_id, ctx.scope)
    model = await quality_service.latest_model(db, ctx.tenant_id, ctx.scope)
    return {"reviewed_items": len(reviews), "accepted": sum(r.decision == "accept" for r, _ in reviews),
            "rejected": sum(r.decision == "reject" for r, _ in reviews),
            "model": {"digest": model.digest, "status": "shadow", "quality_gate": "unverified",
                      "metrics": model.artifact["metrics"]} if model else None,
            "training_requires": {"minimum_examples": 20, "minimum_source_groups": 5,
                                  "permissions": "train allowed at collection and now", "labels": "both classes in train and validation"},
            "factuality": "unmeasured", "teacher_enabled": False}


@router.post("/quality/train", status_code=201)
async def train_quality(ctx: Access, db: Db):
    await store._lock(db, "quality-train", ctx.tenant_id, ctx.scope)
    try:
        model = await quality_service.train(db, ctx.tenant_id, ctx.scope)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await db.commit()
    quality_service.install(model)
    return {"digest": model.digest, "status": "shadow", "quality_gate": "unverified", "metrics": model.artifact["metrics"]}


def view(source: CollectorSource) -> dict:
    return {
        "id": str(source.id), "source_key": source.source_key, "title": source.title,
        "scope": source.scope, "seeds": source.request["seeds"], "request": source.request,
        "enabled": source.enabled, "auto_import": source.auto_import,
        "interval_minutes": source.interval_minutes,
        "next_run_at": source.next_run_at.isoformat() if source.next_run_at else None,
        "last_job_id": str(source.last_job_id) if source.last_job_id else None,
        "version": source.version,
        "last_error": source.last_error,
    }


async def find_source(db, ctx, source_id):
    row = await db.scalar(select(CollectorSource).where(
        CollectorSource.id == source_id, CollectorSource.tenant_id == ctx.tenant_id,
        CollectorSource.scope == ctx.scope,
    ).with_for_update())
    if row is None:
        raise HTTPException(404, "source_not_found")
    return row


@router.get("/sources")
async def sources(ctx: Access, db: Db):
    rows = await db.scalars(select(CollectorSource).where(
        CollectorSource.tenant_id == ctx.tenant_id, CollectorSource.scope == ctx.scope,
    ).order_by(CollectorSource.created_at.desc()).limit(200))
    items = []
    for source in rows:
        active = await store.active_policy(db, ctx.tenant_id, ctx.scope, source.source_key)
        items.append({**view(source), "policy": active[0].to_dict() if active else None})
    return {"items": items, "runtime_enabled": get_settings().collector_control_enabled,
            "daily_page_cap": get_settings().collector_daily_page_cap, "ml_spending_enabled": False}


@router.post("/sources", status_code=201)
async def create_source(body: SourceIn, ctx: Access, db: Db):
    try:
        row, spec = await control.register_source(
            db, ctx, source_key=body.source_key, title=body.title, seeds=body.seeds, policy=body.policy,
            interval_minutes=body.interval_minutes, max_pages=body.max_pages, max_depth=body.max_depth,
            profile=body.profile, auto_import=body.auto_import)
    except control.SourceKeyExists as exc:
        raise HTTPException(409, "source_key_exists") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await db.commit()
    return {**view(row), "policy": spec.to_dict()}


@router.patch("/sources/{source_id}")
async def update_source(source_id: uuid.UUID, body: SourceUpdate, ctx: Access, db: Db):
    row = await find_source(db, ctx, source_id)
    if body.expected_version is not None and row.version != body.expected_version:
        raise HTTPException(409, "source_version_conflict")
    request = dict(row.request)
    if body.seeds is not None:
        request["seeds"] = body.seeds
    if body.max_pages is not None:
        request["max_pages"] = body.max_pages
    active = await store.active_policy(db, ctx.tenant_id, ctx.scope, row.source_key)
    try:
        spec = SourcePolicySpec.from_dict(body.policy) if body.policy is not None else active[0]
        CollectRequest.from_envelope({"extensions": {"collector:request": request}}, spec)
        for url in request["seeds"]:
            parsed = normalize_url(url, allowed_schemes=spec.schemes, allowed_ports=spec.ports)
            if reason := spec.check_url(parsed):
                raise ValueError(reason)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if body.policy is not None:
        await store.register_policy(db, ctx.tenant_id, ctx.scope, row.source_key, spec)
        if not spec.allows("store") or not spec.allows("knowledge") or spec.retain_days is not None:
            doc_ids = select(KbRevision.document_id).join(KbSource).where(
                KbSource.origin_meta["collector_source_key"].astext == row.source_key,
                KbSource.origin_meta["tenant_id"].astext == ctx.tenant_id,
                KbSource.origin_meta["scope"].astext == ctx.scope)
            await db.execute(update(KbDocumentAccess).where(KbDocumentAccess.document_id.in_(doc_ids)).values(hidden=True))
    row.request = request
    if body.title is not None:
        if not body.title.strip() or "\x00" in body.title:
            raise HTTPException(422, "invalid_source_title")
        row.title = body.title.strip()
    if "interval_minutes" in body.model_fields_set:
        row.interval_minutes = body.interval_minutes
        row.next_run_at = datetime.now(UTC) if body.interval_minutes else None
    for name in ("enabled", "auto_import"):
        if (value := getattr(body, name)) is not None:
            setattr(row, name, value)
    row.version += 1
    if not spec.allows("store"):
        row.enabled = False
    await db.commit()
    return {**view(row), "policy": spec.to_dict()}


@router.post("/sources/{source_id}/run", status_code=202)
async def run_source(source_id: uuid.UUID, ctx: Access, db: Db):
    row = await find_source(db, ctx, source_id)
    job = await control.enqueue(db, row)
    await db.commit()
    return _view(job)


@router.post("/jobs/{job_id}/retry")
async def retry(job_id: uuid.UUID, ctx: Access, db: Db):
    job = await control.retry_job(db, ctx, job_id)
    await db.commit()
    return _view(job)


@router.get("/jobs/{job_id}/items")
async def job_items(job_id: uuid.UUID, ctx: Access, db: Db,
                    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    await jobs.get_job(db, ctx.tenant_id, ctx.scope, job_id)
    base = select(CollectorItem).where(CollectorItem.job_id == job_id)
    count = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = list(await db.scalars(base.order_by(CollectorItem.created_at, CollectorItem.id).limit(limit).offset(offset)))
    receipts = {r.item_id: r for r in await db.scalars(select(CollectorDelivery).where(
        CollectorDelivery.item_id.in_([r.id for r in rows]))) }
    return {"items": [{"id": str(r.id), "url": r.url, "status": r.status, "reason_codes": r.reason_codes,
                       "language": r.language, "text_chars": r.text_chars, "signals": r.signals,
                       "preview": ((r.envelope or {}).get("normalized") or {}).get("text", "")[:1200],
                       "delivery": {"outcome": receipts[r.id].outcome,
                                    "revision_id": str(receipts[r.id].revision_id) if receipts[r.id].revision_id else None}
                       if r.id in receipts else None} for r in rows],
            "total": count, "limit": limit, "offset": offset}


@router.get("/items/{item_id}/export")
async def export_item(item_id: uuid.UUID, ctx: Access, db: Db):
    item = await db.get(CollectorItem, item_id)
    if item is None or item.tenant_id != ctx.tenant_id or item.scope != ctx.scope:
        raise HTTPException(404, "item_not_found")
    if not item.envelope or "export" not in item.envelope["rights"]["purposes"]:
        raise HTTPException(403, "export_not_permitted")
    active = await store.active_policy(db, ctx.tenant_id, ctx.scope, item.source_key)
    until = item.envelope["rights"]["retain_until"]
    if active is None or not active[0].allows("export") or (until and parse_iso_datetime(until) <= datetime.now(UTC)):
        raise HTTPException(403, "export_permission_revoked_or_expired")
    return item.envelope


@router.post("/items/{item_id}/import", status_code=202)
async def import_reviewed_item(item_id: uuid.UUID, ctx: Access, db: Db):
    item = await db.get(CollectorItem, item_id, with_for_update=True)
    if item is None or item.tenant_id != ctx.tenant_id or item.scope != ctx.scope:
        raise HTTPException(404, "item_not_found")
    review = await db.scalar(select(CollectorReview).where(CollectorReview.item_id == item.id).order_by(
        CollectorReview.created_at.desc(), CollectorReview.id.desc()).limit(1))
    if not review or review.decision != "accept" or review.text_sha256 != item.text_sha256:
        raise HTTPException(409, "utility_review_required")
    if not item.envelope_id or item.status not in ("accepted", "held"):
        raise HTTPException(422, "item_not_importable")
    active = await store.active_policy(db, ctx.tenant_id, ctx.scope, item.source_key)
    rights = item.envelope["rights"]
    if active is None or not active[0].allows("knowledge") or "knowledge" not in rights["purposes"]:
        raise HTTPException(403, "knowledge_permission_missing")
    if rights["retain_until"] is not None:
        raise HTTPException(422, "kb_import_requires_unlimited_retention_policy")
    existing = await db.scalar(select(CollectorOutbox).where(CollectorOutbox.tenant_id == ctx.tenant_id,
        CollectorOutbox.scope == ctx.scope, CollectorOutbox.sink == "knowledge", CollectorOutbox.envelope_id == item.envelope_id))
    if existing is None:
        existing = CollectorOutbox(job_id=item.job_id, tenant_id=ctx.tenant_id, scope=ctx.scope,
                                   sink="knowledge", envelope_id=item.envelope_id, payload_hash=item.payload_hash)
        db.add(existing)
        await db.commit()
    return {"status": existing.status, "outbox_id": str(existing.id)}


# ---- CP-S2-07: management surface for the standalone /collect screen -------------------------------------------

class RuntimeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool
    reason: str = Field(default="owner", max_length=200)


async def _pending_review_ids(db, ctx, limit: int, offset: int):
    """Documents that are not in the library yet (held, or accepted but only exported) and whose text has no
    accept/reject review. This is what the owner can still act on."""
    latest = (select(CollectorReview.item_id, CollectorReview.text_sha256, CollectorReview.decision,
                     func.row_number().over(partition_by=CollectorReview.item_id,
                                            order_by=(CollectorReview.created_at.desc(), CollectorReview.id.desc())).label("n"))
              .subquery())
    reviewed = select(latest.c.item_id).where(latest.c.n == 1, latest.c.decision.in_(("accept", "reject")))
    imported = select(CollectorDelivery.item_id).where(CollectorDelivery.outcome == "imported")
    base = select(CollectorItem).where(
        CollectorItem.tenant_id == ctx.tenant_id, CollectorItem.scope == ctx.scope,
        CollectorItem.status.in_(("held", "accepted")), CollectorItem.kind == "document",
        CollectorItem.text_sha256.is_not(None), CollectorItem.id.not_in(reviewed), CollectorItem.id.not_in(imported))
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = list(await db.scalars(base.order_by(CollectorItem.created_at.desc(), CollectorItem.id).limit(limit).offset(offset)))
    return rows, total


@router.get("/summary")
async def collect_summary(ctx: Access, db: Db):
    """Where every discovered URL ended up, plus what is waiting on time, budget, rights or the owner."""
    from ttakkari.collector import budget, runtime
    mine = (CollectorItem.tenant_id == ctx.tenant_id, CollectorItem.scope == ctx.scope)
    by_status = dict((await db.execute(select(CollectorItem.status, func.count()).where(*mine).group_by(CollectorItem.status))).all())
    fetched = await db.scalar(select(func.count()).where(*mine, CollectorItem.raw_sha256.is_not(None))) or 0
    extracted = await db.scalar(select(func.count()).where(*mine, CollectorItem.text_sha256.is_not(None))) or 0
    jobs_by = dict((await db.execute(select(CollectorJob.status, func.count()).where(
        CollectorJob.tenant_id == ctx.tenant_id, CollectorJob.scope == ctx.scope).group_by(CollectorJob.status))).all())
    waiting = list(await db.scalars(select(CollectorJob).where(
        CollectorJob.tenant_id == ctx.tenant_id, CollectorJob.scope == ctx.scope,
        CollectorJob.status.in_(("retry_wait", "budget_wait", "policy_blocked", "quarantined"))
    ).order_by(CollectorJob.updated_at.desc()).limit(10)))
    outbox = dict((await db.execute(select(CollectorOutbox.status, func.count()).where(
        CollectorOutbox.tenant_id == ctx.tenant_id, CollectorOutbox.scope == ctx.scope,
        CollectorOutbox.sink.in_(DELIVERY_SINKS)).group_by(CollectorOutbox.status))).all())
    imported = await db.scalar(select(func.count()).select_from(CollectorDelivery).join(
        CollectorItem, CollectorItem.id == CollectorDelivery.item_id).where(*mine, CollectorDelivery.outcome == "imported")) or 0
    _, pending_review = await _pending_review_ids(db, ctx, 1, 0)
    paused = await runtime.get(db, runtime.PAUSED) or {"paused": False}
    s = get_settings()
    return {
        "funnel": {"discovered": by_status.get("discovered", 0), "fetched": fetched, "extracted": extracted,
                   "accepted": by_status.get("accepted", 0), "held": by_status.get("held", 0),
                   "blocked": by_status.get("blocked", 0), "failed": by_status.get("failed", 0),
                   "not_modified": by_status.get("not_modified", 0), "delivered": imported,
                   "awaiting_delivery": outbox.get("pending", 0), "awaiting_review": pending_review},
        "jobs": jobs_by, "outbox": outbox,
        "waiting": [{"job_id": str(j.id), "status": j.status, "source_key": j.source_key,
                     "not_before": j.not_before.isoformat() if j.not_before else None,
                     "reason": (j.last_error or {}).get("code")} for j in waiting],
        "runtime": {"loop_available": s.collector_control_enabled, "paused": bool(paused.get("paused")),
                    "reason": paused.get("reason"), "since": paused.get("at")},
        "budget": await budget.snapshot(db),
        "daily_page_cap": s.collector_daily_page_cap,
    }


@router.get("/budgets")
async def budgets(ctx: Access, db: Db):
    from ttakkari.collector import budget
    day = budget.budget_day()
    pages = await db.get(CollectorDailyBudget, day)
    return {**await budget.snapshot(db), "pages": {"limit": get_settings().collector_daily_page_cap,
                                                    "reserved": pages.reserved_pages if pages else 0}}


@router.get("/review")
async def review_queue(ctx: Access, db: Db, limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0)):
    rows, total = await _pending_review_ids(db, ctx, limit, offset)
    return {"items": [{"id": str(r.id), "url": r.url, "source_key": r.source_key, "status": r.status,
                       "reason_codes": r.reason_codes, "language": r.language, "text_chars": r.text_chars,
                       "signals": r.signals,
                       "preview": ((r.envelope or {}).get("normalized") or {}).get("text", "")[:1200]} for r in rows],
            "total": total, "limit": limit, "offset": offset}


@router.get("/items/{item_id}")
async def item_detail(item_id: uuid.UUID, ctx: Access, db: Db):
    item = await db.get(CollectorItem, item_id)
    if item is None or item.tenant_id != ctx.tenant_id or item.scope != ctx.scope:
        raise HTTPException(404, "item_not_found")
    env = item.envelope or {}
    reviews = list(await db.scalars(select(CollectorReview).where(CollectorReview.item_id == item.id).order_by(
        CollectorReview.created_at.desc()).limit(20)))
    outbox = list(await db.scalars(select(CollectorOutbox).where(
        CollectorOutbox.envelope_id == item.envelope_id, CollectorOutbox.tenant_id == ctx.tenant_id,
        CollectorOutbox.scope == ctx.scope, CollectorOutbox.sink.in_(DELIVERY_SINKS)))) if item.envelope_id else []
    receipts = {r.outbox_id: r for r in await db.scalars(select(CollectorDelivery).where(
        CollectorDelivery.outbox_id.in_([o.id for o in outbox] or [uuid.uuid4()])))}
    return {
        "id": str(item.id), "job_id": str(item.job_id), "source_key": item.source_key, "url": item.url,
        "final_url": item.final_url, "status": item.status, "stage": item.stage, "http_status": item.http_status,
        "language": item.language, "text_chars": item.text_chars, "reason_codes": item.reason_codes,
        "errors": item.errors, "signals": item.signals, "decision": env.get("decision"),
        "quality": env.get("quality"), "dedupe": env.get("dedupe"), "lineage": env.get("lineage"),
        "versions": {"parser": item.parser_version, "normalizer": item.normalizer_version},
        "hashes": {"raw_sha256": item.raw_sha256, "text_sha256": item.text_sha256, "payload": item.payload_hash},
        "rights": {k: (env.get("rights") or {}).get(k) for k in ("permission", "purposes", "license_id", "policy_url", "retain_until")},
        "fetched_at": item.fetched_at.isoformat() if item.fetched_at else None,
        "reviews": [{"decision": r.decision, "reason": r.reason, "at": r.created_at.isoformat(),
                     "current": r.text_sha256 == item.text_sha256} for r in reviews],
        "deliveries": [{"outbox_id": str(o.id), "sink": o.sink, "status": o.status, "attempts": o.attempts,
                        "last_error": o.last_error,
                        "outcome": receipts[o.id].outcome if o.id in receipts else None} for o in outbox],
        "preview": (env.get("normalized") or {}).get("text", "")[:4000],
    }


@router.get("/deliveries")
async def deliveries(ctx: Access, db: Db, status: Literal["pending", "delivered", "failed", "delivery_unknown"] | None = None,
                     limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    base = select(CollectorOutbox).where(CollectorOutbox.tenant_id == ctx.tenant_id, CollectorOutbox.scope == ctx.scope,
                                         CollectorOutbox.sink.in_(DELIVERY_SINKS))
    if status:
        base = base.where(CollectorOutbox.status == status)
    total = await db.scalar(select(func.count()).select_from(base.subquery()))
    rows = list(await db.scalars(base.order_by(CollectorOutbox.created_at.desc(), CollectorOutbox.id).limit(limit).offset(offset)))
    receipts = {r.outbox_id: r for r in await db.scalars(select(CollectorDelivery).where(
        CollectorDelivery.outbox_id.in_([r.id for r in rows] or [uuid.uuid4()])))}
    return {"items": [{"outbox_id": str(r.id), "job_id": str(r.job_id), "sink": r.sink, "status": r.status,
                       "attempts": r.attempts, "not_before": r.not_before.isoformat() if r.not_before else None,
                       "last_error": r.last_error, "created_at": r.created_at.isoformat(),
                       "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
                       "outcome": receipts[r.id].outcome if r.id in receipts else None,
                       "detail": receipts[r.id].detail if r.id in receipts else None} for r in rows],
            "total": total, "limit": limit, "offset": offset}


@router.post("/deliveries/{outbox_id}/reconcile")
async def reconcile_delivery(outbox_id: uuid.UUID, ctx: Access, db: Db):
    from ttakkari import kb_delivery
    out = await db.scalar(select(CollectorOutbox).where(
        CollectorOutbox.id == outbox_id, CollectorOutbox.tenant_id == ctx.tenant_id,
        CollectorOutbox.scope == ctx.scope).with_for_update())
    if out is None:
        raise HTTPException(404, "delivery_not_found")
    result = await kb_delivery.reconcile(db, out)
    await db.commit()
    return {"outbox_id": str(out.id), "status": out.status, **result}


@router.get("/runtime")
async def runtime_state(ctx: Access, db: Db):
    from ttakkari.collector import runtime
    flag = await runtime.get(db, runtime.PAUSED) or {"paused": False}
    pilot = await runtime.get(db, runtime.PILOT)
    s = get_settings()
    return {"loop_available": s.collector_control_enabled, "general_consume_enabled": s.collector_consume_enabled,
            "paused": bool(flag.get("paused")), "reason": flag.get("reason"), "since": flag.get("at"),
            "pilot": pilot, "pilot_enabled": s.collector_pilot_enabled, "ml_spending_enabled": s.collector_teacher_enabled}


@router.put("/runtime")
async def set_runtime(body: RuntimeIn, ctx: Access, db: Db):
    """Pause or resume new scheduling and new job claims for every generation, effective on the next tick."""
    from ttakkari.collector import runtime
    value = await runtime.set_paused(db, body.paused, body.reason.strip() or "owner")
    await db.commit()
    return value


@router.post("/pilot", status_code=201)
async def register_pilot(ctx: Access, db: Db):
    """Register any missing pilot source for this scope. Existing sources are never touched or re-enabled."""
    from ttakkari.collector import pilot
    created = []
    for spec in pilot.PILOT_SOURCES:
        try:
            async with db.begin_nested():
                await control.register_source(db, ctx, **spec)
            created.append(spec["source_key"])
        except control.SourceKeyExists:
            continue
    await db.commit()
    return {"created": created, "available": [s["source_key"] for s in pilot.PILOT_SOURCES]}


@router.get("/review/audit")
async def hold_audit_sample(ctx: Access, db: Db, per_group: int = Query(2, ge=1, le=10)):
    """Stratified recovery audit of held documents (architecture 7). The sample is a deterministic daily reservoir per
    (source, first reason), so repeated opens within a day show the same items and every stratum is represented."""
    from ttakkari.collector import budget
    day = budget.budget_day()
    reason = func.coalesce(CollectorItem.reason_codes[0].astext, "none")
    rank = func.row_number().over(partition_by=(CollectorItem.source_key, reason),
                                  order_by=func.md5(func.concat(CollectorItem.id.cast(String), day)))
    base = select(CollectorItem, reason.label("reason"), rank.label("n")).where(
        CollectorItem.tenant_id == ctx.tenant_id, CollectorItem.scope == ctx.scope, CollectorItem.status == "held",
        CollectorItem.kind == "document", CollectorItem.text_sha256.is_not(None))
    sub = base.subquery()
    sizes = {(r.source_key, r.reason): r.total for r in (await db.execute(
        select(sub.c.source_key, sub.c.reason, func.count().label("total")).group_by(sub.c.source_key, sub.c.reason))).all()}
    picked = (await db.execute(select(CollectorItem, sub.c.reason).join(sub, sub.c.id == CollectorItem.id).where(
        sub.c.n <= per_group).order_by(sub.c.source_key, sub.c.reason, sub.c.n))).all()
    return {"day": day, "per_group": per_group,
            "groups": [{"source_key": s, "reason": r, "held": n, "sampled": min(per_group, n)} for (s, r), n in sorted(sizes.items())],
            "items": [{"id": str(i.id), "url": i.url, "source_key": i.source_key, "status": i.status, "reason_codes": i.reason_codes,
                       "language": i.language, "text_chars": i.text_chars, "signals": i.signals,
                       "preview": ((i.envelope or {}).get("normalized") or {}).get("text", "")[:1200]} for i, _ in picked]}
