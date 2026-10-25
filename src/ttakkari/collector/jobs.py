"""Collector Platform: durable job claim, attempt-token fence, page commit and orphan recovery (CP-S2-02).

Every function takes the caller's AsyncSession and never commits; the caller owns the transaction. That keeps
"state + cursor + outbox" in one commit and lets a failed caller roll the whole step back.

Design rules (architecture.md section 4):
- claim uses SELECT ... FOR UPDATE SKIP LOCKED and stores a fresh attempt token and the owner generation.
- every later write is fenced by the token: a stale or recovered attempt gets AttemptFencedError and changes nothing.
- heartbeat age alone never takes a job from a live owner. Recovery needs the owner confirmed offline; an unknown
  owner leaves the job held.
- cancellation is checked at batch boundaries (commit_page / finish); queued jobs cancel immediately.
"""
from __future__ import annotations

import base64
import random
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.contracts import (
    AttemptFencedError,
    CollectorConflictError,
    CollectorContractError,
    JobNotFoundError,
    JobStateError,
)
from ttakkari.collector_models import CollectorJob, CollectorOutbox

CLAIMABLE = ("queued", "retry_wait")
CANCELLABLE_NOW = ("queued", "retry_wait", "budget_wait")
FINISH_STATUSES = frozenset(
    {"succeeded", "partial", "failed", "retry_wait", "budget_wait", "policy_blocked", "quarantined"}
)
MAX_RETRY_DELAY_SECONDS = 3600
BASE_RETRY_DELAY_SECONDS = 30
MAX_LIST_LIMIT = 100

OwnerState = Literal["alive", "offline", "unknown"]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    attempt_token: uuid.UUID
    tenant_id: str
    scope: str
    stage: str
    attempts: int
    max_attempts: int
    envelope: dict[str, Any]
    cursor: dict[str, Any] | None
    owner_generation: str


@dataclass(frozen=True)
class OutboxItem:
    """An additional delivery row committed together with the page that produced it."""

    sink: str
    envelope_id: uuid.UUID
    payload_hash: str


@dataclass(frozen=True)
class RecoveryReport:
    recovered: list[uuid.UUID] = field(default_factory=list)
    held: list[uuid.UUID] = field(default_factory=list)  # owner liveness could not be confirmed
    alive: list[uuid.UUID] = field(default_factory=list)  # owner confirmed alive, never taken over


def retry_delay(attempts: int, retry_after: float | None = None, rng: random.Random | None = None) -> timedelta:
    """Exponential backoff with up to 20% jitter. A server Retry-After is a floor, never shortened."""
    base = min(BASE_RETRY_DELAY_SECONDS * 2 ** max(attempts - 1, 0), MAX_RETRY_DELAY_SECONDS)
    jitter = (rng or random).uniform(0, base * 0.2)
    return timedelta(seconds=max(base + jitter, retry_after or 0))


async def claim_batch(
    db: AsyncSession, *, owner_generation: str, limit: int = 1, now: datetime | None = None,
    managed_only: bool = False,
) -> list[ClaimedJob]:
    """Lock up to `limit` runnable jobs for this owner. Jobs with no attempts left are not claimed."""
    now = now or _now()
    from ttakkari.collector.control_models import CollectorSource
    from ttakkari.collector_models import CollectionMissionJob
    managed = or_(select(CollectorSource.id).where(CollectorSource.last_job_id == CollectorJob.id).exists(),
                  select(CollectionMissionJob.job_id).where(CollectionMissionJob.job_id == CollectorJob.id).exists())
    rows = (
        await db.scalars(
            select(CollectorJob)
            .where(
                CollectorJob.status.in_(CLAIMABLE),
                CollectorJob.cancel_requested.is_(False),
                CollectorJob.attempts < CollectorJob.max_attempts,
                managed if managed_only else True,
                or_(CollectorJob.not_before.is_(None), CollectorJob.not_before <= now),
            )
            .order_by(CollectorJob.created_at, CollectorJob.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).all()
    claimed: list[ClaimedJob] = []
    for job in rows:
        job.status = "running"
        job.owner_generation = owner_generation
        job.attempt_token = uuid.uuid4()
        job.attempts += 1
        job.heartbeat_at = now
        job.updated_at = now
        claimed.append(
            ClaimedJob(
                id=job.id,
                attempt_token=job.attempt_token,
                tenant_id=job.tenant_id,
                scope=job.scope,
                stage=job.stage,
                attempts=job.attempts,
                max_attempts=job.max_attempts,
                envelope=job.envelope,
                cursor=job.cursor,
                owner_generation=owner_generation,
            )
        )
    await db.flush()
    return claimed


async def _fenced_job(db: AsyncSession, job_id: uuid.UUID, attempt_token: uuid.UUID) -> CollectorJob:
    job = (
        await db.execute(
            select(CollectorJob)
            .where(
                CollectorJob.id == job_id,
                CollectorJob.attempt_token == attempt_token,
                CollectorJob.status == "running",
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if job is None:
        raise AttemptFencedError("the attempt no longer owns this job")
    return job


async def heartbeat(db: AsyncSession, job_id: uuid.UUID, attempt_token: uuid.UUID, now: datetime | None = None) -> None:
    job = await _fenced_job(db, job_id, attempt_token)
    job.heartbeat_at = now or _now()
    await db.flush()


async def commit_page(
    db: AsyncSession,
    job_id: uuid.UUID,
    attempt_token: uuid.UUID,
    *,
    cursor: dict[str, Any] | None,
    stage: str | None = None,
    outbox: Iterable[OutboxItem] = (),
    now: datetime | None = None,
) -> Literal["continue", "cancelled"]:
    """Advance the cursor and add child outbox rows in one savepoint, fenced by the attempt token.

    If cancellation was requested the job is cancelled here, at the batch boundary, and "cancelled" is returned.
    A duplicate outbox key rolls back the cursor move too, so a page is never half committed.
    """
    now = now or _now()
    job = await _fenced_job(db, job_id, attempt_token)
    try:
        async with db.begin_nested():
            job.cursor = cursor
            if stage is not None:
                job.stage = stage
            job.heartbeat_at = now
            job.updated_at = now
            for item in outbox:
                db.add(
                    CollectorOutbox(
                        id=uuid.uuid4(),
                        job_id=job.id,
                        tenant_id=job.tenant_id,
                        scope=job.scope,
                        sink=item.sink,
                        envelope_id=item.envelope_id,
                        payload_hash=item.payload_hash,
                        status="pending",
                        created_at=now,
                    )
                )
            await db.flush()
    except IntegrityError as e:
        await db.refresh(job)
        raise CollectorConflictError("outbox row for this sink/envelope already exists; page not committed") from e
    if job.cancel_requested:
        _release(job, "cancelled", now=now, error={"code": "cancelled", "at": now.isoformat()})
        await db.flush()
        return "cancelled"
    return "continue"


def _release(
    job: CollectorJob,
    status: str,
    *,
    now: datetime,
    error: dict[str, Any] | None = None,
    not_before: datetime | None = None,
) -> None:
    job.status = status
    job.attempt_token = None
    job.owner_generation = None
    job.heartbeat_at = None
    job.not_before = not_before
    job.updated_at = now
    if error is not None:
        job.last_error = error


async def finish(
    db: AsyncSession,
    job_id: uuid.UUID,
    attempt_token: uuid.UUID,
    *,
    status: str,
    error: dict[str, Any] | None = None,
    retry_after: float | None = None,
    now: datetime | None = None,
) -> str:
    """End an attempt. Returns the status actually stored.

    - a requested cancellation wins over succeeded/partial/retry_wait/budget_wait (failed, policy_blocked and
      quarantined are real outcomes and are kept)
    - retry_wait with no attempts left becomes failed (no infinite retry)
    - retry_wait stores a durable not_before from exponential backoff + jitter (Retry-After is a floor)
    """
    if status not in FINISH_STATUSES:
        raise JobStateError(f"cannot finish an attempt as '{status}'")
    now = now or _now()
    job = await _fenced_job(db, job_id, attempt_token)
    not_before = None
    if status in ("succeeded", "partial", "retry_wait", "budget_wait") and job.cancel_requested:
        # A cancel recorded before the attempt ended wins even if the work happened to finish first: the caller was
        # told "cancelling". Committed pages stay. The would-be status is kept in the error for audit.
        error = {"code": "cancelled", "at": now.isoformat(), **({"finished_as": status} if status in ("succeeded", "partial") else {})}
        status = "cancelled"
    elif status == "retry_wait":
        if job.attempts >= job.max_attempts:
            status = "failed"
            error = {**(error or {}), "code": "attempts_exhausted", "attempts": job.attempts}
        else:
            not_before = now + retry_delay(job.attempts, retry_after)
    _release(job, status, now=now, error=error, not_before=not_before)
    await db.flush()
    return status


async def yield_attempt(db: AsyncSession, job_id: uuid.UUID, attempt_token: uuid.UUID) -> str:
    """Release a successful bounded batch for the next tick, without spending a retry.

    Cursor/items were already committed at a fenced page boundary. Crashes still spend
    an attempt; only this explicit healthy yield restores it. Cancellation wins.
    """
    job = await _fenced_job(db, job_id, attempt_token)
    status = "cancelled" if job.cancel_requested else "queued"
    job.attempts = max(0, job.attempts - 1)
    job.last_error = None
    _release(job, status, now=_now())
    await db.flush()
    return status


async def _scoped_job(db: AsyncSession, tenant_id: str, scope: str, job_id: uuid.UUID, *, lock: bool = False) -> CollectorJob:
    stmt = select(CollectorJob).where(
        CollectorJob.id == job_id, CollectorJob.tenant_id == tenant_id, CollectorJob.scope == scope
    )
    job = (await db.execute(stmt.with_for_update() if lock else stmt)).scalar_one_or_none()
    if job is None:
        raise JobNotFoundError("job not found")
    return job


async def get_job(db: AsyncSession, tenant_id: str, scope: str, job_id: uuid.UUID) -> CollectorJob:
    return await _scoped_job(db, tenant_id, scope, job_id)


def _encode_cursor(job: CollectorJob) -> str:
    return base64.urlsafe_b64encode(f"{job.created_at.isoformat()}|{job.id}".encode()).decode()


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        created, ident = base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        return datetime.fromisoformat(created), uuid.UUID(ident)
    except Exception as e:
        raise CollectorContractError("cursor is not a valid job cursor") from e


async def list_jobs(
    db: AsyncSession, tenant_id: str, scope: str, *, limit: int = 20, cursor: str | None = None
) -> tuple[list[CollectorJob], str | None]:
    """Newest first, keyset paginated. Never returns another tenant/scope's rows."""
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise CollectorContractError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    stmt = select(CollectorJob).where(CollectorJob.tenant_id == tenant_id, CollectorJob.scope == scope)
    if cursor:
        created, ident = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                CollectorJob.created_at < created,
                and_(CollectorJob.created_at == created, CollectorJob.id < ident),
            )
        )
    rows = list(
        (await db.scalars(stmt.order_by(CollectorJob.created_at.desc(), CollectorJob.id.desc()).limit(limit + 1))).all()
    )
    if len(rows) > limit:
        return rows[:limit], _encode_cursor(rows[limit - 1])
    return rows, None


async def cancel(
    db: AsyncSession, tenant_id: str, scope: str, job_id: uuid.UUID, now: datetime | None = None
) -> Literal["cancelled", "cancelling"]:
    """Cancel now when nobody runs the job; otherwise flag it so the running attempt stops at its next boundary.

    Cancelling an already cancelled job is idempotent. A finished job cannot be cancelled (409).
    """
    now = now or _now()
    job = await _scoped_job(db, tenant_id, scope, job_id, lock=True)
    if job.status == "cancelled":
        return "cancelled"
    if job.status in CANCELLABLE_NOW:
        _release(job, "cancelled", now=now, error={"code": "cancelled", "at": now.isoformat()})
        job.cancel_requested = True
        await db.flush()
        return "cancelled"
    if job.status == "running":
        job.cancel_requested = True
        job.updated_at = now
        await db.flush()
        return "cancelling"
    raise JobStateError(f"job is already {job.status} and cannot be cancelled")


async def recover_orphans(
    db: AsyncSession,
    *,
    owner_state: Callable[[str], OwnerState],
    stale_after: timedelta,
    now: datetime | None = None,
) -> RecoveryReport:
    """Return running jobs of a dead owner to the queue. Heartbeat age only nominates candidates.

    alive   -> untouched (a slow live generation keeps its job)
    unknown -> held: nothing changes until liveness can be confirmed
    offline -> retry_wait (or failed when no attempts are left); the old attempt token is cleared, so a late
               commit from the old owner is fenced.
    """
    now = now or _now()
    cutoff = now - stale_after
    rows = (
        await db.scalars(
            select(CollectorJob)
            .where(
                CollectorJob.status == "running",
                or_(CollectorJob.heartbeat_at.is_(None), CollectorJob.heartbeat_at < cutoff),
            )
            .order_by(CollectorJob.created_at)
            .with_for_update(skip_locked=True)
        )
    ).all()
    recovered: list[uuid.UUID] = []
    held: list[uuid.UUID] = []
    alive: list[uuid.UUID] = []
    for job in rows:
        state = owner_state(job.owner_generation) if job.owner_generation else "unknown"
        if state == "alive":
            alive.append(job.id)
        elif state == "offline":
            error = {"code": "owner_lost", "owner": job.owner_generation, "at": now.isoformat()}
            if job.attempts >= job.max_attempts:
                error["code"] = "attempts_exhausted"
                _release(job, "failed", now=now, error=error)
            else:
                _release(job, "retry_wait", now=now, error=error, not_before=now)
            recovered.append(job.id)
        else:
            held.append(job.id)
    await db.flush()
    return RecoveryReport(recovered=recovered, held=held, alive=alive)
