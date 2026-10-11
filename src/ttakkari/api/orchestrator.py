"""오케스트레이터 MCP 서버가 부르는 API(ADR 0006). 토큰에 묶인 오케스트레이터 세션의 작업 세션만 다룬다."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ttakkari.api.deps import Db
from ttakkari.api.sessions import ACTIVE, queue_run
from ttakkari.models import Artifact, ChatSession, Run, RunOrigin, SessionKind
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.runs.manager import manager

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])
RESULT_CHARS = 4000
MAX_ACTIVE_WORKERS = 20


class WorkerIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    prompt: str = Field(min_length=1, max_length=100_000)
    engine: Literal["auto", "claude", "codex"] = "auto"
    model: str | None = Field(default=None, max_length=100)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None


class MessageIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)


class WorkerOut(BaseModel):
    worker_id: uuid.UUID
    title: str
    engine: str
    status: str
    active_run_id: uuid.UUID | None = None
    last_result: str | None = None
    last_error: str | None = None
    artifacts: list[str] = Field(default_factory=list)


async def _orchestrator(db: Db, sid: uuid.UUID) -> ChatSession:
    s = await db.get(ChatSession, sid)
    if s is None or s.kind != SessionKind.orchestrator or s.archived_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "오케스트레이터 세션이 아닙니다.")
    return s


async def _worker(db: Db, sid: uuid.UUID, worker_id: uuid.UUID) -> ChatSession:
    w = await db.get(ChatSession, worker_id)
    if w is None or w.parent_id != sid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "이 오케스트레이터의 작업 세션이 아닙니다.")
    return w


async def _out(db: Db, w: ChatSession, detail: bool = False) -> WorkerOut:
    # 다른 태스크(매니저)가 방금 바꾼 상태를 읽어야 해서 identity map 의 낡은 값을 덮어쓴다.
    runs = list(await db.scalars(select(Run).where(Run.session_id == w.id).order_by(Run.created_at.desc()).limit(5)
                                 .execution_options(populate_existing=True)))
    active = next((r for r in runs if r.status in ACTIVE), None)
    done = next((r for r in runs if r.status not in ACTIVE), None)
    out = WorkerOut(worker_id=w.id, title=w.title, engine=w.engine,
                    status=active.status if active else (done.status if done else "idle"),
                    active_run_id=active.id if active else None)
    if done is not None:
        limit = RESULT_CHARS if detail else 300
        out.last_result = (done.result_text or "")[:limit] or None
        out.last_error = (done.error or "")[:limit] or None
        if detail:
            arts = await db.scalars(select(Artifact.filename).where(Artifact.run_id == done.id, Artifact.deleted_at.is_(None)))
            out.artifacts = list(arts)
    return out


@router.post("/workers", response_model=WorkerOut, status_code=201)
async def spawn_worker(body: WorkerIn, sid: OrchestratorSession, db: Db) -> WorkerOut:
    orch = await _orchestrator(db, sid)
    active = await db.scalar(
        select(func.count(func.distinct(ChatSession.id))).join(Run, Run.session_id == ChatSession.id)
        .where(ChatSession.parent_id == orch.id, Run.status.in_(ACTIVE)))
    if (active or 0) >= MAX_ACTIVE_WORKERS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"동시에 돌 수 있는 작업 세션은 {MAX_ACTIVE_WORKERS}개입니다.")
    w = ChatSession(workspace_id=orch.workspace_id, title=body.title, engine=body.engine, model=body.model,
                    effort=body.effort, kind=SessionKind.worker, parent_id=orch.id)
    db.add(w)
    await db.flush()
    await queue_run(db, w, body.prompt, origin=RunOrigin.orchestrator)
    return await _out(db, w)


@router.post("/workers/{worker_id}/messages", response_model=WorkerOut, status_code=202)
async def message_worker(worker_id: uuid.UUID, body: MessageIn, sid: OrchestratorSession, db: Db) -> WorkerOut:
    await _orchestrator(db, sid)
    w = await _worker(db, sid, worker_id)
    if w.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "보관된 작업 세션입니다.")
    await queue_run(db, w, body.prompt, origin=RunOrigin.orchestrator)
    return await _out(db, w)


@router.get("/workers", response_model=list[WorkerOut])
async def list_workers(sid: OrchestratorSession, db: Db) -> list[WorkerOut]:
    await _orchestrator(db, sid)
    rows = await db.scalars(select(ChatSession).where(ChatSession.parent_id == sid, ChatSession.archived_at.is_(None))
                            .order_by(ChatSession.created_at))
    return [await _out(db, w) for w in rows]


@router.get("/workers/{worker_id}", response_model=WorkerOut)
async def get_worker(worker_id: uuid.UUID, sid: OrchestratorSession, db: Db) -> WorkerOut:
    await _orchestrator(db, sid)
    return await _out(db, await _worker(db, sid, worker_id), detail=True)


@router.post("/workers/{worker_id}/stop", response_model=WorkerOut)
async def stop_worker(worker_id: uuid.UUID, sid: OrchestratorSession, db: Db) -> WorkerOut:
    await _orchestrator(db, sid)
    w = await _worker(db, sid, worker_id)
    runs = list(await db.scalars(select(Run).where(Run.session_id == w.id, Run.status.in_(ACTIVE))))
    # 대기 중인 것부터 지워야 실행 중인 것이 끝나는 순간 다음 것이 시작되지 않는다.
    running = [r for r in runs if manager.is_active(r.id)]
    for run in runs:
        if run not in running:
            await manager.cancel_queued(db, run)
    await db.commit()
    for run in running:
        await manager.cancel(run.id)
    # 종료 기록은 실행 태스크가 한다. 잠깐 기다려 최신 상태를 돌려준다.
    for _ in range(40):
        if not await db.scalar(select(Run.id).where(Run.session_id == w.id, Run.status.in_(ACTIVE)).limit(1)):
            break
        await asyncio.sleep(0.25)
    return await _out(db, w)
