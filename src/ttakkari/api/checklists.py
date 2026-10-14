"""사용자 UI와 세션 인증 MCP가 같은 체크리스트 서비스를 사용한다."""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ttakkari.api.deps import Db
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.planner import checklists as svc
from ttakkari.security.auth import User


class ItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    day: date | None = None

    @field_validator("title")
    @classmethod
    def clean_title(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("항목 이름을 입력해 주세요.")
        return v.strip()


class TemplateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=300)
    clean_title = field_validator("title")(ItemIn.clean_title.__func__)


class ItemPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str | None = Field(default=None, min_length=1, max_length=300)
    checked: bool | None = None

    @field_validator("title", "checked")
    @classmethod
    def non_null(cls, v):
        if v is None:
            raise ValueError("값을 비울 수 없습니다.")
        if isinstance(v, str):
            return ItemIn.clean_title(v)
        return v


async def user_access(user: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    if await db.get(ChatSession, sid) is None:
        raise HTTPException(403, "세션이 없습니다.")


def make_router(prefix: str, access) -> APIRouter:
    actor = "agent" if "/agent/" in prefix else "user"
    router = APIRouter(prefix=prefix, tags=["checklists"], dependencies=[Depends(access)])

    @router.get("")
    async def list_daily(db: Db, day: date | None = None) -> dict:
        return await svc.daily(db, day)

    @router.get("/templates")
    async def list_templates(db: Db) -> dict:
        return await svc.templates(db)

    @router.post("/items", status_code=201)
    async def create_item(body: ItemIn, db: Db) -> dict:
        return await svc.create(db, body.title, body.day, actor=actor)

    @router.patch("/items/{id}")
    async def patch_item(id: uuid.UUID, body: ItemPatch, db: Db) -> dict:
        return await svc.change(db, id, body.model_dump(exclude_unset=True), actor=actor)

    @router.delete("/items/{id}", status_code=204)
    async def delete_item(id: uuid.UUID, db: Db) -> Response:
        await svc.change(db, id, {}, delete=True, actor=actor)
        return Response(status_code=204)

    @router.post("/templates", status_code=201)
    async def create_template(body: TemplateIn, db: Db) -> dict:
        return await svc.create(db, body.title, fixed=True, actor=actor)

    @router.patch("/templates/{id}")
    async def patch_template(id: uuid.UUID, body: TemplateIn, db: Db) -> dict:
        return await svc.change(db, id, body.model_dump(), fixed=True, actor=actor)

    @router.delete("/templates/{id}", status_code=204)
    async def delete_template(id: uuid.UUID, db: Db) -> Response:
        await svc.change(db, id, {}, fixed=True, delete=True, actor=actor)
        return Response(status_code=204)

    return router


router = make_router("/api/checklists", user_access)
agent_router = make_router("/api/agent/checklists", agent_access)
