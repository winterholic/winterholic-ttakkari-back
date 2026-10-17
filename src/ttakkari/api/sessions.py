from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from ttakkari.api import desk_feedback
from ttakkari.api.deps import Db, get_session, get_workspace
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ArtcraftTask,
    ChatSession,
    Run,
    RunEvent,
    RunOrigin,
    RunStatus,
    SessionKind,
    Workspace,
    utcnow,
)
from ttakkari.runs import events
from ttakkari.runs.manager import manager
from ttakkari.schemas import (
    FailedRunOut,
    FailedRunsPage,
    RunEventOut,
    RunIn,
    RunOut,
    SessionIn,
    SessionOut,
    SessionPatch,
)
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api", tags=["sessions"])
ACTIVE = [RunStatus.queued, RunStatus.running]
SSE_PING_SECONDS = 15
EVENT_PAGE = 500


async def _session_out(db: Db, s: ChatSession) -> SessionOut:
    out = SessionOut.model_validate(s)
    last = await db.scalar(select(Run).where(Run.session_id == s.id).order_by(Run.created_at.desc()).limit(1))
    if last is not None:
        out.last_run_status = last.status
        out.last_run_profile = last.profile
        if last.status in ACTIVE:
            out.active_run_id = last.id
            out.last_run_started_at = last.started_at
            if last.status == RunStatus.queued:
                latest_event = await db.scalar(select(RunEvent).where(RunEvent.run_id == last.id)
                                               .order_by(RunEvent.seq.desc()).limit(1))
                out.waiting_reason = desk_feedback.quota_wait_reason(last, latest_event)
                out.waiting_limit = out.waiting_reason is not None
            elif last.status == RunStatus.running:
                from ttakkari.runs.manager import manager
                out.is_stuck = bool(last.pid is None and last.id not in manager._handles)
            if last.status in ACTIVE:
                progress_event = await db.scalar(select(RunEvent).where(
                    RunEvent.run_id == last.id,
                    RunEvent.type.in_(("tool.result", "tool.call", "message", "run.diff", "checkpoint")),
                ).order_by(RunEvent.seq.desc()).limit(1))
                out.last_progress = desk_feedback.progress_label(progress_event)
        # 오케스트레이터 화면 오른쪽 탭의 한 줄 요약
        done = last if last.status not in ACTIVE else await db.scalar(
            select(Run).where(Run.session_id == s.id, Run.status.not_in(ACTIVE)).order_by(Run.created_at.desc()).limit(1))
        if done is not None:
            text = done.result_text or done.error or ""
            out.last_result = " ".join(text.split())[:200] or None
    return out


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(_: User, db: Db, workspace_id: uuid.UUID | None = None, include_archived: bool = False,
                        limit: int = Query(50, le=200), before: str | None = None,
                        parent_id: uuid.UUID | None = None, pool: str | None = None) -> list[SessionOut]:
    """parent_id 를 주면 그 오케스트레이터의 작업 세션, 안 주면 사이드바 목록(작업 세션 제외)."""
    q = select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
    q = q.where(ChatSession.parent_id == parent_id) if parent_id else q.where(ChatSession.parent_id.is_(None))
    # ArtCraft 작업 세션은 ArtCraft 화면에서 다룬다(ADR 0039). 대화·작업 공간 목록에는 섞지 않는다.
    q = q.where(~exists().where(ArtcraftTask.session_id == ChatSession.id))
    if pool:
        q = q.where(ChatSession.pool == pool)
    if workspace_id:
        q = q.where(ChatSession.workspace_id == workspace_id)
    if not include_archived:
        q = q.where(ChatSession.archived_at.is_(None))
    return [await _session_out(db, s) for s in await db.scalars(q)]


@router.post("/sessions", response_model=SessionOut, status_code=201)
async def create_session(body: SessionIn, _: User, db: Db) -> SessionOut:
    ws = await get_workspace(db, body.workspace_id)
    if ws.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "보관된 워크스페이스입니다.")
    # 오케스트레이터는 auto 범위에서 사용량을 비교한다(ADR 0006). 접수는 엔진 순서를 따로 정한다(ADR 0013).
    engine = "auto" if body.kind in (SessionKind.orchestrator, SessionKind.desk) else (body.engine or ws.default_engine)
    if engine == "muse" and body.model and body.model not in get_settings().engine_models.get("muse", []):
        raise HTTPException(422, "Muse Code에서 지원하는 코딩 모델을 선택하세요.")
    s = ChatSession(workspace_id=ws.id, title=body.title or "새 작업", engine=engine,
                    model=body.model, effort=body.effort, strict_engine=body.strict_engine,
                    kind=body.kind, pool=body.pool)
    db.add(s)
    await db.commit()
    return await _session_out(db, s)


@router.get("/sessions/{session_id}", response_model=SessionOut)
async def get_sess(session_id: uuid.UUID, _: User, db: Db) -> SessionOut:
    return await _session_out(db, await get_session(db, session_id))


@router.patch("/sessions/{session_id}", response_model=SessionOut)
async def patch_session(session_id: uuid.UUID, body: SessionPatch, _: User, db: Db) -> SessionOut:
    s = await get_session(db, session_id)
    data = body.model_dump(exclude_unset=True)
    archived = data.pop("archived", None)
    for k, v in data.items():
        setattr(s, k, v)
    if archived is not None:
        s.archived_at = utcnow() if archived else None
    await db.commit()
    return await _session_out(db, s)


@router.get("/sessions/{session_id}/runs", response_model=list[RunOut])
async def list_runs(session_id: uuid.UUID, _: User, db: Db, limit: int | None = Query(None, ge=1, le=500),
                    before: datetime | None = None) -> list[RunOut]:
    """limit 을 주면 before 보다 앞선 최근 limit 개만 시간순으로 준다. 접수 대화는 하나뿐이라 끝없이 길어진다(ADR 0014)."""
    await get_session(db, session_id)
    q = select(Run).where(Run.session_id == session_id)
    if before is not None:
        q = q.where(Run.created_at < before)
    if limit is None:
        runs = list(await db.scalars(q.order_by(Run.created_at.asc())))
    else:
        runs = list(await db.scalars(q.order_by(Run.created_at.desc()).limit(limit)))[::-1]
    return await desk_feedback.runs_out(db, runs)


@router.get("/runs/failed", response_model=FailedRunsPage)
async def list_failed_runs(_: User, db: Db, limit: int = Query(30, ge=1, le=100),
                           before: datetime | None = None, before_id: uuid.UUID | None = None,
                           since: datetime | None = None) -> FailedRunsPage:
    """모든 종류의 세션에서 실패한 Run을 최신순으로 조회한다."""
    if (before is None) != (before_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "before와 before_id를 함께 보내야 합니다.")
    failed_at = func.coalesce(Run.finished_at, Run.created_at)
    q = (select(Run, ChatSession, Workspace.name)
         .join(ChatSession, ChatSession.id == Run.session_id)
         .join(Workspace, Workspace.id == Run.workspace_id)
         .where(Run.status == RunStatus.failed))
    if since is not None:
        q = q.where(failed_at >= since)
    if before is not None and before_id is not None:
        q = q.where(or_(failed_at < before, (failed_at == before) & (Run.id < before_id)))
    rows = list((await db.execute(q.order_by(failed_at.desc(), Run.id.desc()).limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [FailedRunOut.model_validate({
        **RunOut.model_validate(run).model_dump(),
        "failed_at": run.finished_at or run.created_at,
        "session_title": session.title,
        "session_kind": session.kind,
        "session_parent_id": session.parent_id,
        "pool": session.pool,
        "workspace_name": workspace_name,
    }) for run, session, workspace_name in rows]
    last = items[-1] if has_more and items else None
    return FailedRunsPage(items=items, next_before=last.failed_at if last else None,
                          next_before_id=last.id if last else None)


@router.post("/sessions/{session_id}/runs", response_model=RunOut, status_code=202)
async def create_run(session_id: uuid.UUID, body: RunIn, _: User, db: Db, request: Request,
                     request_key: Annotated[str | None, Header(alias="Idempotency-Key", max_length=120)] = None) -> Run:
    s = await get_session(db, session_id)
    ws = await get_workspace(db, s.workspace_id)
    if ws.archived_at is not None or s.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "보관된 세션입니다.")
    run = await queue_run(db, s, body.prompt, context_artifact_ids=body.context_artifact_ids,
                          model=body.model, effort=body.effort, ip=client_ip(request), request_key=request_key)
    await db.refresh(run)
    return run


async def queue_run(db: AsyncSession, s: ChatSession, prompt: str, *, origin: RunOrigin = RunOrigin.user,
                    context_artifact_ids: list[uuid.UUID] | None = None, model: str | None = None,
                    effort: str | None = None, ip: str | None = None, request_key: str | None = None,
                    on_queued: Callable[[Run], None] | None = None) -> Run:
    """세션 대기열에 Run 을 넣고 차례가 되면 시작한다. 실행 중이어도 받는다(manager.dispatch 가 순서대로 돌린다)."""
    selected_model = model or s.model
    if s.engine == "muse" and selected_model and selected_model not in get_settings().engine_models.get("muse", []):
        raise HTTPException(422, "Muse Code에서 지원하는 코딩 모델을 선택하세요.")
    if request_key:
        await db.scalar(select(ChatSession.id).where(ChatSession.id == s.id).with_for_update())
        existing = await db.scalar(select(Run).where(Run.session_id == s.id, Run.request_key == request_key))
        if existing:
            if (existing.prompt != prompt or existing.context_artifact_ids != [str(a) for a in context_artifact_ids or []]
                    or (model is not None and existing.model != model) or (effort is not None and existing.effort != effort)):
                raise HTTPException(409, "같은 요청 키가 다른 지시에 사용됐습니다.")
            return existing
    ahead = await db.scalar(select(func.count()).select_from(Run).where(Run.session_id == s.id, Run.status.in_(ACTIVE)))
    run = Run(session_id=s.id, workspace_id=s.workspace_id, prompt=prompt, origin=origin, request_key=request_key,
              context_artifact_ids=[str(a) for a in context_artifact_ids or []], engine=s.engine,
              model=model or s.model, effort=effort or s.effort)
    db.add(run)
    if s.title == "새 작업":
        s.title = prompt.strip().splitlines()[0][:60] or s.title
    s.updated_at = utcnow()
    await db.flush()
    if on_queued is not None:
        on_queued(run)
    events.append(db, run, "run.status", {"status": RunStatus.queued, "ahead": ahead or 0})
    record(db, actor="user" if origin == RunOrigin.user else "agent", action="run.create", target_type="run",
           target_id=run.id, ip=ip, detail={"engine": run.engine, "origin": origin})
    from ttakkari import generation
    if generation.current:
        await generation.fence_pending(db)
        await generation.wake(db, run.id)
    await db.commit()
    await manager.dispatch(s.id)
    return run


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, _: User, db: Db) -> RunOut:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run 이 없습니다.")
    return (await desk_feedback.runs_out(db, [run]))[0]


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: uuid.UUID, _: User, db: Db, request: Request) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run 이 없습니다.")
    if run.status in TERMINAL_RUN_STATUSES:
        return run
    if run.status == RunStatus.queued and not await manager.is_owned(run_id):
        await manager.cancel_queued(db, run)
        record(db, action="run.cancel", target_type="run", target_id=run_id, ip=client_ip(request))
        await db.commit()
        return run
    ok = await manager.cancel(run_id)
    record(db, action="run.cancel", target_type="run", target_id=run_id, outcome="ok" if ok else "error",
           ip=client_ip(request))
    await db.commit()
    if not ok:
        raise HTTPException(status.HTTP_409_CONFLICT, "실행 중인 프로세스를 찾지 못했습니다.")
    # 취소 반영(프로세스 종료·상태 기록)은 실행 태스크가 한다. 잠깐 기다려 최신 상태를 돌려준다.
    for _ in range(20):
        await asyncio.sleep(0.25)
        await db.refresh(run)
        if run.status in TERMINAL_RUN_STATUSES:
            break
    return run


@router.get("/runs/{run_id}/events", response_model=list[RunEventOut])
async def list_events(run_id: uuid.UUID, _: User, db: Db, after: int = 0,
                      limit: int = Query(EVENT_PAGE, le=2000)) -> list[RunEvent]:
    q = select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after).order_by(RunEvent.seq).limit(limit)
    return list(await db.scalars(q))


@router.get("/runs/{run_id}/stream")
async def stream_run(run_id: uuid.UUID, _: User, db: Db, request: Request, after: int = 0,
                     last_event_id: Annotated[str | None, Header()] = None) -> EventSourceResponse:
    """SSE. 모바일 재연결을 위해 Last-Event-ID(=seq) 이후부터 DB 에서 다시 보낸다."""
    if await db.get(Run, run_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run 이 없습니다.")
    cursor = after
    if last_event_id and last_event_id.isdigit():
        cursor = max(cursor, int(last_event_id))

    async def gen():
        nonlocal cursor
        opened = time.monotonic()
        sm = sessionmaker()
        while True:
            from ttakkari import generation
            if (generation.current and generation.current.state == "draining"
                    and time.monotonic() - opened > get_settings().runtime_sse_drain_seconds):
                # EOF without "end" makes the existing client reconnect with Last-Event-ID.
                return
            signal = events.signal_for(run_id)
            async with sm() as s:
                rows = list(await s.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > cursor).order_by(RunEvent.seq).limit(EVENT_PAGE)))
                run = await s.get(Run, run_id)
            for ev in rows:
                cursor = ev.seq
                yield {"id": str(ev.seq), "event": "run_event",
                       "data": RunEventOut.model_validate(ev).model_dump_json()}
            if len(rows) == EVENT_PAGE:
                continue
            if run is None or (run.status in TERMINAL_RUN_STATUSES and cursor >= run.last_seq):
                yield {"event": "end", "data": "{}"}
                return
            if await request.is_disconnected():
                return
            try:
                timeout = min(SSE_PING_SECONDS, get_settings().runtime_sse_drain_seconds) if generation.current else SSE_PING_SECONDS
                await asyncio.wait_for(signal.wait(), timeout)
            except TimeoutError:
                pass

    return EventSourceResponse(gen(), ping=SSE_PING_SECONDS, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
