"""Owner mission API. Authentication remains mandatory; source rights stay server-owned."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ttakkari.api.deps import Db
from ttakkari.collector import missions, store
from ttakkari.collector_models import (
    CollectionMission,
    CollectionMissionJob,
    CollectorJob,
)
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/collect/missions", tags=["collect-missions"])
agent_router = APIRouter(prefix="/api/agent/collection/missions", tags=["agent"])


class RetryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_run_seq: int = Field(ge=1)


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
    return scrub_value({
        **{key: view[key] for key in ("id", "title", "status", "run_seq", "counts", "research", "knowledge_processing")},
        "stored_status": mission.status,
        "jobs": [{**{key: job[key] for key in ("job_id", "status", "stage", "run_seq")},
                  "error": {key: str(value)[:80] for key, value in (job["error"] or {}).items()
                            if key in {"code", "type", "cause_code", "attempts"}}} for job in current[:50]],
        "jobs_truncated": len(current) > 50,
        "gaps": [{key: str(value)[:300] for key, value in gap.items() if key in {"reason", "url", "stop"}}
                 for gap in mission.gaps[:30]],
        "gaps_truncated": len(mission.gaps) > 30,
        "search_failures": [{key: value for key, value in failure.items() if key in {"index", "code", "retryable"}}
                            for failure in (state.get("failed") or [])[:20]],
        "source_count": len(mission.plan.get("sources") or []),
    })


@agent_router.get("/{mission_id}")
async def agent_status(mission_id: uuid.UUID, sid: AgentSession, db: Db):
    from ttakkari.api.operations import personal

    await personal(sid, db)
    return await _diagnostic_view(db, await _owner_mission(db, mission_id))


@agent_router.post("/{mission_id}/retry", status_code=202)
async def agent_retry(mission_id: uuid.UUID, body: RetryInput, sid: AgentSession, db: Db):
    from ttakkari.api.operations import personal
    from ttakkari.audit import record

    await personal(sid, db)
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
