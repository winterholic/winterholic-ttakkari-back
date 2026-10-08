from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sse_starlette.sse import EventSourceResponse

from ttakkari.api.deps import Db, get_session, get_workspace
from ttakkari.audit import record
from ttakkari.db import sessionmaker
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ChatSession,
    Run,
    RunEvent,
    RunStatus,
    utcnow,
)
from ttakkari.runs import events
from ttakkari.runs.manager import manager
from ttakkari.schemas import (
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
        if last.status in ACTIVE:
            out.active_run_id = last.id
    return out


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(_: User, db: Db, workspace_id: uuid.UUID | None = None, include_archived: bool = False,
                        limit: int = Query(50, le=200), before: str | None = None) -> list[SessionOut]:
    q = select(ChatSession).order_by(ChatSession.updated_at.desc()).limit(limit)
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
    s = ChatSession(workspace_id=ws.id, title=body.title or "새 작업", engine=body.engine or ws.default_engine,
                    model=body.model, effort=body.effort)
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
async def list_runs(session_id: uuid.UUID, _: User, db: Db) -> list[Run]:
    await get_session(db, session_id)
    return list(await db.scalars(select(Run).where(Run.session_id == session_id).order_by(Run.created_at.asc())))


@router.post("/sessions/{session_id}/runs", response_model=RunOut, status_code=202)
async def create_run(session_id: uuid.UUID, body: RunIn, _: User, db: Db, request: Request) -> Run:
    s = await get_session(db, session_id)
    ws = await get_workspace(db, s.workspace_id)
    if ws.archived_at is not None or s.archived_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "보관된 세션입니다.")
    # 실행 중이어도 받는다. 세션 대기열에 쌓이고 앞 Run 이 끝나면 순서대로 시작한다(manager.dispatch).
    ahead = await db.scalar(select(func.count()).select_from(Run).where(Run.session_id == s.id, Run.status.in_(ACTIVE)))
    run = Run(session_id=s.id, workspace_id=ws.id, prompt=body.prompt,
              context_artifact_ids=[str(a) for a in body.context_artifact_ids], engine=s.engine,
              model=body.model or s.model, effort=body.effort or s.effort)
    db.add(run)
    if s.title == "새 작업":
        s.title = body.prompt.strip().splitlines()[0][:60] or s.title
    s.updated_at = utcnow()
    await db.flush()
    events.append(db, run, "run.status", {"status": RunStatus.queued, "ahead": ahead or 0})
    record(db, action="run.create", target_type="run", target_id=run.id, ip=client_ip(request),
           detail={"engine": run.engine, "workspace": str(ws.id)})
    await db.commit()
    await manager.dispatch(s.id)
    await db.refresh(run)
    return run


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(run_id: uuid.UUID, _: User, db: Db) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run 이 없습니다.")
    return run


@router.post("/runs/{run_id}/cancel", response_model=RunOut)
async def cancel_run(run_id: uuid.UUID, _: User, db: Db, request: Request) -> Run:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run 이 없습니다.")
    if run.status in TERMINAL_RUN_STATUSES:
        return run
    if run.status == RunStatus.queued and not manager.is_active(run_id):
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
        sm = sessionmaker()
        while True:
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
                await asyncio.wait_for(signal.wait(), SSE_PING_SECONDS)
            except TimeoutError:
                pass

    return EventSourceResponse(gen(), ping=SSE_PING_SECONDS, headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
