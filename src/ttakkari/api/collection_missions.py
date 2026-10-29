"""Owner mission API. Authentication remains mandatory; source rights stay server-owned."""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ttakkari.api.deps import Db
from ttakkari.collector import missions, store
from ttakkari.collector.control_models import CollectorSource
from ttakkari.collector.library import LIBRARY_SCOPE, in_library
from ttakkari.collector_models import (
    CollectionMission,
    CollectionMissionJob,
    CollectorHostState,
    CollectorJob,
)
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/collect/missions", tags=["collect-missions"])
agent_router = APIRouter(prefix="/api/agent/collection/missions", tags=["agent"])


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_seq: int = Field(ge=1)


_ERROR_CLASSIFICATION_FIELDS = {
    "code", "type", "cause", "cause_code", "attempts", "error_type", "error_code", "retryable",
}
_ERROR_CODES = frozenset({
    "all_items_blocked", "attempt_fenced", "attempts_exhausted", "cancelled", "collector_error",
    "current_source_retention_expired", "envelope_id_conflict", "handler_error", "idempotency_conflict",
    "invalid_envelope", "invalid_job_state", "invalid_reprocess_request", "invalid_request", "invalid_source_retention",
    "items_failed", "job_not_found", "mission_page_budget", "owner_lost", "quality_model_invalid",
    "raw_blob_hash_mismatch", "raw_blob_unavailable", "raw_reprocessing_not_permitted",
    "raw_retention_expired", "reprocess_source_changed", "reprocess_source_unavailable",
    "reprocessed_item_failed", "scope_binding_mismatch", "search_provider_failed", "source_policy_missing",
    "storage_cap_reached", "store_not_permitted", "unbound_mission_preparation", "unsupported_source_kind",
    "fetch_deferred",
})
_ERROR_TYPES = frozenset({
    "CollectorContractError", "FetchError", "KeyError", "OSError", "RuntimeError", "TimeoutError",
    "TypeError", "ValueError",
})


def _classified_error(error):
    """Keep bounded exception classification; never return raw exception text or nested payloads."""
    if not isinstance(error, dict):
        return {}
    result = {}
    for key in _ERROR_CLASSIFICATION_FIELDS:
        value = error.get(key)
        if key in {"code", "cause", "cause_code", "error_code"}:
            if isinstance(value, str) and value in _ERROR_CODES:
                result[key] = value
        elif key in {"type", "error_type"}:
            if isinstance(value, str) and value in _ERROR_TYPES:
                result[key] = value
        elif key == "attempts":
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 1000:
                result[key] = value
        elif key == "retryable" and isinstance(value, bool):
            result[key] = value
    return result


def _safe_url(raw):
    """Return the source origin and path while excluding credentials, query and fragment."""
    try:
        parts = urlsplit(str(raw))
        host = parts.hostname
        if parts.scheme not in {"http", "https"} or not host:
            return None
        host = host.lower()
        port = parts.port
        if ":" in host:
            host = f"[{host}]"
        if port is not None and not (parts.scheme == "https" and port == 443) and not (parts.scheme == "http" and port == 80):
            host = f"{host}:{port}"
        return f"{parts.scheme}://{host}{(parts.path or '/')[:500]}"
    except (TypeError, ValueError):
        return None


def _policy_diagnostic(spec):
    """Expose only the source-rights fields needed to explain a policy match."""
    if not isinstance(spec, dict):
        return {}
    result = {}
    for key, cap in (("allowed_hosts", 100), ("allowed_path_prefixes", 100), ("excluded_path_prefixes", 100)):
        values = spec.get(key)
        if isinstance(values, (list, tuple)):
            result[key] = [value[:300] for value in values[:cap] if isinstance(value, str)]
    if isinstance(spec.get("license_id"), str):
        result["license_id"] = spec["license_id"][:100]
    if isinstance(spec.get("robots_required"), bool):
        result["robots_required"] = spec["robots_required"]
    permissions = spec.get("permissions")
    if isinstance(permissions, dict):
        result["permissions"] = {key: value for key, value in permissions.items()
                                 if key in {"store", "knowledge", "train", "export"}
                                 and value in {"allowed", "denied", "unknown"}}
    if isinstance(spec.get("policy_url"), str):
        result["policy_url"] = _safe_url(spec["policy_url"])
    return result


async def _owner_mission(db, mission_id, *, lock=False):
    query = select(CollectionMission).where(CollectionMission.id == mission_id, CollectionMission.tenant_id == "owner")
    mission = await db.scalar(query.with_for_update() if lock else query)
    if mission is None:
        raise HTTPException(404, "수집 임무를 찾을 수 없습니다.")
    return mission


async def _diagnostic_view(db, mission):
    """Bounded error evidence; exclude request context, document bodies and raw error messages."""
    from ttakkari.oplog.scrub import scrub_value

    view = await missions.view(db, mission)
    current = [job for job in view["jobs"] if job["run_seq"] == view["run_seq"]]
    state = mission.plan.get("research") or {}
    source_diagnostics = await _source_diagnostics(db, mission)
    reservations = [{"job_id": job["job_id"], "stage": job["stage"], "status": job["status"],
                     "active": job["status"] not in missions.TERMINAL} for job in current[:50]]
    return scrub_value({
        **{key: view[key] for key in ("id", "title", "status", "run_seq", "counts", "research", "knowledge_processing")},
        "stored_status": mission.status,
        "jobs": [{**{key: job[key] for key in ("job_id", "status", "stage", "run_seq")},
                  "error": _classified_error(job["error"])} for job in current[:50]],
        "jobs_truncated": len(current) > 50,
        "gaps": [{**{key: str(value)[:300] for key, value in gap.items() if key in {"reason", "stop"}},
                  **({"url": _safe_url(gap["url"])} if isinstance(gap.get("url"), str) else {})}
                 for gap in mission.gaps[:30]],
        "gaps_truncated": len(mission.gaps) > 30,
        "search_failures": [{key: value for key, value in failure.items() if key in {"index", "code", "retryable"}}
                            for failure in (state.get("failed") or [])[:20]],
        "source_count": len(mission.plan.get("sources") or []),
        "sources": source_diagnostics,
        "reservations": {"active_count": sum(row["active"] for row in reservations), "jobs": reservations},
    })


async def _source_diagnostics(db, mission):
    """Read only policies used by the collector, scoped to this owner mission's source paths."""
    entries = (mission.plan.get("sources") or [])[:100]
    candidates = list(await db.scalars(select(CollectorSource).where(
        CollectorSource.tenant_id == mission.tenant_id,
        in_library(CollectorSource.scope), CollectorSource.enabled.is_(True),
    )))
    observations = []
    origins = set()
    parsed_entries = []
    for entry in entries:
        raw_url = entry.get("url") if isinstance(entry, dict) else None
        safe_url = _safe_url(raw_url)
        if not safe_url:
            continue
        parsed = missions.input_url(raw_url)
        parts = urlsplit(raw_url)
        origin = f"{parts.scheme}://{parts.hostname.lower()}"
        if parts.port is not None and not (parts.scheme == "https" and parts.port == 443) and not (parts.scheme == "http" and parts.port == 80):
            host = f"[{parts.hostname.lower()}]" if ":" in parts.hostname else parts.hostname.lower()
            origin = f"{parts.scheme}://{host}:{parts.port}"
        origins.add(origin)
        parsed_entries.append((entry, parsed, safe_url, origin))

    host_states = {}
    if origins:
        rows = await db.scalars(select(CollectorHostState).where(CollectorHostState.origin.in_(origins)))
        host_states = {row.origin: row for row in rows}

    active_policies = []
    for candidate in candidates:
        active = await store.active_policy(db, mission.tenant_id, LIBRARY_SCOPE, candidate.source_key)
        if active:
            policy, version, spec_hash = active
            active_policies.append((candidate, policy, version, spec_hash))

    now = datetime.now(UTC)
    for entry, parsed, safe_url, origin in parsed_entries:
        matched = None
        policy_candidates = []
        seeded = [row for row in active_policies if row[0].request and
                  entry["url"] in row[0].request.get("seeds", [])]
        ordered = seeded + [row for row in active_policies if row not in seeded]
        for candidate, policy, version, spec_hash in ordered:
            reason = "store_permission_missing" if not policy.allows("store") else policy.check_url(parsed)
            relevant = (candidate in [row[0] for row in seeded] or policy.host_allowed(parsed.host))
            if reason is None:
                matched = {"matched": True,
                           "source_key_hash": hashlib.sha256(candidate.source_key.encode()).hexdigest()[:16],
                           "version": version, "spec_hash": spec_hash, **_policy_diagnostic(policy.to_dict())}
                break
            if relevant and len(policy_candidates) < 10:
                policy_candidates.append({"source_key_hash": hashlib.sha256(candidate.source_key.encode()).hexdigest()[:16],
                                          "version": version, "spec_hash": spec_hash, "reason": reason,
                                          **_policy_diagnostic(policy.to_dict())})
        host_state = host_states.get(origin)
        robots = {"status": host_state.robots_status if host_state and host_state.robots_status else "not_cached",
                  "fresh": bool(host_state and host_state.robots_expires_at and host_state.robots_expires_at > now)}
        if host_state:
            robots.update({"fetched_at": host_state.robots_fetched_at.isoformat() if host_state.robots_fetched_at else None,
                           "expires_at": host_state.robots_expires_at.isoformat() if host_state.robots_expires_at else None})
        policy_view = matched or {"matched": False,
                                  "reason": policy_candidates[0]["reason"] if policy_candidates else "source_policy_required",
                                  "candidates": policy_candidates}
        observations.append({"url": safe_url,
                             "policy": policy_view, "robots": robots})
    return observations


@agent_router.get("/{mission_id}")
async def agent_status(mission_id: uuid.UUID, sid: AgentSession, db: Db):
    from ttakkari.api.operations import caller

    await caller(sid, db)
    return await _diagnostic_view(db, await _owner_mission(db, mission_id))


@agent_router.post("/{mission_id}/retry", status_code=202)
async def agent_retry(mission_id: uuid.UUID, body: RetryInput, sid: AgentSession, db: Db):
    from ttakkari.api.operations import caller
    from ttakkari.audit import record

    await caller(sid, db)
    await store._lock(db, "mission-control", "owner", str(mission_id))
    mission = await _owner_mission(db, mission_id, lock=True)
    if (mission.current_run_seq or 1) != body.expected_run_seq:
        raise HTTPException(409, "임무 회차가 바뀌었습니다. 현재 상태를 다시 확인해 주세요.")
    if mission.status in {"paused", "cancelled"}:
        raise HTTPException(409, "사용자가 중단한 임무는 자동 재시도하지 않습니다.")
    active = await db.scalar(select(CollectorJob.id).join(
        CollectionMissionJob, CollectionMissionJob.job_id == CollectorJob.id
    ).where(CollectionMissionJob.mission_id == mission.id, CollectorJob.status.not_in(missions.TERMINAL)).limit(1))
    if active:
        raise HTTPException(409, "진행 중인 작업이 있어 재시도하지 않았습니다.")
    mission = await missions.rerun(db, mission.id)
    record(db, actor="agent", action="collection.retry", target_type="collection_mission", target_id=str(mission.id),
           detail={"session_id": str(sid), "previous_run_seq": body.expected_run_seq, "run_seq": mission.current_run_seq})
    await db.commit()
    return await _diagnostic_view(db, mission)


class MissionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=20_000)
    context: str = Field(default="", max_length=500_000)
    urls: list[str] = Field(default_factory=list, max_length=100)
    max_pages: int = Field(default=200, ge=1, le=100_000)
    max_depth: int = Field(default=3, ge=0, le=12)
    request_key: str | None = Field(default=None, min_length=1, max_length=256)


class ExtendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_pages: int = Field(default=50, ge=1, le=100_000)


class ScheduleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_minutes: int | None = Field(default=None, ge=15, le=43_200)


@router.post("/plan")
async def preview(body: MissionInput, _: User):
    try:
        return missions.plan(**body.model_dump(exclude={"request_key"}))
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("", status_code=202)
async def create(body: MissionInput, _: User, db: Db):
    try:
        mission = await missions.create(db, **body.model_dump())
        await db.commit()
        return await missions.view(db, mission)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("")
async def list_missions(_: User, db: Db, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0)):
    rows = list(await db.scalars(select(CollectionMission).where(CollectionMission.tenant_id == "owner")
                                .order_by(CollectionMission.created_at.desc(), CollectionMission.id.desc()).limit(limit).offset(offset)))
    return {"items": [await missions.view(db, row, detailed=False) for row in rows], "limit": limit, "offset": offset}


@router.get("/{mission_id}")
async def detail(mission_id: uuid.UUID, _: User, db: Db):
    mission = await db.scalar(select(CollectionMission).where(CollectionMission.id == mission_id, CollectionMission.tenant_id == "owner"))
    if mission is None:
        raise HTTPException(404, "수집 임무를 찾을 수 없습니다.")
    return await missions.view(db, mission)


@router.post("/{mission_id}/items/{item_id}/unhold")
async def unhold_mission_item(mission_id: uuid.UUID, item_id: uuid.UUID, _: User, db: Db):
    try:
        result = await missions.unhold_item(db, mission_id, item_id)
        await db.commit()
        return result
    except ValueError as exc:
        if str(exc) == "item_not_found":
            raise HTTPException(404, "수집 문서를 찾을 수 없습니다.") from exc
        if str(exc) == "knowledge_permission_missing":
            raise HTTPException(403, "knowledge_permission_missing") from exc
        raise HTTPException(422, str(exc)) from exc


@router.post("/{mission_id}/pause")
async def pause_mission(mission_id: uuid.UUID, _: User, db: Db):
    try:
        mission = await missions.pause(db, mission_id)
        await db.commit()
        return await missions.view(db, mission)
    except ValueError as exc:
        raise HTTPException(404 if "찾을 수 없습니다" in str(exc) else 422, str(exc)) from exc


@router.post("/{mission_id}/resume")
async def resume_mission(mission_id: uuid.UUID, _: User, db: Db):
    try:
        mission = await missions.resume(db, mission_id)
        await db.commit()
        return await missions.view(db, mission)
    except ValueError as exc:
        raise HTTPException(404 if "찾을 수 없습니다" in str(exc) else 422, str(exc)) from exc


@router.post("/{mission_id}/cancel")
async def cancel_mission(mission_id: uuid.UUID, _: User, db: Db):
    try:
        mission = await missions.cancel(db, mission_id)
        await db.commit()
        return await missions.view(db, mission)
    except ValueError as exc:
        raise HTTPException(404 if "찾을 수 없습니다" in str(exc) else 422, str(exc)) from exc


@router.post("/{mission_id}/extend")
async def extend_mission(mission_id: uuid.UUID, body: ExtendInput, _: User, db: Db):
    try:
        mission = await missions.extend(db, mission_id, add_pages=body.add_pages)
        await db.commit()
        return await missions.view(db, mission)
    except ValueError as exc:
        raise HTTPException(404 if "찾을 수 없습니다" in str(exc) else 422, str(exc)) from exc


@router.post("/{mission_id}/rerun")
async def rerun_mission(mission_id: uuid.UUID, _: User, db: Db):
    try:
        mission = await missions.rerun(db, mission_id)
        await db.commit()
        return await missions.view(db, mission)
    except ValueError as exc:
        raise HTTPException(404 if "찾을 수 없습니다" in str(exc) else 422, str(exc)) from exc


@router.post("/{mission_id}/schedule")
async def schedule_mission(mission_id: uuid.UUID, body: ScheduleInput, _: User, db: Db):
    try:
        mission = await missions.update_schedule(db, mission_id, interval_minutes=body.interval_minutes)
        await db.commit()
        return await missions.view(db, mission)
    except ValueError as exc:
        raise HTTPException(404 if "찾을 수 없습니다" in str(exc) else 422, str(exc)) from exc
