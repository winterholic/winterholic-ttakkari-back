"""Owner-managed sources, atomic scheduled intake and bounded admission. No model calls."""
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from ttakkari.collector import jobs, store
from ttakkari.collector.budget import budget_day
from ttakkari.collector.contracts import (
    AuthContext,
    CollectorContractError,
    JobStateError,
)
from ttakkari.collector.control_models import CollectorDailyBudget, CollectorSource
from ttakkari.collector.intake import submit_envelope
from ttakkari.collector.netguard import normalize_url
from ttakkari.collector.policy import CollectRequest, SourcePolicySpec
from ttakkari.collector.quality import unmeasured_quality
from ttakkari.collector_models import CollectorJob
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker

ACTIVE = ("queued", "running", "retry_wait", "budget_wait")


class SourceKeyExists(Exception):
    pass


async def register_source(db, ctx: AuthContext, *, source_key: str, title: str, seeds: list[str], policy: dict,
                          interval_minutes: int | None, max_pages: int, max_depth: int = 0,
                          profile: str = "general", auto_import: bool = True, profile_params: dict | None = None):
    """Validate the policy and every seed against it, then register policy version and source in one transaction.
    Shared by the owner API and the pilot bootstrap so both pass exactly the same checks. Raises ValueError / SourceKeyExists."""
    spec = SourcePolicySpec.from_dict(policy)
    if not spec.allows("store"):
        raise CollectorContractError("store permission must be verified before source registration")
    request = {"seeds": seeds, "profile": profile, "max_pages": max_pages, "max_depth": max_depth}
    if profile_params:
        request["profile_params"] = profile_params
    CollectRequest.from_envelope({"extensions": {"collector:request": request}}, spec)
    for seed in seeds:
        parsed = normalize_url(seed, allowed_schemes=spec.schemes, allowed_ports=spec.ports)
        if reason := spec.check_url(parsed):
            raise CollectorContractError(reason)
    await store._lock(db, "control-source", ctx.tenant_id, ctx.scope, source_key)
    existing = await db.scalar(select(CollectorSource).where(
        CollectorSource.tenant_id == ctx.tenant_id, CollectorSource.scope == ctx.scope,
        CollectorSource.source_key == source_key))
    if existing is not None:
        raise SourceKeyExists(source_key)
    await store.register_policy(db, ctx.tenant_id, ctx.scope, source_key, spec)
    row = CollectorSource(tenant_id=ctx.tenant_id, scope=ctx.scope, source_key=source_key, title=title, request=request,
                          interval_minutes=interval_minutes, auto_import=auto_import,
                          next_run_at=datetime.now(UTC) if interval_minutes else None)
    db.add(row)
    return row, spec


def intake_envelope(source: CollectorSource, spec: SourcePolicySpec, now: datetime, envelope_id: uuid.UUID) -> dict:
    """A collection request is held intake metadata; it is never a collected document."""
    return {
        "schema_version": "1.0.0", "envelope_id": str(envelope_id),
        "producer": {"id": "collector-control", "version": "1"}, "recorded_at": now.isoformat(),
        "source": {"key": source.source_key, "kind": "web", "url": source.request["seeds"][0],
                   "collected_at": now.isoformat(), "published_at": None, "valid_from": None, "valid_to": None},
        "access": {"tenant_id": source.tenant_id, "scope": source.scope, "principal_ids": ["owner"], "sensitivity": "normal"},
        "rights": spec.rights_block("intake", now, "not_applicable"),
        "raw": {"sha256": hashlib.sha256(b"").hexdigest(), "byte_size": 0,
                "media_type": "application/octet-stream", "blob_handle": "intake:unfetched"},
        "normalized": None, "quality": unmeasured_quality("intake-v1"), "fact_check_status": "unknown",
        "dedupe": {"exact_of": None, "cluster_id": None, "canonical_candidate": None,
                   "canonical_status": "unreviewed", "algorithm_version": "none"},
        "decision": {"state": "hold", "reason_codes": ["awaiting_fetch"], "policy_version": "intake"},
        "errors": [], "lineage": [], "extensions": {"collector:request": source.request},
    }


async def enqueue(db, source: CollectorSource, *, now: datetime | None = None) -> CollectorJob:
    """Caller holds source row lock; repeated clicks or schedule ticks share an active job."""
    now = now or datetime.now(UTC)
    if not source.enabled:
        raise JobStateError("source is paused")
    if source.last_job_id:
        current = await db.get(CollectorJob, source.last_job_id)
        if current is not None and current.status in ACTIVE:
            return current
    policy = await store.active_policy(db, source.tenant_id, source.scope, source.source_key)
    if policy is None or not policy[0].allows("store"):
        raise JobStateError("source store policy is not allowed")
    # A hard, global page admission ceiling: every new attempt is reserved before enqueue.
    day = budget_day(now)
    await db.execute(insert(CollectorDailyBudget).values(day=day, reserved_pages=0).on_conflict_do_nothing())
    budget = await db.get(CollectorDailyBudget, day, with_for_update=True)
    pages = source.request["max_pages"]
    if budget.reserved_pages + pages > get_settings().collector_daily_page_cap:
        raise JobStateError("daily collection page budget reached")
    budget.reserved_pages += pages
    ident = uuid.uuid4()
    from ttakkari.collector import quality_service
    envelope = intake_envelope(source, policy[0], now, ident)
    model = await quality_service.latest_model(db, source.tenant_id, source.scope)
    if model is not None:
        quality_service.install(model)
        request = dict(source.request)
        request["l1"] = {"mode": "shadow", "scorer": quality_service.scorer_name(source.tenant_id, source.scope, model.digest)}
        envelope["extensions"]["collector:request"] = request
    result = await submit_envelope(
        db, AuthContext(tenant_id=source.tenant_id, scope=source.scope, principal_id="owner", kind="owner"), str(ident),
        envelope,
    )
    source.last_job_id = result.job_id
    source.last_error = None
    if source.interval_minutes is not None:
        source.next_run_at = now + timedelta(minutes=source.interval_minutes)
    await db.flush()
    return await db.get(CollectorJob, result.job_id)


async def schedule_once(*, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    count = 0
    async with sessionmaker()() as db:
        sources = list(await db.scalars(select(CollectorSource).where(
            CollectorSource.enabled.is_(True), CollectorSource.interval_minutes.is_not(None),
            CollectorSource.next_run_at <= now,
        ).order_by(CollectorSource.next_run_at).limit(10).with_for_update(skip_locked=True)))
        for source in sources:
            try:
                async with db.begin_nested():
                    await enqueue(db, source, now=now)
                    source.next_run_at = now + timedelta(minutes=source.interval_minutes)
                    count += 1
            except JobStateError as exc:
                # Visible in the source view; retry at the next scheduled interval, never a tight loop.
                source.last_error = str(exc)[:500]
                source.next_run_at = now + timedelta(minutes=source.interval_minutes)
        await db.commit()
    return count


async def retry_job(db, ctx: AuthContext, job_id: uuid.UUID) -> CollectorJob:
    job = await jobs.get_job(db, ctx.tenant_id, ctx.scope, job_id)
    job = await db.get(CollectorJob, job.id, with_for_update=True, populate_existing=True)
    if job.status in ACTIVE:
        raise JobStateError("job is already active")
    job.status, job.cancel_requested, job.last_error = "queued", False, None
    job.owner_generation, job.attempt_token, job.not_before = None, None, None
    # The cursor and previous attempt count are retained; one explicit retry grants one new attempt.
    job.max_attempts = max(job.max_attempts, job.attempts + 1)
    return job
