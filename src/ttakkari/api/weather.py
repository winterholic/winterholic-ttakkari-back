from __future__ import annotations

from datetime import date
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from ttakkari.api.deps import Db
from ttakkari.models import WeatherBriefing
from ttakkari.security.auth import User
from ttakkari.weather.service import start_for_day

router = APIRouter(prefix="/api/weather", tags=["weather"])


class WeatherOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    day: date
    status: str
    request_source: str
    raw_forecasts: dict[str, Any] | None
    forecast: dict[str, Any] | None
    source_errors: dict[str, str] | None
    briefing: dict[str, Any] | None
    analysis_status: str | None
    analysis_engine: str | None
    analysis_model: str | None
    analysis_route: list[dict[str, Any]] | None
    failure: str | None
    scheduled_at: Any | None
    generated_at: Any | None
    push_sent_at: Any | None
    push_error: str | None
    created_at: Any
    updated_at: Any


@router.get("/briefings", response_model=list[WeatherOut])
async def history(_: User, db: Db, limit: int = 30) -> list[WeatherBriefing]:
    if not 1 <= limit <= 100:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "limit은 1~100이어야 합니다.")
    return list(await db.scalars(select(WeatherBriefing).order_by(WeatherBriefing.day.desc()).limit(limit)))


@router.get("/briefings/{day}", response_model=WeatherOut)
async def by_day(day: date, _: User, db: Db) -> WeatherBriefing:
    row = await db.scalar(select(WeatherBriefing).where(WeatherBriefing.day == day))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "해당 날짜의 날씨 브리핑이 없습니다.")
    return row


@router.get("/latest", response_model=WeatherOut | None)
async def latest(_: User, db: Db) -> WeatherBriefing | None:
    return await db.scalar(select(WeatherBriefing).order_by(WeatherBriefing.day.desc()).limit(1))


@router.post("/generate", response_model=WeatherOut, status_code=status.HTTP_202_ACCEPTED)
async def generate(_: User) -> WeatherBriefing:
    """Generate today's brief. The unique Asia/Seoul day key serializes manual and scheduled starts."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    return await start_for_day(datetime.now(ZoneInfo("Asia/Seoul")).date(), "manual")
