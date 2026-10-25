"""Owner mission API. Authentication remains mandatory; source rights stay server-owned."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ttakkari.api.deps import Db
from ttakkari.collector import missions
from ttakkari.collector_models import CollectionMission
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/collect/missions", tags=["collect-missions"])


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
    interval_minutes: int | None = Field(default=None, ge=1, le=43_200)


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
