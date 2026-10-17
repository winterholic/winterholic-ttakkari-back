"""운영 로그·이슈 API(ADR 0033). 사용자 UI(`/api/ops-logs`)와 세션 인증 MCP(`/api/agent/ops-logs`)가 같은 조회 함수를 쓴다."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from ttakkari.api.deps import Db
from ttakkari.models import ChatSession
from ttakkari.oplog import ingest, views
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User

Chars = Annotated[int | None, Query(ge=500, le=16000)]


class IssuePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["unresolved", "resolved", "ignored"] | None = None
    note: str | None = Field(default=None, max_length=500)
    release: str | None = Field(default=None, pattern=r"^[0-9a-f]{7,40}$")


async def user_access(user: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    session = await db.get(ChatSession, sid)
    if session is None:
        raise HTTPException(403, "세션이 없습니다.")
    if session.pool == "work":
        raise HTTPException(403, "회사 세션에서는 개인 서버의 운영 로그를 볼 수 없습니다.")


def _bad(exc: Exception) -> HTTPException:
    if isinstance(exc, views.NotFound):
        return HTTPException(404, str(exc))
    return HTTPException(422, str(exc))


def make_router(prefix: str, access) -> APIRouter:
    actor = "agent" if "/agent/" in prefix else "user"
    router = APIRouter(prefix=prefix, tags=["ops-logs"], dependencies=[Depends(access)])

    @router.get("/issues")
    async def issues(
        db: Db, status: str | None = "unresolved", source: str | None = None, level: str | None = None,
        q: str | None = Query(default=None, max_length=200), release: str | None = Query(default=None, max_length=40),
        tag: str | None = Query(default=None, max_length=100), since_hours: int | None = Query(default=None, ge=1, le=24 * 365),
        sort: str = "last_seen", limit: int = Query(default=10, ge=1, le=views.MAX_LIMIT), cursor: int = Query(default=0, ge=0),
        max_chars: Chars = None,
    ) -> dict:
        await ingest.ensure_fresh()
        try:
            return await views.list_issues(db, status=status, source=source, level=level, query=q, release=release, tag=tag,
                                           since_hours=since_hours, sort=sort, limit=limit, cursor=cursor, max_chars=max_chars)
        except ValueError as exc:
            raise _bad(exc) from exc

    @router.get("/issues/{issue}")
    async def issue(issue: str, db: Db, events: int = Query(default=3, ge=0, le=10), max_chars: Chars = None) -> dict:
        try:
            return await views.issue_detail(db, issue, events=events, max_chars=max_chars)
        except (ValueError, views.NotFound) as exc:
            raise _bad(exc) from exc

    @router.patch("/issues/{issue}")
    async def patch_issue(issue: str, body: IssuePatch, db: Db) -> dict:
        try:
            return await views.update_issue(db, issue, status=body.status, note=body.note, release=body.release, actor=actor)
        except (ValueError, views.NotFound) as exc:
            raise _bad(exc) from exc

    @router.get("/events")
    async def events(
        db: Db, q: str | None = Query(default=None, max_length=200), since_hours: int | None = Query(default=72, ge=1, le=24 * 365),
        until_hours: int | None = Query(default=None, ge=1, le=24 * 365), level: str | None = None, source: str | None = None,
        release: str | None = Query(default=None, max_length=40), issue: str | None = None, tag: str | None = Query(default=None, max_length=100),
        limit: int = Query(default=10, ge=1, le=views.MAX_LIMIT), cursor: int = Query(default=0, ge=0), max_chars: Chars = None,
    ) -> dict:
        await ingest.ensure_fresh()
        try:
            return await views.search_events(db, query=q, since_hours=since_hours, until_hours=until_hours, level=level, source=source,
                                             release=release, issue=issue, tag=tag, limit=limit, cursor=cursor, max_chars=max_chars)
        except ValueError as exc:
            raise _bad(exc) from exc

    @router.get("/trend")
    async def trend(db: Db, hours: int = Query(default=168, ge=1, le=720), unit: Literal["hour", "day"] = "hour",
                    issue: str | None = None) -> dict:
        await ingest.ensure_fresh()
        try:
            return await views.trend(db, hours=hours, unit=unit, issue=issue)
        except ValueError as exc:
            raise _bad(exc) from exc

    @router.get("/deploys")
    async def deploys(db: Db, limit: int = Query(default=10, ge=1, le=views.MAX_LIMIT), status: str | None = None,
                      max_chars: Chars = None) -> dict:
        await ingest.ensure_fresh()
        return await views.deploys(db, limit=limit, status=status, max_chars=max_chars)

    @router.get("/deploys/{ref}/log")
    async def deploy_log(ref: str, mode: Literal["summary", "errors", "tail", "range"] = "summary",
                         lines: int = Query(default=60, ge=1, le=200), offset: int | None = Query(default=None, ge=1),
                         max_chars: Chars = None) -> dict:
        try:
            return views.deploy_log(ref, mode, lines, offset, max_chars)
        except (ValueError, views.NotFound) as exc:
            raise _bad(exc) from exc

    @router.post("/sync")
    async def sync() -> dict:
        return {"result": await ingest.sync_all(force=True)}

    return router


router = make_router("/api/ops-logs", user_access)
agent_router = make_router("/api/agent/ops-logs", agent_access)
