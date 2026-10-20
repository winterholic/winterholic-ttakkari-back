"""Collector Platform: Intake service for idempotent envelope registration with RFC 8785 JCS and scope binding."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.contracts import (
    AuthContext,
    CollectorConflictError,
    CollectorContractError,
    EnvelopeIdConflictError,
    compute_payload_hash,
    validate_envelope,
    verify_auth_binding,
)
from ttakkari.collector_models import CollectorJob, CollectorOutbox

DEFAULT_SINK = "default"
# collector_jobs 컬럼 길이. 넘기면 DB 가 DataError 를 내므로 계약 오류로 먼저 거른다.
MAX_KEY_LENGTH = 256


@dataclass(frozen=True)
class IntakeResult:
    job_id: uuid.UUID
    status: str  # "created" or "replay"
    payload_hash: str
    envelope_id: uuid.UUID
    source_key: str
    request_key: str
    job: Any = None


async def submit_envelope(
    db: AsyncSession,
    auth: AuthContext | dict[str, Any],
    request_key: str,
    envelope: dict[str, Any],
) -> IntakeResult:
    """Validate envelope and atomically insert or deduplicate in collector_jobs and collector_outbox.

    Validation Order (as specified in contracts/intake-binding.json):
    1. Schema & semantic validation (raises CollectorContractError, 422)
    2. Scope binding verification against derived auth context (raises ScopeBindingMismatchError, 403)
       - If mismatch: NO job, NO outbox, NO cross-scope dedupe lookup!
    3. Deterministic RFC 8785 JCS payload hash (excluding /recorded_at)
    4. Idempotency lookup: (derived.tenant_id, derived.scope, source.key, request_key)
       - If identical hash: 202 replay (returns existing job ID, 0 new rows)
       - If different hash: 409 conflict (CollectorConflictError)
       - If the envelope_id is already registered in this tenant/scope under another request key: 409
         (EnvelopeIdConflictError), never a raw IntegrityError
       - If new: 202 created (atomic job + outbox in same transaction)
    """
    if not isinstance(request_key, str) or not request_key.strip():
        raise CollectorContractError("request_key must be a non-empty string")
    if len(request_key) > MAX_KEY_LENGTH:
        raise CollectorContractError(f"request_key longer than {MAX_KEY_LENGTH} characters")
    if "\x00" in request_key:
        raise CollectorContractError("request_key contains U+0000")

    # 1. Schema & semantic validation
    validated_envelope = validate_envelope(envelope)
    source_key = validated_envelope["source"]["key"]
    if len(source_key) > MAX_KEY_LENGTH:
        raise CollectorContractError(f"source.key longer than {MAX_KEY_LENGTH} characters")

    # 2. Scope binding verification (never trust envelope access directly)
    derived = verify_auth_binding(auth, validated_envelope)
    tenant_id = derived["tenant_id"]
    scope = derived["scope"]
    envelope_id = uuid.UUID(str(validated_envelope["envelope_id"]))

    # 3. Deterministic RFC 8785 JCS payload hash (excluding /recorded_at)
    payload_hash = compute_payload_hash(validated_envelope)

    # 4. Check for existing job under (tenant_id, scope, source_key, request_key)
    stmt = select(CollectorJob).where(
        CollectorJob.tenant_id == tenant_id,
        CollectorJob.scope == scope,
        CollectorJob.source_key == source_key,
        CollectorJob.request_key == request_key,
    )
    existing_job = (await db.execute(stmt)).scalar_one_or_none()
    if existing_job is not None:
        return _replay_or_conflict(existing_job, payload_hash, source_key, request_key)
    if await _envelope_id_registered(db, tenant_id, scope, envelope_id):
        return await _resolve_collision(db, stmt, payload_hash, source_key, request_key, envelope_id)

    # 5. Insert new job and outbox row within a savepoint (nested transaction)
    job_id = uuid.uuid4()
    now = datetime.now(UTC)
    new_job = CollectorJob(
        id=job_id,
        tenant_id=tenant_id,
        scope=scope,
        source_key=source_key,
        request_key=request_key,
        payload_hash=payload_hash,
        envelope_id=envelope_id,
        status="queued",
        stage="intake",
        envelope=validated_envelope,
        attempts=0,
        max_attempts=3,
        created_at=now,
        updated_at=now,
    )
    outbox_entry = CollectorOutbox(
        id=uuid.uuid4(),
        job_id=job_id,
        tenant_id=tenant_id,
        scope=scope,
        sink=DEFAULT_SINK,
        envelope_id=envelope_id,
        payload_hash=payload_hash,
        status="pending",
        created_at=now,
    )

    try:
        async with db.begin_nested():
            db.add(new_job)
            db.add(outbox_entry)
            await db.flush()
    except IntegrityError:
        # Race: a concurrent submission committed first. The savepoint rollback removed our rows, so re-read.
        return await _resolve_collision(db, stmt, payload_hash, source_key, request_key, envelope_id)

    return IntakeResult(
        job_id=job_id,
        status="created",
        payload_hash=payload_hash,
        envelope_id=envelope_id,
        source_key=source_key,
        request_key=request_key,
        job=new_job,
    )


async def _resolve_collision(
    db: AsyncSession,
    job_stmt: Any,
    payload_hash: str,
    source_key: str,
    request_key: str,
    envelope_id: uuid.UUID,
) -> IntakeResult:
    """The envelope_id is taken. If the same request key owns it (a concurrent twin committed between our
    lookups) this is a replay or a payload conflict; otherwise another request key owns it: 409."""
    existing_job = (await db.execute(job_stmt)).scalar_one_or_none()
    if existing_job is not None:
        return _replay_or_conflict(existing_job, payload_hash, source_key, request_key)
    raise EnvelopeIdConflictError(f"envelope_id {envelope_id} is already registered under another request key")


def _replay_or_conflict(
    existing_job: CollectorJob, payload_hash: str, source_key: str, request_key: str
) -> IntakeResult:
    if existing_job.payload_hash != payload_hash:
        raise CollectorConflictError(
            f"Conflict for request_key '{request_key}': existing payload hash {existing_job.payload_hash} "
            f"differs from submitted payload hash {payload_hash}"
        )
    return IntakeResult(
        job_id=existing_job.id,
        status="replay",
        payload_hash=payload_hash,
        envelope_id=existing_job.envelope_id,
        source_key=source_key,
        request_key=request_key,
        job=existing_job,
    )


async def _envelope_id_registered(db: AsyncSession, tenant_id: str, scope: str, envelope_id: uuid.UUID) -> bool:
    """Same tenant/scope/sink outbox key as the table's unique constraint, so other tenants never collide."""
    found = await db.scalar(
        select(CollectorOutbox.id).where(
            CollectorOutbox.tenant_id == tenant_id,
            CollectorOutbox.scope == scope,
            CollectorOutbox.sink == DEFAULT_SINK,
            CollectorOutbox.envelope_id == envelope_id,
        )
    )
    return found is not None
