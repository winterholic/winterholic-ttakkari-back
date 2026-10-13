"""새 연결은 PMS, 기존 맥락 프로젝트 연결은 이력을 읽기 위해 유지한다."""
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context import store
from ttakkari.models import CtxEntity, Meeting, PmsProject


async def get_project(db: AsyncSession, project_id: uuid.UUID | None):
    if project_id is None:
        return None
    return await db.get(PmsProject, project_id) or await db.get(CtxEntity, project_id)


async def find_project(db: AsyncSession, name: str, scope: str = "all"):
    stmt = select(PmsProject).where(func.lower(PmsProject.name) == name.strip().lower())
    if scope in ("work", "personal"):
        stmt = stmt.where(PmsProject.scope == scope)
    project = await db.scalar(stmt.order_by(PmsProject.created_at).limit(1))
    return project or await store.find_entity(db, name, "project", scope=scope)


def project_filter(project_id: uuid.UUID):
    return or_(Meeting.pms_project_id == project_id, Meeting.entity_id == project_id)
