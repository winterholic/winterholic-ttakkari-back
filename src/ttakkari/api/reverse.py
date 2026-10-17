"""리버스 엔지니어링 API. 소유자 화면과 개인 세션 MCP가 같은 작업 기록을 쓴다."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ttakkari import reverse
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User, client_ip


class StartIn(BaseModel):
    target: str = Field(max_length=500)


def _http(exc: reverse.ReverseError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message})


async def user_access(user: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db) -> None:
    session = await db.get(ChatSession, sid)
    if session is None or session.pool == "work":
        raise HTTPException(403, "개인 세션에서만 리버스 엔지니어링을 요청할 수 있습니다.")


def make_router(prefix: str, access, *, start_path: str, actor: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["reverse"], dependencies=[Depends(access)])

    @router.post(start_path, status_code=202)
    async def start(body: StartIn, request: Request, db: Db) -> dict:
        try:
            job = await reverse.start_analysis(body.target, db)
        except reverse.ReverseError as exc:
            raise _http(exc) from exc
        record(db, action="reverse.start", actor=actor, target_type="reverse_job", target_id=job["id"],
               detail={"target": job["target"]}, ip=client_ip(request))
        await db.commit()
        return job

    @router.get("")
    async def listing(db: Db) -> dict:
        return await reverse.list_jobs(db)

    @router.get("/{job_id}")
    async def detail(job_id: str, db: Db, response: Response) -> dict:
        response.headers["Cache-Control"] = "no-store"
        try:
            return await reverse.job_detail(job_id, db)
        except reverse.ReverseError as exc:
            raise _http(exc) from exc

    @router.post("/{job_id}/retry", status_code=202)
    async def retry(job_id: str, request: Request, db: Db) -> dict:
        try:
            job = await reverse.retry_job(job_id, db)
        except reverse.ReverseError as exc:
            raise _http(exc) from exc
        record(db, action="reverse.retry", actor=actor, target_type="reverse_job", target_id=job["id"],
               ip=client_ip(request))
        await db.commit()
        return job

    return router


router = make_router("/api/reverse", user_access, start_path="", actor="user")
agent_router = make_router("/api/agent/reverse", agent_access, start_path="/start", actor="agent")


@router.delete("/{job_id}")
async def remove(job_id: str, request: Request, db: Db) -> dict:
    try:
        out = await reverse.remove_job(job_id, db)
    except reverse.ReverseError as exc:
        raise _http(exc) from exc
    record(db, action="reverse.delete", target_type="reverse_job", target_id=job_id,
           ip=client_ip(request))
    await db.commit()
    return out
