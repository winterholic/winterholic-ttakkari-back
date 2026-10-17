"""Owner UI and personal-session MCP share the same read-only source index."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ttakkari.api.deps import Db
from ttakkari.config import get_settings
from ttakkari.design_system import Browser, NotFound, Unavailable
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User

Kind = Literal["token", "document", "component", "example"]


def browser() -> Browser:
    return Browser(get_settings().design_system_root)


async def user_access(user: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    session = await db.get(ChatSession, sid)
    if session is None or session.pool == "work":
        raise HTTPException(403, "개인 세션에서만 개인 디자인시스템을 조회할 수 있습니다.")


def _read(fn, *args, **kwargs) -> dict:
    try:
        return fn(*args, **kwargs)
    except NotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except Unavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def make_router(prefix: str, access) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["design-systems"], dependencies=[Depends(access)])

    @router.get("")
    def catalog() -> dict:
        return _read(browser().catalog)

    @router.get("/{system}/items")
    def search(system: str, q: str = Query(default="", max_length=200), kind: Kind | None = None,
               limit: int = Query(default=20, ge=1, le=40), cursor: int = Query(default=0, ge=0),
               revision: str | None = Query(default=None, pattern=r"^[0-9a-f]{40}$")) -> dict:
        return _read(browser().search, system, query=q, kind=kind, limit=limit, cursor=cursor, revision=revision)

    @router.get("/{system}/item")
    def detail(system: str, item: str = Query(max_length=500), offset: int = Query(default=0, ge=0),
               length: int = Query(default=6000, ge=500, le=10000),
               revision: str | None = Query(default=None, pattern=r"^[0-9a-f]{40}$")) -> dict:
        return _read(browser().detail, system, item, offset=offset, length=length, revision=revision)

    return router


router = make_router("/api/design-systems", user_access)
agent_router = make_router("/api/agent/design-systems", agent_access)


@router.get("/{system}/preview", dependencies=[Depends(user_access)])
def preview(system: str, item: str | None = Query(default=None, max_length=500),
            theme: Literal["light", "dark"] = "light",
            revision: str | None = Query(default=None, pattern=r"^[0-9a-f]{40}$")) -> dict:
    """Owner-only visual payload; separate from the 16k MCP text contract."""
    return _read(browser().preview, system, item=item, theme=theme, revision=revision)
