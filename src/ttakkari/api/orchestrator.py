"""오케스트레이터 MCP 서버가 부르는 API(ADR 0006). 토큰에 묶인 오케스트레이터 세션의 작업 세션만 다룬다."""

from __future__ import annotations

import asyncio
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select

from ttakkari.api.deps import Db
from ttakkari.api.pms import TaskIn, _project, _task_response, check_project_scope
from ttakkari.api.sessions import ACTIVE, queue_run
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.model_catalog import ANTIGRAVITY_MODELS, thinking_level
from ttakkari.models import (
    Artifact,
    ChatSession,
    PmsProject,
    PmsTask,
    Run,
    RunEvent,
    RunOrigin,
    RunStatus,
    SessionKind,
)
from ttakkari.orchestrator.runtime import OrchestratorSession
from ttakkari.pms import service as pms_svc
from ttakkari.runs import context_usage, events, luna_benchmark, worker_routing
from ttakkari.runs import profiles as profiles_mod
from ttakkari.runs.account_status import account_status
from ttakkari.runs.manager import manager

router = APIRouter(prefix="/api/orchestrator", tags=["orchestrator"])
RESULT_CHARS = 4000
MAX_ACTIVE_WORKERS = 20


class QwenFitIn(BaseModel):
    """평시 병렬-fit 판정용 선언. 다 채워야 Qwen 후보가 된다(모르면 탈락, fail closed).

    needs_mcp: MCP 도구(위임·조회)가 필요하면 True. Qwen 하네스에는 MCP가 없어
    파일 읽기·편집·테스트 명령만으로 끝나야 False다.
    narrow_scope: 좁고 자기완결적인 작업이면 True.
    verifiable: 정해진 검사·테스트로 결과를 확인할 수 있으면 True.
    """
    needs_mcp: bool | None = None
    narrow_scope: bool | None = None
    verifiable: bool | None = None


class WorkerIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    prompt: str = Field(min_length=1, max_length=100_000)
    engine: Literal["auto", "claude", "codex", "antigravity", "muse", "qwen-local"] = "auto"
    model: str | None = Field(default=None, max_length=100)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    task_type: worker_routing.TaskType | None = None
    strict_engine: bool = False
    qwen_fit: QwenFitIn | None = None
    # 게임 스튜디오 프로젝트에 대한 작업이면 게임 ID 를 준다. 작업 세션은 그 게임 저장소 workspace 에서 돌고 prompt 에 게임·빌드·잠금 결정이 붙는다.
    game_id: uuid.UUID | None = None
    game_build_id: uuid.UUID | None = None
    # 오케스트레이터와 같은 급(peer_models)을 작업 세션에 쓸 때만 필요하다.
    escalation_reason: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def antigravity_model_policy(self):
        if self.engine == "antigravity":
            if self.model is not None and self.model not in ANTIGRAVITY_MODELS:
                raise ValueError("Choose an available Antigravity CLI model from account_usage.")
            if self.effort not in (None, "low", "medium", "high"):
                raise ValueError("Antigravity models support low, medium, or high effort only.")
            selected = self.model or get_settings().worker_default_models["antigravity"]["model"]
            if self.effort and self.effort != thinking_level(selected):
                raise ValueError("Antigravity effort must match the selected model's thinking level.")
        elif self.engine == "muse" and self.model is not None:
            if self.model not in get_settings().engine_models.get("muse", []):
                raise ValueError("Choose a documented Muse Code coding model; model access requires separate verification.")
        elif self.engine == "auto" and self.model is not None:
            if self.model not in {m for models in get_settings().engine_models.values() for m in models}:
                raise ValueError("Choose an available worker model from account_usage.")
            if self.model in ANTIGRAVITY_MODELS and self.effort and self.effort != thinking_level(self.model):
                raise ValueError("Antigravity effort must match the selected model's thinking level.")
        return self


class MessageIn(BaseModel):
    prompt: str = Field(default="", max_length=100_000)
    # True 면 이 작업 세션의 엔진 대화를 버리고 새로 시작한다(컨텍스트 교체). 지시는 자기 완결적이어야 한다.
    fresh_context: bool = False
    reroute: bool = False
    task_type: worker_routing.TaskType | None = None
    strict_engine: bool | None = None
    escalation_reason: str | None = Field(default=None, max_length=1000)
    verification_outcome: Literal["passed", "failed"] | None = None
    verification_note: str | None = Field(default=None, max_length=2000)
    verifier_worker_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def require_prompt_unless_verification_only(self):
        if not self.prompt.strip() and self.verification_outcome is None:
            raise ValueError("prompt is required unless recording verification_outcome.")
        return self


class VerificationIn(BaseModel):
    outcome: Literal["passed", "failed"]
    note: str | None = Field(default=None, max_length=2000)
    verifier_worker_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None


class WorkerOut(BaseModel):
    worker_id: uuid.UUID
    title: str
    engine: str
    model: str | None = None
    effort: str | None = None
    task_type: str | None = None
    strict_engine: bool = False
    routing: dict | None = None
    verification_required: bool = False
    verification_status: str | None = None
    false_completion: bool | None = None
    status: str
    context: str = ""
    context_usage: dict | None = None
    active_run_id: uuid.UUID | None = None
    waiting_limit: bool = False
    waiting_reason: str | None = None
    last_progress: str | None = None
    is_stuck: bool = False
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
    ctx = await context_usage.latest(db, w)
    out = WorkerOut(worker_id=w.id, title=w.title, engine=w.engine, model=w.model, effort=w.effort,
                    task_type=w.worker_task_type, strict_engine=w.strict_engine,
                    status=active.status if active else (done.status if done else "idle"),
                    active_run_id=active.id if active else None,
                    context=context_usage.describe(ctx), context_usage=ctx)
    if active:
        from ttakkari.api.desk_feedback import progress_label, quota_wait_reason
        if active.status == RunStatus.queued:
            latest_event = await db.scalar(select(RunEvent).where(RunEvent.run_id == active.id)
                                           .order_by(RunEvent.seq.desc()).limit(1))
            out.waiting_reason = quota_wait_reason(active, latest_event)
            out.waiting_limit = out.waiting_reason is not None
        elif active.status == RunStatus.running:
            from ttakkari.runs.manager import manager
            out.is_stuck = bool(active.pid is None and active.id not in manager._handles)

        prog_ev = await db.scalar(select(RunEvent).where(
            RunEvent.run_id == active.id,
            RunEvent.type.in_(("tool.result", "tool.call", "message", "run.diff", "checkpoint"))
        ).order_by(RunEvent.seq.desc()).limit(1))
        out.last_progress = progress_label(prog_ev)
    if runs:
        out.routing = await db.scalar(select(RunEvent.payload).where(
            RunEvent.run_id == runs[0].id, RunEvent.type == "worker.routing").order_by(RunEvent.seq.desc()).limit(1))
    usage_info = (done.usage or {}) if done is not None else {}
    out.verification_required = bool(
        usage_info.get("verification_required")
        or (out.routing or {}).get("verification_required")
        or worker_routing.needs_independent_verification(w.worker_task_type, (done.model if done else None) or w.model)
    )
    out.verification_status = usage_info.get("verification_status") or (
        "pending" if out.verification_required else None
    )
    out.false_completion = usage_info.get("false_completion")
    if done is not None:
        limit = RESULT_CHARS if detail else 300
        out.last_result = (done.result_text or "")[:limit] or None
        out.last_error = (done.error or "")[:limit] or None
        if detail:
            arts = await db.scalars(select(Artifact.filename).where(Artifact.run_id == done.id, Artifact.deleted_at.is_(None)))
            out.artifacts = list(arts)
    return out


async def _record_verification(
    db: Db, w: ChatSession, outcome: Literal["passed", "failed"],
    note: str | None = None, verifier_worker_id: uuid.UUID | None = None,
    run_id: uuid.UUID | None = None,
) -> Run:
    if run_id is not None:
        target = await db.get(Run, run_id)
        if target is None or target.session_id != w.id or target.status in ACTIVE:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "대상 완료 Run을 찾을 수 없습니다.")
    else:
        target = await db.scalar(
            select(Run).where(Run.session_id == w.id, Run.status.not_in(ACTIVE))
            .order_by(Run.created_at.desc()).limit(1)
        )
        if target is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "검증 결과를 기록할 완료된 Run이 없습니다.")
    usage_data = dict(target.usage or {})
    selected_model = usage_data.get("selected_model") or target.model or w.model
    task_type = usage_data.get("task_type") or w.worker_task_type or worker_routing.infer_task_type(selected_model)
    usage_data.update({
        "task_type": task_type,
        "selected_engine": usage_data.get("selected_engine") or target.engine or w.engine,
        "selected_model": selected_model,
        "verification_required": usage_data.get(
            "verification_required",
            worker_routing.needs_independent_verification(task_type, selected_model),
        ),
        "verification_status": outcome,
        "false_completion": outcome == "failed",
        "verification_note": (note or "")[:2000] or None,
        "verifier_worker_id": str(verifier_worker_id) if verifier_worker_id else None,
    })
    target.usage = usage_data
    events.append(db, target, "worker.verification", {
        "worker_id": str(w.id),
        "run_id": str(target.id),
        "task_type": task_type,
        "engine": usage_data["selected_engine"],
        "model": selected_model,
        "outcome": outcome,
        "false_completion": outcome == "failed",
        "note": usage_data["verification_note"],
        "verifier_worker_id": usage_data["verifier_worker_id"],
    })
    record(db, actor="agent", action="orchestrator.verify_worker", target_type="run", target_id=target.id,
           detail={"worker_id": str(w.id), "outcome": outcome, "false_completion": outcome == "failed",
                   "model": selected_model, "task_type": task_type})
    return target


async def _game_worker_context(db: Db, orch: ChatSession, body: WorkerIn, task_type: str):
    """게임 작업이면 worker 를 게임 저장소 workspace 에서 돌리고 prompt 에 게임·빌드·잠금 결정을 붙인다(TASK-G08). 별도 큐는 없다."""
    from ttakkari.games import worker as game_worker
    from ttakkari.games.contract import GameContractError
    from ttakkari.models import GameProject
    if orch.pool != "personal":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "게임 스튜디오 프로젝트는 개인 모드 오케스트레이터만 다룰 수 있습니다.")
    game = await db.get(GameProject, body.game_id)
    if game is None or game.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "게임 프로젝트를 찾을 수 없습니다.")
    try:
        ctx = await game_worker.load_context(db, game, body.game_build_id)
    except GameContractError as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND if e.code in {"no_build", "no_workspace"} else status.HTTP_422_UNPROCESSABLE_ENTITY, str(e)) from e
    kind = "verify" if task_type == "verification" else "modify"

    def tag(run: Run) -> None:
        run.usage = {**(run.usage or {}), game_worker.USAGE_KEY: game_worker.run_tag(ctx, kind)}
    return ctx.workspace.id, game_worker.compose_prompt(ctx, kind, body.prompt), tag


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
    task_type = body.task_type or worker_routing.infer_task_type(body.model)
    if body.engine == "auto" and body.model and not worker_routing.accepts_model(task_type, body.model):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Model is outside the task_type policy; choose an explicit engine to pin it.")
    model = body.model or (worker_routing.ROUTES[task_type][0][0][1] if body.engine == "auto" else default.get("model"))
    engine = body.engine
    model_engine = profiles_mod.model_engine(model)
    effective_engine = model_engine or engine
    effort = worker_routing.effort(
        profiles_mod.Profile(uuid.uuid4(), "routing", effective_engine, model=model),
        body.effort or default.get("effort"),
    )
    if engine != "auto" and model and profiles_mod.model_engine(model) not in (None, engine):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{model} 은(는) {engine} 엔진 모델이 아닙니다.")
    if engine == "qwen-local" and model != "Qwen3.8-Flash-Next-Q2_K":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "qwen-local은 로컬 Qwen3.8 Q2_K 모델만 지원합니다.")
    if engine == "antigravity" and (model not in s.engine_models.get(engine, []) or
                                    (effort is not None and effort not in {"low", "medium", "high"})):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "account_usage에 표시된 Antigravity 모델과 low/medium/high effort를 선택하세요.")
    peer = task_type == "reasoning" or (model or "").lower() in {m.lower() for m in s.peer_models}
    if body.task_type and task_type != "reasoning" and model in s.peer_models:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Peer models require task_type=reasoning.")
    reason = (body.escalation_reason or "").strip()
    if peer and not reason:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"{model} 은(는) 오케스트레이터와 같은 급이라 작업 세션에는 escalation_reason 이 필요합니다. "
                            "중요한 설계 결정·어려운 추론·새로운 발상이 필요한 부분만 맡기고, 아니면 sonnet 이나 gpt-6-luna 를 쓰세요.")
    workspace_id, prompt, on_queued = orch.workspace_id, body.prompt, None
    if body.game_id is not None:
        workspace_id, prompt, on_queued = await _game_worker_context(db, orch, body, task_type)
    w = ChatSession(workspace_id=workspace_id, title=body.title, engine=engine, model=model,
                    effort=effort, kind=SessionKind.worker, parent_id=orch.id, pool=orch.pool,
                    worker_task_type=task_type, strict_engine=body.strict_engine)
    db.add(w)
    await db.flush()
    if peer:
        record(db, actor="agent", action="orchestrator.escalate", target_type="session", target_id=w.id,
               detail={"model": model, "effort": effort, "reason": reason[:500]})
    fit = body.qwen_fit

    def _stash_fit(run) -> None:
        if on_queued is not None:
            on_queued(run)
        if fit is not None:
            run.usage = {**(run.usage or {}), "qwen_fit": fit.model_dump()}

    await queue_run(db, w, prompt, origin=RunOrigin.orchestrator, on_queued=_stash_fit)
    return await _out(db, w)


@router.post("/workers/{worker_id}/messages", response_model=WorkerOut, status_code=202)
async def message_worker(worker_id: uuid.UUID, body: MessageIn, sid: OrchestratorSession, db: Db) -> WorkerOut:
    await _orchestrator(db, sid)
    w = await _worker(db, sid, worker_id)
    if w.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "보관된 작업 세션입니다.")
    if body.task_type and not body.reroute:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Changing task_type requires reroute=True.")
    if body.strict_engine is not None:
        w.strict_engine = body.strict_engine
    if body.verification_outcome is not None:
        await _record_verification(db, w, body.verification_outcome, body.verification_note, body.verifier_worker_id)
    if body.reroute:
        # Never alter the engine policy under a running/queued Run or silently discard its work.
        await db.refresh(w, with_for_update=True)
        if await db.scalar(select(Run.id).where(Run.session_id == w.id, Run.status.in_(ACTIVE)).limit(1)):
            raise HTTPException(status.HTTP_409_CONFLICT, "Finish or cancel the active Run after inspecting its progress before rerouting.")
        task_type = body.task_type or w.worker_task_type or worker_routing.infer_task_type(w.model)
        if task_type == "reasoning" and not (body.escalation_reason or "").strip():
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "reasoning requires escalation_reason.")
        # Preserve legacy provider conversations under the model they actually used.
        previous = list(await db.scalars(select(Run).where(Run.session_id == w.id).order_by(Run.created_at.desc())))
        saved = dict(w.engine_sessions or {})
        for account in await profiles_mod.accounts(db):
            old = saved.get(str(account.id))
            last = next((r for r in previous if r.profile == account.name), None)
            old_model = (last.model if last else None) or w.model
            if old_model and profiles_mod.model_engine(old_model) != account.engine:
                # Legacy Run.model retained the requested model even after a native engine fallback.
                counterpart = get_settings().model_counterparts.get(old_model.lower())
                old_model = counterpart if profiles_mod.model_engine(counterpart) == account.engine else None
            if old and old_model:
                saved.setdefault(f"{account.id}:{old_model}", old)
        w.engine_sessions = saved
        w.engine, w.worker_task_type = "auto", task_type
        route_engine, route_model = worker_routing.ROUTES[task_type][0][0]
        w.model = route_model
        w.effort = worker_routing.effort(
            profiles_mod.Profile(uuid.uuid4(), "routing", route_engine, model=route_model), "high",
        )
        record(db, actor="agent", action="orchestrator.reroute", target_type="session", target_id=w.id,
               detail={"task_type": task_type, "reason": (body.escalation_reason or "")[:500]})
    if body.fresh_context:
        await _reset(db, w)
    if body.prompt.strip():
        await queue_run(db, w, body.prompt, origin=RunOrigin.orchestrator)
    else:
        await db.commit()
    return await _out(db, w)


@router.post("/workers/{worker_id}/verification", response_model=WorkerOut)
async def verify_worker(worker_id: uuid.UUID, body: VerificationIn, sid: OrchestratorSession, db: Db) -> WorkerOut:
    await _orchestrator(db, sid)
    w = await _worker(db, sid, worker_id)
    await _record_verification(db, w, body.outcome, body.note, body.verifier_worker_id, body.run_id)
    await db.commit()
    return await _out(db, w, detail=True)


@router.get("/workers/metrics")
async def worker_metrics(sid: OrchestratorSession, db: Db) -> dict:
    orch = await _orchestrator(db, sid)
    rows = list(await db.execute(
        select(Run, ChatSession.worker_task_type)
        .join(ChatSession, ChatSession.id == Run.session_id)
        .where(ChatSession.kind == SessionKind.worker, ChatSession.pool == orch.pool, Run.status.not_in(ACTIVE))
        .order_by(Run.created_at.desc())
        .limit(500)
    ))
    by_model_task: dict[str, dict] = {}
    for run, sess_task in rows:
        u = run.usage or {}
        model = u.get("selected_model") or run.model or "unknown"
        task = u.get("task_type") or sess_task or "general"
        key = f"{model}:{task}"
        bucket = by_model_task.setdefault(key, {
            "model": model, "engine": u.get("selected_engine") or run.engine, "task_type": task,
            "total_runs": 0, "verification_required_runs": 0,
            "verified_passed_runs": 0, "false_completion_runs": 0, "unverified_runs": 0,
        })
        bucket["total_runs"] += 1
        if u.get("verification_required"):
            bucket["verification_required_runs"] += 1
        v_status = u.get("verification_status")
        if v_status == "passed":
            bucket["verified_passed_runs"] += 1
        elif v_status == "failed" or u.get("false_completion") is True:
            bucket["false_completion_runs"] += 1
        elif u.get("verification_required"):
            bucket["unverified_runs"] += 1
    return {"pool": orch.pool, "metrics": list(by_model_task.values())}


async def _reset(db: Db, sess: ChatSession) -> bool:
    """엔진 대화를 새로 시작하게 한다. 실행 중이면 그 Run 이 끝날 때 적용한다(True)."""
    running = await db.scalars(select(Run.id).where(Run.session_id == sess.id, Run.status.in_(ACTIVE)))
    if any([await manager.is_owned(rid) for rid in running]):
        await manager.request_context_reset(sess.id)
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
    running = [r for r in runs if await manager.is_owned(r.id)]
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


@router.post("/luna-common30-v2", status_code=status.HTTP_202_ACCEPTED)
async def start_luna_benchmark(sid: OrchestratorSession, db: Db) -> dict:
    orch = await _orchestrator(db, sid)
    if orch.pool != "personal":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "개인 계정 벤치는 개인 오케스트레이터에서만 실행할 수 있습니다.")
    try:
        job = await luna_benchmark.start(str(sid), db)
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return {"job_id": str(job.id), "status": job.status, "contract": luna_benchmark.contract()}


@router.get("/luna-common30-v2/{job_id}")
async def get_luna_benchmark(job_id: uuid.UUID, sid: OrchestratorSession, db: Db,
                             offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=20)) -> dict:
    await _orchestrator(db, sid)
    job = luna_benchmark.get_job(job_id, str(sid))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "평가 작업을 찾을 수 없습니다.")
    return job.view(offset, limit)


@router.delete("/luna-common30-v2/{job_id}")
async def cancel_luna_benchmark(job_id: uuid.UUID, sid: OrchestratorSession, db: Db) -> dict:
    await _orchestrator(db, sid)
    job = luna_benchmark.get_job(job_id, str(sid))
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "평가 작업을 찾을 수 없습니다.")
    await luna_benchmark.cancel(job)
    return job.view()


@router.get("/pms/projects")
async def pms_projects(sid: OrchestratorSession, db: Db, q: str | None = None) -> dict:
    """Claude 한도 대기 작업을 WBS 에 기록할 PMS 프로젝트를 찾는다."""
    orch = await _orchestrator(db, sid)
    scope = "work" if orch.pool == "work" else "personal"
    stmt = select(PmsProject).where(PmsProject.scope == scope).order_by(PmsProject.updated_at.desc())
    if q:
        stmt = stmt.where(PmsProject.name.ilike(f"%{q.strip()}%"))
    rows = list(await db.scalars(stmt.limit(50)))
    return {"projects": [{"id": p.id, "name": p.name, "status": p.status} for p in rows]}


@router.post("/pms/projects/{pid}/tasks", status_code=201)
async def create_pms_task(pid: uuid.UUID, body: TaskIn, sid: OrchestratorSession, db: Db) -> dict:
    """작업자를 배정하지 않고 Claude 초기화 후 처리할 일을 PMS WBS 에 등록한다."""
    orch = await _orchestrator(db, sid)
    project = await _project(db, pid)
    check_project_scope(project, orch.pool)
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
