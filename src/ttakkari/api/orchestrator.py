"""오케스트레이터 MCP 서버가 부르는 API(ADR 0006). 토큰에 묶인 오케스트레이터 세션의 작업 세션만 다룬다."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from ttakkari.api.deps import Db
from ttakkari.api.pms import TaskIn, _project, _task_response
from ttakkari.api.sessions import ACTIVE, queue_run
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.models import (
    Artifact,
    ChatSession,
    PmsProject,
    PmsTask,
    Run,
    RunOrigin,
    SessionKind,
)
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.pms import service as pms_svc
from ttakkari.runs import context_usage
from ttakkari.runs import profiles as profiles_mod
from ttakkari.runs.account_status import account_status
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
    # 오케스트레이터와 같은 급(peer_models)을 작업 세션에 쓸 때만 필요하다.
    escalation_reason: str | None = Field(default=None, max_length=1000)


class MessageIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=100_000)
    # True 면 이 작업 세션의 엔진 대화를 버리고 새로 시작한다(컨텍스트 교체). 지시는 자기 완결적이어야 한다.
    fresh_context: bool = False


class WorkerOut(BaseModel):
    worker_id: uuid.UUID
    title: str
    engine: str
    model: str | None = None
    effort: str | None = None
    status: str
    context: str = ""
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
    out = WorkerOut(worker_id=w.id, title=w.title, engine=w.engine, model=w.model, effort=w.effort,
                    status=active.status if active else (done.status if done else "idle"),
                    active_run_id=active.id if active else None,
                    context=context_usage.describe(await context_usage.latest(db, w)))
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
    s = get_settings()
    default = s.worker_default_models.get(body.engine) or {}
    model = body.model or default.get("model")
    effort = body.effort or default.get("effort")
    engine = body.engine
    if engine != "auto" and model and profiles_mod.model_engine(model) not in (None, engine):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{model} 은(는) {engine} 엔진 모델이 아닙니다.")
    peer = (model or "").lower() in {m.lower() for m in s.peer_models}
    reason = (body.escalation_reason or "").strip()
    if peer and not reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{model} 은(는) 오케스트레이터와 같은 급이라 작업 세션에는 escalation_reason 이 필요합니다. "
                            "중요한 설계 결정·어려운 추론·새로운 발상이 필요한 부분만 맡기고, 아니면 sonnet 이나 gpt-6-luna 를 쓰세요.")
    w = ChatSession(workspace_id=orch.workspace_id, title=body.title, engine=engine, model=model,
                    effort=effort, kind=SessionKind.worker, parent_id=orch.id, pool=orch.pool)
    db.add(w)
    await db.flush()
    if peer:
        record(db, actor="agent", action="orchestrator.escalate", target_type="session", target_id=w.id,
               detail={"model": model, "effort": effort, "reason": reason[:500]})
    await queue_run(db, w, body.prompt, origin=RunOrigin.orchestrator)
    return await _out(db, w)


@router.post("/workers/{worker_id}/messages", response_model=WorkerOut, status_code=202)
async def message_worker(worker_id: uuid.UUID, body: MessageIn, sid: OrchestratorSession, db: Db) -> WorkerOut:
    await _orchestrator(db, sid)
    w = await _worker(db, sid, worker_id)
    if w.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "보관된 작업 세션입니다.")
    if body.fresh_context:
        await _reset(db, w)
    await queue_run(db, w, body.prompt, origin=RunOrigin.orchestrator)
    return await _out(db, w)


async def _reset(db: Db, sess: ChatSession) -> bool:
    """엔진 대화를 새로 시작하게 한다. 실행 중이면 그 Run 이 끝날 때 적용한다(True)."""
    running = await db.scalars(select(Run.id).where(Run.session_id == sess.id, Run.status.in_(ACTIVE)))
    if any(manager.is_active(rid) for rid in running):
        manager.request_context_reset(sess.id)
        return True
    context_usage.reset_engine_sessions(sess)
    await db.commit()
    return False


@router.get("/context")
async def my_context(sid: OrchestratorSession, db: Db) -> dict:
    orch = await _orchestrator(db, sid)
    ctx = await context_usage.latest(db, orch)
    return {"context": context_usage.describe(ctx), **(ctx or {})}


@router.post("/context/reset")
async def reset_my_context(sid: OrchestratorSession, db: Db) -> dict:
    """메인 오케스트레이터 자신의 대화를 다음 턴부터 새로 시작한다. 지금 턴은 그대로 끝난다."""
    orch = await _orchestrator(db, sid)
    deferred = await _reset(db, orch)
    record(db, actor="agent", action="orchestrator.context_reset", target_type="session", target_id=orch.id)
    await db.commit()
    return {"applies": "after this turn" if deferred else "next turn"}


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


@router.get("/accounts/usage")
async def accounts_usage(sid: OrchestratorSession, db: Db) -> dict:
    """계정별 배정 판단 정보. 회사 업무 오케스트레이터는 회사 그룹 계정만 본다."""
    orch = await _orchestrator(db, sid)
    return await account_status(db, orch.pool)


@router.get("/pms/projects")
async def pms_projects(sid: OrchestratorSession, db: Db, q: str | None = None) -> dict:
    """Claude 한도 대기 작업을 WBS 에 기록할 PMS 프로젝트를 찾는다."""
    await _orchestrator(db, sid)
    stmt = select(PmsProject).where(PmsProject.scope == "personal").order_by(PmsProject.updated_at.desc())
    if q:
        stmt = stmt.where(PmsProject.name.ilike(f"%{q.strip()}%"))
    rows = list(await db.scalars(stmt.limit(50)))
    return {"projects": [{"id": p.id, "name": p.name, "status": p.status} for p in rows]}


@router.post("/pms/projects/{pid}/tasks", status_code=201)
async def create_pms_task(pid: uuid.UUID, body: TaskIn, sid: OrchestratorSession, db: Db) -> dict:
    """작업자를 배정하지 않고 Claude 초기화 후 처리할 일을 PMS WBS 에 등록한다."""
    await _orchestrator(db, sid)
    project = await _project(db, pid)
    tasks = await pms_svc.load_tasks(db, pid)
    if len(tasks) >= pms_svc.MAX_TASKS:
        raise pms_svc.bad(f"작업은 프로젝트당 {pms_svc.MAX_TASKS}개까지예요.", "too_many")
    by_id = {t.id: t for t in tasks}
    parent_id = body.parent_id
    if body.after_id:
        sibling = by_id.get(body.after_id)
        if sibling is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "기준 작업이 없어요.")
        parent_id = sibling.parent_id
    elif parent_id and parent_id not in by_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "상위 작업이 없어요.")
    _, nodes = pms_svc.build(tasks)
    if parent_id and nodes[parent_id].depth + 1 >= pms_svc.MAX_DEPTH:
        raise pms_svc.bad(f"작업은 {pms_svc.MAX_DEPTH}단계까지만 나눌 수 있어요.", "too_deep")
    pms_svc.check_dates(body.start_date, body.end_date)
    task = PmsTask(project_id=pid, parent_id=parent_id, title=body.title.strip(), description=body.description,
                   priority=body.priority, assignee=body.assignee.strip() if body.assignee else None,
                   start_date=body.start_date, end_date=body.end_date, status="todo", progress=0)
    task.id = uuid.uuid4()
    task.predecessors = pms_svc.clean_predecessors(task, body.predecessors, tasks)
    siblings = pms_svc.siblings(tasks, parent_id)
    index = next((i + 1 for i, sibling in enumerate(siblings) if sibling.id == body.after_id), len(siblings))
    db.add(task)
    siblings.insert(index, task)
    pms_svc.resequence(siblings)
    await db.flush()
    await db.commit()
    return {"project": {"id": project.id, "name": project.name}, **await _task_response(db, task)}
