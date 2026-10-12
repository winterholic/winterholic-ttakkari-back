"""메모·캘린더(ADR 0008). 사용자 화면용 API. 에이전트용은 api/agent.py 에 같은 서비스를 쓴다."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.models import CalendarEvent, Memo, utcnow
from ttakkari.planner import kr_holidays
from ttakkari.planner import service as svc
from ttakkari.security.auth import User

router = APIRouter(prefix="/api", tags=["planner"])
MAX_BODY = 200_000
FromDate = Annotated[date, Query(alias="from")]
ToDate = Annotated[date, Query()]


class MemoIn(BaseModel):
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=MAX_BODY)
    tags: list[str] = Field(default_factory=list, max_length=40)
    pinned: bool = False


class MemoPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=MAX_BODY)
    tags: list[str] | None = Field(default=None, max_length=40)
    pinned: bool | None = None
    # 편집기가 마지막으로 본 버전. 다르면 409 로 막아 다른 기기·에이전트의 수정을 덮어쓰지 않는다.
    rev: int | None = None


class EventIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start: str = Field(min_length=10, max_length=40)
    end: str | None = Field(default=None, min_length=10, max_length=40)
    all_day: bool = False
    description: str = Field(default="", max_length=10_000)
    location: str = Field(default="", max_length=200)
    rrule: str | None = Field(default=None, max_length=500)


class EventPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    start: str | None = Field(default=None, min_length=10, max_length=40)
    end: str | None = Field(default=None, min_length=10, max_length=40)
    all_day: bool | None = None
    description: str | None = Field(default=None, max_length=10_000)
    location: str | None = Field(default=None, max_length=200)
    rrule: str | None = Field(default=None, max_length=500)


def new_event(body: EventIn, source: str) -> CalendarEvent:
    ev = CalendarEvent(title=body.title.strip(), description=body.description, location=body.location,
                       all_day=body.all_day, source=source)
    # 끝을 생략하면 종일은 하루, 시간 일정은 한 시간으로 본다.
    ev.start_at, ev.end_at = svc.to_range(body.start, body.end or body.start, body.all_day)
    if body.end is None and not body.all_day:
        ev.end_at = ev.start_at + timedelta(hours=1)
    ev.rrule = svc.normalize_rrule(body.rrule, ev.start_at) if body.rrule else None
    return ev


# ---- 메모

@router.get("/memos")
async def list_memos(_: User, db: Db, q: str | None = None, tag: str | None = None, pinned: bool | None = None,
                     limit: int = Query(default=100, ge=1, le=300), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    rows = await db.scalars(svc.memo_query(q, tag, pinned).limit(limit).offset(offset))
    return {"memos": [svc.memo_out(m, body=False) for m in rows]}


@router.get("/memos/tags")
async def memo_tags(_: User, db: Db) -> dict[str, Any]:
    # 태그는 최대 20개씩이라 메모 수가 많지 않은 개인 앱에서는 파이썬 집계로 충분하다.
    counts: dict[str, int] = {}
    for tags in await db.scalars(select(Memo.tags).where(Memo.deleted_at.is_(None))):
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"tags": [{"name": n, "count": c} for n, c in rows]}


@router.post("/memos", status_code=status.HTTP_201_CREATED)
async def create_memo(body: MemoIn, _: User, db: Db) -> dict[str, Any]:
    m = Memo(title=body.title.strip(), body=body.body, tags=svc.clean_tags(body.tags), pinned=body.pinned, source="user")
    db.add(m)
    await db.commit()
    return svc.memo_out(m)


@router.get("/memos/{memo_id}")
async def get_memo(memo_id: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    return svc.memo_out(await svc.get_memo(db, memo_id))


@router.patch("/memos/{memo_id}")
async def patch_memo(memo_id: uuid.UUID, body: MemoPatch, _: User, db: Db) -> dict[str, Any]:
    m = await svc.get_memo(db, memo_id)
    svc.check_rev(m, body.rev)
    if body.title is not None:
        m.title = body.title.strip()
    if body.body is not None:
        m.body = body.body
    if body.tags is not None:
        m.tags = svc.clean_tags(body.tags)
    if body.pinned is not None:
        m.pinned = body.pinned
    m.rev += 1
    m.updated_at = utcnow()
    await db.commit()
    return svc.memo_out(m)


@router.delete("/memos/{memo_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_memo(memo_id: uuid.UUID, _: User, db: Db) -> Response:
    """사용자는 화면에서 확인을 거친 뒤 완전히 지운다(에이전트의 삭제는 보관 처리)."""
    m = await svc.get_memo(db, memo_id, include_deleted=True)
    record(db, actor="user", action="memo.delete", target_type="memo", target_id=m.id)
    await db.delete(m)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- 캘린더

@router.get("/calendar/events")
async def list_events(_: User, db: Db, from_: FromDate, to: ToDate, q: str | None = None) -> dict[str, Any]:
    return {"events": await svc.list_events(db, from_, to, q)}


@router.get("/calendar/holidays")
async def list_holidays(_: User, from_: FromDate, to: ToDate) -> dict[str, Any]:
    svc.window(from_, to)
    return {"holidays": kr_holidays.holidays_between(from_, to),
            "coverage": {"from_year": kr_holidays.MIN_YEAR, "to_year": kr_holidays.MAX_YEAR}}


@router.post("/calendar/events", status_code=status.HTTP_201_CREATED)
async def create_event(body: EventIn, _: User, db: Db) -> dict[str, Any]:
    ev = new_event(body, "user")
    db.add(ev)
    await db.commit()
    return svc.event_out(ev)


@router.get("/calendar/events/{event_id}")
async def get_event(event_id: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    return svc.event_out(await svc.get_event(db, event_id))


@router.patch("/calendar/events/{event_id}")
async def patch_event(event_id: uuid.UUID, body: EventPatch, _: User, db: Db) -> dict[str, Any]:
    ev = await svc.get_event(db, event_id)
    svc.apply_event_fields(ev, body.model_dump(exclude_unset=True))
    ev.updated_at = utcnow()
    await db.commit()
    return svc.event_out(ev)


@router.delete("/calendar/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_event(event_id: uuid.UUID, _: User, db: Db) -> Response:
    ev = await svc.get_event(db, event_id, include_deleted=True)
    record(db, actor="user", action="calendar.delete", target_type="calendar_event", target_id=ev.id)
    await db.delete(ev)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
