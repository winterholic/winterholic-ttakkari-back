"""하네스 조회 API. 소유자 로그인 전용이고 에이전트 세션 토큰으로는 들어올 수 없다(ADR 0069)."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ttakkari.api.deps import Db
from ttakkari.harness_view import service
from ttakkari.harness_view.model import CATEGORIES, ENGINES, SCOPES
from ttakkari.security.auth import User


async def user_access(user: User) -> None:
    pass


router = APIRouter(prefix="/api/harness", tags=["harness"], dependencies=[Depends(user_access)])

Engine = Literal[tuple(ENGINES)]  # type: ignore[valid-type]
Scope = Literal[tuple(SCOPES)]  # type: ignore[valid-type]


@router.get("")
async def overview(db: Db, refresh: bool = False) -> dict:
    return service.summary(await service.snapshot(db, refresh=refresh))


@router.get("/items")
async def items(db: Db, q: str = Query(default="", max_length=200),
                category: str | None = Query(default=None, max_length=300), engine: Engine | None = None,
                scope: Scope | None = None, active: bool | None = None, warned: bool = False,
                session: Literal["desk", "lead", "worker", "orchestrator"] | None = None, present: bool | None = None,
                group: str | None = Query(default=None, max_length=80),
                limit: int = Query(default=30, ge=1, le=service.PAGE_MAX), cursor: int = Query(default=0, ge=0)) -> dict:
    cats = {c for c in (category or "").split(",") if c}
    if cats - set(CATEGORIES):
        raise HTTPException(422, "알 수 없는 카테고리")
    snap = await service.snapshot(db)
    return service.search(snap, q=q, categories=cats or None, engine=engine, scope=scope, active=active, warned=warned,
                          session=session, present=present, group=group, limit=limit, cursor=cursor)


@router.get("/items/{item_id}")
async def item(item_id: str, db: Db, offset: int = Query(default=0, ge=0),
               length: int = Query(default=service.BODY_DEFAULT, ge=500, le=service.BODY_MAX)) -> dict:
    if len(item_id) != 14 or any(c not in "0123456789abcdef" for c in item_id):
        raise HTTPException(404, "항목이 없습니다.")
    found = service.detail(await service.snapshot(db), item_id, offset=offset, length=length)
    if found is None:
        raise HTTPException(404, "항목이 없습니다.")
    return found


@router.get("/layers")
async def layers(db: Db, session: Literal["desk", "lead", "worker", "orchestrator"],
                 engine: Literal["claude", "codex", "antigravity", "muse", "qwen-local"],
                 pool: Literal["personal", "work"] = "personal") -> dict:
    return await service.layers(db, await service.snapshot(db), session=session, engine=engine, pool=pool)
