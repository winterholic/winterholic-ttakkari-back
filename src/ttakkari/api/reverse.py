"""리버스 엔지니어링 API. 소유자 화면과 세션 MCP가 같은 작업 기록을 쓴다."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator, model_validator

from ttakkari import reverse
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.security.auth import User, client_ip


class StartIn(BaseModel):
    description: str = Field(default="", max_length=40_000)
    target: str | None = Field(default=None, max_length=40_000)
    workspace_id: uuid.UUID | None = None
    title: str | None = Field(default=None, max_length=200)
    target_type: str = "auto"
    mode: str = "auto"
    references: list[str] = Field(default_factory=list, max_length=30)
    max_repair_attempts: int = Field(default=0, ge=0, le=3)

    @field_validator("target_type")
    @classmethod
    def valid_target_type(cls, value: str) -> str:
        if value not in {"auto", "cli", "web", "api", "source"}:
            raise ValueError("target_type은 auto, cli, web, api, source 중 하나여야 합니다.")
        return value

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, value: str) -> str:
        if value not in {"auto", "analyze", "reconstruct", "analyze_reconstruct"}:
            raise ValueError("mode는 auto, analyze, reconstruct, analyze_reconstruct 중 하나여야 합니다.")
        return value

    @model_validator(mode="after")
    def resolve_description(self) -> StartIn:
        desc = (self.description or "").strip() or (self.target or "").strip()
        if not desc:
            raise ValueError("분석할 대상과 재현하고 싶은 동작을 설명해 주세요.")
        self.description = desc
        return self


class MessageIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=40_000)


class UpdateIn(BaseModel):
    title: str | None = Field(default=None, max_length=200)
    target_type: str | None = None
    mode: str | None = None
    references: list[str] | None = Field(default=None, max_length=30)
    max_repair_attempts: int | None = Field(default=None, ge=0, le=3)

    @field_validator("target_type")
    @classmethod
    def valid_target_type(cls, value: str | None) -> str | None:
        if value is not None and value not in {"auto", "cli", "web", "api", "source"}:
            raise ValueError("target_type은 auto, cli, web, api, source 중 하나여야 합니다.")
        return value

    @field_validator("mode")
    @classmethod
    def valid_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in {"auto", "analyze", "reconstruct", "analyze_reconstruct"}:
            raise ValueError("지원하지 않는 실행 모드입니다.")
        return value


def _http(exc: reverse.ReverseError) -> HTTPException:
    return HTTPException(exc.status, {"code": exc.code, "message": exc.message})


async def user_access(user: User) -> None:
    pass


async def agent_access(sid: OrchestratorSession, db: Db):
    session = await db.get(ChatSession, sid)
    if session is None:
        raise HTTPException(403, "세션이 없습니다.")
    return session


def make_router(prefix: str, access, *, start_path: str, actor: str) -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["reverse"], dependencies=[Depends(access)])
    caller_access = Depends(access)

    @router.post(start_path, status_code=202)
    async def start(body: StartIn, request: Request, db: Db, caller=caller_access) -> dict:
        try:
            job = await reverse.start_analysis(body.description, db, body.workspace_id,
                pool=getattr(caller, "pool", None), title=body.title,
                target_type=body.target_type, mode=body.mode, references=body.references,
                max_repair_attempts=body.max_repair_attempts)
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

    @router.patch("/{job_id}", status_code=202)
    async def update(job_id: uuid.UUID, body: UpdateIn, request: Request, db: Db) -> dict:
        from ttakkari.models import ReverseJob
        from ttakkari.reverse_tasks import update_configuration
        job = await db.get(ReverseJob, job_id)
        if job is None:
            raise HTTPException(404, "작업을 찾을 수 없습니다.")
        try:
            out = await update_configuration(db, job, **body.model_dump(exclude_unset=True))
        except reverse.ReverseError as exc:
            raise _http(exc) from exc
        record(db, action="reverse.update", actor=actor, target_type="reverse_job", target_id=job_id,
               detail=body.model_dump(exclude_unset=True), ip=client_ip(request))
        await db.commit()
        return out

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

    @router.post("/{job_id}/messages", status_code=202)
    async def message(job_id: uuid.UUID, body: MessageIn, request: Request, db: Db) -> dict:
        from ttakkari.models import ReverseJob
        from ttakkari.reverse_tasks import follow_up
        job = await db.get(ReverseJob, job_id)
        if job is None:
            raise HTTPException(404, "작업을 찾을 수 없습니다.")
        try:
            out = await follow_up(db, job, body.prompt)
        except reverse.ReverseError as exc:
            raise _http(exc) from exc
        record(db, action="reverse.message", actor=actor, target_type="reverse_job", target_id=job_id,
               ip=client_ip(request))
        await db.commit()
        return out

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
