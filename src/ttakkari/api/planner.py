"""메모·캘린더(ADR 0008). 사용자 화면용 API. 에이전트용은 api/agent.py 에 같은 서비스를 쓴다."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.models import CalendarEvent, Memo, MemoCategory, utcnow
from ttakkari.planner import kr_holidays, reminders
from ttakkari.planner import service as svc
from ttakkari.security.auth import User

router = APIRouter(prefix="/api", tags=["planner"])
MAX_BODY = 200_000
FromDate = Annotated[date, Query(alias="from")]
ToDate = Annotated[date, Query()]
# 시간 일정: at_start·10m·30m·1h·1d, 종일 일정: day_9am·prev_day_9am. null 이면 알림 없음(기본).
Reminder = Literal["at_start", "10m", "30m", "1h", "1d", "day_9am", "prev_day_9am"]


class MemoIn(BaseModel):
    title: str = Field(default="", max_length=200)
    body: str = Field(default="", max_length=MAX_BODY)
    tags: list[str] = Field(default_factory=list, max_length=40)
    pinned: bool = False
    category_id: uuid.UUID | None = None


class MemoPatch(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=MAX_BODY)
    tags: list[str] | None = Field(default=None, max_length=40)
    pinned: bool | None = None
    # 보내지 않으면 그대로, null 이면 미분류로 옮긴다(model_fields_set 로 구분).
    category_id: uuid.UUID | None = None
    # 편집기가 마지막으로 본 버전. 다르면 409 로 막아 다른 기기·에이전트의 수정을 덮어쓰지 않는다.
    rev: int | None = None


class CategoryIn(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    color: Literal[svc.CATEGORY_COLORS] | None = None  # type: ignore[valid-type]


class CategoryPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    color: Literal[svc.CATEGORY_COLORS] | None = None  # type: ignore[valid-type]


class CategoryOrder(BaseModel):
    ids: list[uuid.UUID] = Field(max_length=200)


def category_filter(category: str | None) -> Any:
    """목록 쿼리의 category: 없으면 전체, "none" 이면 미분류, 그 밖은 카테고리 id."""
    if not category:
        return None
    if category == "none":
        return svc.NONE
    try:
        return uuid.UUID(category)
    except ValueError as e:
        raise svc.bad("category 는 카테고리 id 나 none 이에요.") from e


class EventIn(BaseModel):
    # 반복(rrule)은 보류라 받지 않는다. 모르는 필드는 조용히 버리지 않고 422 로 알린다.
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    start: str = Field(min_length=10, max_length=40)
    end: str | None = Field(default=None, min_length=10, max_length=40)
    all_day: bool = False
    description: str = Field(default="", max_length=10_000)
    location: str = Field(default="", max_length=200)
    reminder: Reminder | None = None


class EventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=200)
    start: str | None = Field(default=None, min_length=10, max_length=40)
    end: str | None = Field(default=None, min_length=10, max_length=40)
    all_day: bool | None = None
    description: str | None = Field(default=None, max_length=10_000)
    location: str | None = Field(default=None, max_length=200)
    reminder: Reminder | None = None


def new_event(body: EventIn, source: str, scope: str = "personal") -> CalendarEvent:
    ev = CalendarEvent(title=body.title.strip(), description=body.description, location=body.location,
                       all_day=body.all_day, source=source, scope=scope)
    # 끝을 생략하면 종일은 하루, 시간 일정은 한 시간으로 본다.
    ev.start_at, ev.end_at = svc.to_range(body.start, body.end or body.start, body.all_day)
    if body.end is None and not body.all_day:
        ev.end_at = ev.start_at + timedelta(hours=1)
    svc.check_reminder(body.reminder, body.all_day, False)
    ev.reminder = body.reminder
    reminders.schedule(ev)
    return ev


# ---- 메모

@router.get("/memos")
async def list_memos(_: User, db: Db, q: str | None = None, tag: Annotated[list[str] | None, Query()] = None,
                     category: str | None = None, pinned: bool | None = None,
                     limit: int = Query(default=100, ge=1, le=300), offset: int = Query(default=0, ge=0)) -> dict[str, Any]:
    """tag 는 여러 번 줄 수 있고 모두 가진 메모만 남긴다. category 는 id 또는 none(미분류)."""
    rows = await db.scalars(svc.memo_query(q, tag, pinned, category_filter(category)).limit(limit).offset(offset))
    return {"memos": [svc.memo_out(m, body=False) for m in rows]}


@router.get("/memos/tags")
async def memo_tags(_: User, db: Db, category: str | None = None) -> dict[str, Any]:
    """category 를 주면 그 카테고리 메모의 태그만 센다(필터 칩이 지금 보는 카테고리에 맞게)."""
    # 태그는 최대 20개씩이라 메모 수가 많지 않은 개인 앱에서는 파이썬 집계로 충분하다.
    counts: dict[str, int] = {}
    stmt = select(Memo.tags).where(Memo.deleted_at.is_(None))
    cat = category_filter(category)
    if cat is svc.NONE:
        stmt = stmt.where(Memo.category_id.is_(None))
    elif cat is not None:
        stmt = stmt.where(Memo.category_id == cat)
    for tags in await db.scalars(stmt):
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return {"tags": [{"name": n, "count": c} for n, c in rows]}


@router.post("/memos", status_code=status.HTTP_201_CREATED)
async def create_memo(body: MemoIn, _: User, db: Db) -> dict[str, Any]:
    m = Memo(title=body.title.strip(), body=body.body, tags=svc.clean_tags(body.tags), pinned=body.pinned, source="user",
             category_id=(await svc.get_category(db, body.category_id)).id if body.category_id else None)
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
    if "category_id" in body.model_fields_set:
        m.category_id = (await svc.get_category(db, body.category_id)).id if body.category_id else None
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


# ---- 메모 카테고리

@router.get("/memo-categories")
async def list_categories(_: User, db: Db) -> dict[str, Any]:
    """순서대로. total·uncategorized 는 전체·미분류 칩의 개수다."""
    live = Memo.deleted_at.is_(None)
    counts = dict((await db.execute(select(Memo.category_id, func.count()).where(live).group_by(Memo.category_id))).all())
    return {"categories": [svc.category_out(c, counts.get(c.id, 0)) for c in await svc.list_categories(db)],
            "total": sum(counts.values()), "uncategorized": counts.get(None, 0)}


@router.post("/memo-categories", status_code=status.HTTP_201_CREATED)
async def create_category(body: CategoryIn, _: User, db: Db) -> dict[str, Any]:
    name = svc.clean_category_name(body.name)
    await svc.ensure_unique_name(db, name)
    last = await db.scalar(select(func.max(MemoCategory.position)))
    c = MemoCategory(name=name, color=body.color, position=(last or 0) + 1)
    db.add(c)
    await db.commit()
    return svc.category_out(c)


@router.put("/memo-categories/order")
async def order_categories(body: CategoryOrder, _: User, db: Db) -> dict[str, Any]:
    """보낸 순서대로 position 을 다시 매긴다. 빠진 카테고리는 뒤에 원래 순서로 붙인다."""
    cats = await svc.list_categories(db)
    rank = {cid: i for i, cid in enumerate(body.ids)}
    cats.sort(key=lambda c: (rank.get(c.id, len(rank)), c.position))
    for i, c in enumerate(cats, start=1):
        c.position = i
    await db.commit()
    return {"categories": [svc.category_out(c) for c in cats]}


@router.patch("/memo-categories/{category_id}")
async def patch_category(category_id: uuid.UUID, body: CategoryPatch, _: User, db: Db) -> dict[str, Any]:
    c = await svc.get_category(db, category_id)
    if body.name is not None:
        name = svc.clean_category_name(body.name)
        await svc.ensure_unique_name(db, name, own=c.id)
        c.name = name
    if "color" in body.model_fields_set:
        c.color = body.color
    await db.commit()
    return svc.category_out(c)


@router.delete("/memo-categories/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(category_id: uuid.UUID, _: User, db: Db) -> Response:
    """메모는 지우지 않고 미분류로 옮긴다. FK 의 SET NULL 과 같지만 rev 를 올려, 다른 기기에 열린 편집기가 지운 카테고리로
    되돌려 저장하지 않고 충돌로 알게 한다. updated_at 은 두어 목록 순서가 흔들리지 않게 한다."""
    c = await svc.get_category(db, category_id)
    await db.execute(update(Memo).where(Memo.category_id == c.id).values(category_id=None, rev=Memo.rev + 1))
    record(db, actor="user", action="memo_category.delete", target_type="memo_category", target_id=c.id)
    await db.delete(c)
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
