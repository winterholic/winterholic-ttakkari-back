from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select, update

from ttakkari.api.deps import Db
from ttakkari.models import Notification, utcnow
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/notifications", tags=["notifications"])
Category = Literal["tasks", "assistant", "planner", "meetings", "system"]
CATEGORIES = ("tasks", "assistant", "planner", "meetings", "system")


class NotificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    category: Category
    title: str
    body: str
    url: str
    created_at: datetime
    read_at: datetime | None


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    next_before: int | None
    unread_counts: dict[str, int]


@router.get("", response_model=NotificationPage)
async def list_notifications(
    _: User, db: Db, category: Category | None = None, unread_only: bool = False,
    before: Annotated[int | None, Query(gt=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> NotificationPage:
    stmt = select(Notification)
    if category:
        stmt = stmt.where(Notification.category == category)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    if before is not None:
        stmt = stmt.where(Notification.id < before)
    rows = list(await db.scalars(stmt.order_by(Notification.id.desc()).limit(limit + 1)))
    counts = dict.fromkeys(CATEGORIES, 0)
    grouped = await db.execute(select(Notification.category, func.count()).where(
        Notification.read_at.is_(None)).group_by(Notification.category))
    counts.update(dict(grouped.all()))
    counts["all"] = sum(counts.values())
    return NotificationPage(items=[NotificationOut.model_validate(n) for n in rows[:limit]],
                            next_before=rows[limit - 1].id if len(rows) > limit else None, unread_counts=counts)


class ReadAllIn(BaseModel):
    category: Category | None = None
    # 이후 도착한 알림까지 읽음으로 바꾸지 않는다.
    through_id: int = Field(gt=0)


@router.post("/read-all")
async def read_all(body: ReadAllIn, _: User, db: Db) -> dict[str, int]:
    stmt = update(Notification).where(Notification.read_at.is_(None), Notification.id <= body.through_id)
    if body.category:
        stmt = stmt.where(Notification.category == body.category)
    result = await db.execute(stmt.values(read_at=utcnow()))
    await db.commit()
    return {"updated": result.rowcount}


@router.patch("/{notification_id}/read", response_model=NotificationOut)
async def read_notification(notification_id: int, _: User, db: Db) -> Notification:
    # 동시 요청에도 최초 읽은 시각을 유지한다.
    await db.execute(update(Notification).where(Notification.id == notification_id, Notification.read_at.is_(None))
                     .values(read_at=utcnow()))
    row = await db.get(Notification, notification_id)
    if row is None:
        raise HTTPException(404, "알림이 없습니다.")
    await db.commit()
    return row
