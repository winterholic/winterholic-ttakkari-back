"""날짜별 체크 기록과 고정 원본. 원본 수정·삭제는 이미 생성한 날짜 기록을 바꾸지 않는다."""
import uuid
from datetime import date

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.models import ChecklistItem, ChecklistTemplate, utcnow
from ttakkari.planner.service import KST


def today() -> date:
    return utcnow().astimezone(KST).date()


def item_out(item: ChecklistItem) -> dict:
    return {"id": str(item.id), "day": item.day.isoformat(), "title": item.title,
            "checked": item.checked, "template_id": str(item.template_id) if item.template_id else None}


def template_out(item: ChecklistTemplate) -> dict:
    return {"id": str(item.id), "title": item.title}


async def seed(db: AsyncSession, day: date) -> None:
    # 과거 기록을 현재의 원본으로 소급 생성하지 않는다. 새 고정 항목은 오늘부터 적용한다.
    if day != today():
        return
    templates = (await db.scalars(select(ChecklistTemplate).where(ChecklistTemplate.deleted_at.is_(None)))).all()
    if templates:
        await db.execute(insert(ChecklistItem).values([
            {"id": uuid.uuid4(), "day": day, "template_id": t.id, "title": t.title, "checked": False}
            for t in templates
        ]).on_conflict_do_nothing(index_elements=["day", "template_id"]))
    await db.commit()


async def daily(db: AsyncSession, day: date | None = None) -> dict:
    day = day or today()
    await seed(db, day)
    items = list(await db.scalars(select(ChecklistItem).where(ChecklistItem.day == day,
                     ChecklistItem.deleted_at.is_(None)).order_by(ChecklistItem.created_at, ChecklistItem.id)))
    return {"day": day.isoformat(), "items": [item_out(i) for i in items],
            "completed": sum(i.checked for i in items), "total": len(items)}


async def templates(db: AsyncSession) -> dict:
    items = await db.scalars(select(ChecklistTemplate).where(ChecklistTemplate.deleted_at.is_(None))
                            .order_by(ChecklistTemplate.created_at, ChecklistTemplate.id))
    return {"templates": [template_out(i) for i in items]}


async def create(db: AsyncSession, title: str, day: date | None = None, *, fixed: bool = False, actor: str = "user") -> dict:
    if fixed:
        item = ChecklistTemplate(title=title)
    else:
        day = day or today()
        await seed(db, day)
        item = ChecklistItem(title=title, day=day)
    db.add(item)
    await db.flush()
    record(db, actor=actor, action="checklist.create", target_type="checklist_template" if fixed else "checklist_item", target_id=item.id)
    await db.commit()
    await db.refresh(item)
    if fixed:
        await seed(db, today())
    return template_out(item) if fixed else item_out(item)


async def change(db: AsyncSession, id: uuid.UUID, patch: dict, *, fixed: bool = False, delete: bool = False, actor: str = "user") -> dict:
    cls = ChecklistTemplate if fixed else ChecklistItem
    item = await db.scalar(select(cls).where(cls.id == id).with_for_update())
    if item is None or item.deleted_at is not None:
        raise HTTPException(404, "체크리스트 항목이 없습니다.")
    if delete:
        item.deleted_at = utcnow()
    else:
        for key, value in patch.items():
            setattr(item, key, value)
    record(db, actor=actor, action="checklist.delete" if delete else "checklist.update",
           target_type="checklist_template" if fixed else "checklist_item", target_id=item.id, detail={"fields": list(patch)})
    await db.commit()
    await db.refresh(item)
    return template_out(item) if fixed else item_out(item)
