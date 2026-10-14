"""클로바노트 연동 API(ADR 0021). 사용자 화면용 /api/clovanote 와 에이전트용 /api/agent/clovanote.

에이전트는 노트를 읽고, 동기화·가져오기를 요청할 수 있다(회의록을 만들 뿐 맥락 도서관 반영은 여전히 사용자 승인).
로그인 창 열기는 사용자 화면에만 있다(Mac 화면에 Chrome 을 띄우는 일이라서).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, or_, select

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.meetings import clova_jobs, pipeline
from ttakkari.meetings import clovanote as cn
from ttakkari.meetings.projects import find_project
from ttakkari.models import ChatSession, ClovaJob, ClovaNote, Meeting
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/clovanote", tags=["clovanote"])
agent_router = APIRouter(prefix="/api/agent/clovanote", tags=["agent"])
NoteState = Literal["ready", "processing", "failed", "unknown"]


class ImportIn(BaseModel):
    scope: Literal["personal", "work"] = "personal"
    project_id: uuid.UUID | None = None
    analyze: bool = True


class AgentImportIn(BaseModel):
    scope: Literal["personal", "work"] = "personal"
    project: str | None = None  # 프로젝트 이름·별칭
    analyze: bool = True


def _status_out(st: cn.Status | None) -> dict[str, Any]:
    login = cn.client.login
    return {
        "state": st.state if st else "unknown", "message": st.message if st else "아직 확인하지 않았어요.",
        "checked_at": st.checked_at if st else None, "account": st.account if st else None,
        "workspace": st.workspace if st else None, "quota": st.quota if st else None,
        "login": {"state": login.state, "message": login.message, "started_at": login.started_at, "finished_at": login.finished_at},
        "browser_running": cn.client.running,
    }


async def _counts(db: Db) -> dict[str, int]:
    active = await db.scalar(select(func.count()).select_from(ClovaJob).where(ClovaJob.status.in_(clova_jobs.ACTIVE)))
    notes = await db.scalar(select(func.count()).select_from(ClovaNote).where(func.coalesce(ClovaNote.meta["missing"].astext, "false") != "true"))
    unimported = await db.scalar(select(func.count()).select_from(ClovaNote).where(
        ClovaNote.meeting_id.is_(None), ClovaNote.state == "ready", func.coalesce(ClovaNote.meta["missing"].astext, "false") != "true",
        func.coalesce(ClovaNote.meta["sample"].astext, "false") != "true"))
    last_sync = await db.scalar(select(func.max(ClovaJob.finished_at)).where(ClovaJob.kind == "sync", ClovaJob.status == "succeeded"))
    return {"active_jobs": active or 0, "notes": notes or 0, "unimported": unimported or 0,
            "last_sync_at": last_sync.isoformat() if last_sync else None}


@router.get("/status")
async def get_status(_: User, db: Db, refresh: bool = False) -> dict[str, Any]:
    """refresh=true 면 실제로 Chrome 을 띄워 로그인·남은 시간을 확인한다(몇 초). 아니면 마지막 결과."""
    st = await cn.client.check() if refresh or cn.client.status_cache is None else cn.client.status_cache
    if st.state != "login_window" and cn.login_chrome_pids():
        st = cn.Status("login_window", cn.client.login.message or "로그인 창이 열려 있어요.")
    return {**_status_out(st), **(await _counts(db))}


@router.post("/login", status_code=status.HTTP_202_ACCEPTED)
async def open_login(_: User, db: Db) -> dict[str, Any]:
    """Mac 화면에 일반 Chrome 로그인 창을 띄운다(자동화 없는 창이라 네이버 추가 인증이 덜하다). 끝나면 자동으로 닫고 확인한다."""
    login = await cn.client.open_login_window()
    record(db, action="clovanote.login_window", target_type="clovanote", outcome="ok" if login.state == "waiting" else "error")
    await db.commit()
    return _status_out(cn.client.status_cache) | {"login": {"state": login.state, "message": login.message,
                                                             "started_at": login.started_at, "finished_at": None}}


@router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def sync(_: User, db: Db) -> dict[str, Any]:
    job = await clova_jobs.enqueue(db, "sync")
    await db.commit()
    clova_jobs.kick()
    return clova_jobs.job_out(job)


def _note_filter(q: str | None, state: str | None, imported: bool | None, include_samples: bool):
    clauses = [func.coalesce(ClovaNote.meta["missing"].astext, "false") != "true"]
    if not include_samples:
        clauses.append(func.coalesce(ClovaNote.meta["sample"].astext, "false") != "true")
    if state:
        clauses.append(ClovaNote.state == state)
    if imported is not None:
        clauses.append(ClovaNote.meeting_id.is_not(None) if imported else ClovaNote.meeting_id.is_(None))
    if q and q.strip():
        like = f"%{q.strip().replace('%', r'\%').replace('_', r'\_')}%"
        clauses.append(or_(ClovaNote.title.ilike(like), func.coalesce(ClovaNote.transcript, "").ilike(like)))
    return clauses


@router.get("/notes")
async def list_notes(_: User, db: Db, q: Annotated[str | None, Query(max_length=200)] = None, state: NoteState | None = None,
                     imported: bool | None = None, samples: bool = False, limit: int = Query(50, ge=1, le=200),
                     offset: int = Query(0, ge=0)) -> dict[str, Any]:
    rows = await db.scalars(select(ClovaNote).where(*_note_filter(q, state, imported, samples))
                            .order_by(func.coalesce(ClovaNote.recorded_at, ClovaNote.created_at).desc()).limit(limit).offset(offset))
    notes = list(rows)
    titles = {m.id: (m.title, m.status) for m in await db.scalars(select(Meeting).where(Meeting.id.in_([n.meeting_id for n in notes if n.meeting_id])))} \
        if any(n.meeting_id for n in notes) else {}
    jobs = {j.note_id: j for j in await db.scalars(select(ClovaJob).where(ClovaJob.kind == "import", ClovaJob.status.in_(clova_jobs.ACTIVE)))}
    out = []
    for n in notes:
        d = clova_jobs.note_out(n)
        if n.meeting_id in titles:
            d["meeting"] = {"id": str(n.meeting_id), "title": titles[n.meeting_id][0], "status": titles[n.meeting_id][1]}
        if n.note_id in jobs:
            d["import_job"] = clova_jobs.job_out(jobs[n.note_id])
        out.append(d)
    return {"notes": out}


@router.get("/notes/{note_id}")
async def get_note(note_id: str, _: User, db: Db) -> dict[str, Any]:
    n = await db.scalar(select(ClovaNote).where(ClovaNote.note_id == note_id))
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "노트가 없어요. 동기화해 보세요.")
    return clova_jobs.note_out(n, full=True)


@router.post("/notes/{note_id}/import", status_code=status.HTTP_202_ACCEPTED)
async def import_note(note_id: str, body: ImportIn, _: User, db: Db) -> dict[str, Any]:
    n = await db.scalar(select(ClovaNote).where(ClovaNote.note_id == note_id))
    if n is not None and n.meeting_id is not None and await db.get(Meeting, n.meeting_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "already_imported", "message": "이미 회의록으로 가져온 노트예요.",
                                                        "meeting_id": str(n.meeting_id)})
    try:
        await pipeline.check_project(db, body.project_id, body.scope)
    except pipeline.PipelineError as e:
        raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from e
    job = await clova_jobs.enqueue(db, "import", note_id=note_id, params={
        "scope": body.scope, "project_id": str(body.project_id) if body.project_id else None, "analyze": body.analyze})
    await db.commit()
    clova_jobs.kick()
    return clova_jobs.job_out(job)


@router.get("/jobs")
async def list_jobs(_: User, db: Db, active: bool = False, kind: str | None = None, limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    q = select(ClovaJob)
    if active:
        q = q.where(ClovaJob.status.in_(clova_jobs.ACTIVE))
    if kind:
        q = q.where(ClovaJob.kind == kind)
    rows = await db.scalars(q.order_by(ClovaJob.created_at.desc()).limit(limit))
    return {"jobs": [clova_jobs.job_out(j) for j in rows]}


@router.get("/jobs/failed")
async def list_failed_jobs(_: User, db: Db, limit: int = Query(30, ge=1, le=100),
                           before: datetime | None = None, before_id: uuid.UUID | None = None,
                           since: datetime | None = None) -> dict[str, Any]:
    """클로바노트 백그라운드 작업 중 실패한 것만 최신순으로 조회한다."""
    if (before is None) != (before_id is None):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "before와 before_id를 함께 보내야 합니다.")
    failed_at = func.coalesce(ClovaJob.finished_at, ClovaJob.updated_at, ClovaJob.created_at)
    q = (select(ClovaJob, Meeting.title, ClovaNote.title)
         .outerjoin(Meeting, Meeting.id == ClovaJob.meeting_id)
         .outerjoin(ClovaNote, ClovaNote.note_id == ClovaJob.note_id)
         .where(ClovaJob.status == "failed"))
    if since is not None:
        q = q.where(failed_at >= since)
    if before is not None and before_id is not None:
        q = q.where(or_(failed_at < before, (failed_at == before) & (ClovaJob.id < before_id)))
    rows = list((await db.execute(q.order_by(failed_at.desc(), ClovaJob.id.desc()).limit(limit + 1))).all())
    has_more = len(rows) > limit
    rows = rows[:limit]
    items = [{**clova_jobs.job_out(job), "failed_at": job.finished_at or job.updated_at or job.created_at,
              "meeting_title": meeting_title, "note_title": note_title}
             for job, meeting_title, note_title in rows]
    last = items[-1] if has_more and items else None
    return {"items": items, "next_before": last["failed_at"] if last else None,
            "next_before_id": last["id"] if last else None}


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    j = await db.get(ClovaJob, job_id)
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "작업이 없어요.")
    return clova_jobs.job_out(j)


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_job(job_id: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    """실패한 작업을 같은 내용으로 다시 건다. 업로드는 회의록의 '전사 요청'으로 다시 거는 게 맞아서 여기서는 받지 않는다."""
    j = await db.get(ClovaJob, job_id)
    if j is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "작업이 없어요.")
    if j.status != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "bad_state", "message": "실패한 작업만 다시 할 수 있어요."})
    if j.kind == "upload":
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "bad_state", "message": "회의록 화면에서 전사를 다시 요청해 주세요."})
    new = await clova_jobs.enqueue(db, j.kind, meeting_id=j.meeting_id, note_id=j.note_id, params=j.params)
    await db.commit()
    clova_jobs.kick()
    return clova_jobs.job_out(new)


# ---- 에이전트(MCP list_clovanote_notes / get_clovanote_note / sync_clovanote / import_clovanote_note)
# 클로바노트 계정은 개인 계정이라, 회사 세션은 회사 회의록으로 가져온 노트만 본다.

async def _agent_work(db: Db, sid: uuid.UUID) -> bool:
    s = await db.get(ChatSession, sid)
    if s is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "세션이 없습니다.")
    return s.pool == "work"


def _work_only():
    return ClovaNote.meeting_id.in_(select(Meeting.id).where(Meeting.scope == "work"))


@agent_router.get("/notes")
async def agent_notes(sid: AgentSession, db: Db, q: str | None = None, state: NoteState | None = None, imported: bool | None = None,
                      limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    clauses = _note_filter(q, state, imported, False)
    if await _agent_work(db, sid):
        clauses.append(_work_only())
    rows = await db.scalars(select(ClovaNote).where(*clauses).order_by(func.coalesce(ClovaNote.recorded_at, ClovaNote.created_at).desc()).limit(limit))
    keep = ("note_id", "title", "state", "duration_ms", "recorded_at", "speakers", "has_transcript", "summary", "meeting_id")
    return {"notes": [{k: clova_jobs.note_out(n)[k] for k in keep} for n in rows],
            "note": "동기화가 오래됐으면 sync_clovanote 로 새 노트를 받아요. 전사는 get_clovanote_note 로 읽어요."}


@agent_router.get("/notes/{note_id}")
async def agent_note(note_id: str, sid: AgentSession, db: Db, offset: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=500)) -> dict[str, Any]:
    clauses = [ClovaNote.note_id == note_id]
    if await _agent_work(db, sid):
        clauses.append(_work_only())
    n = await db.scalar(select(ClovaNote).where(*clauses))
    if n is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "노트가 없어요. list_clovanote_notes 로 id 를 확인하세요.")
    d = clova_jobs.note_out(n)
    segs = n.segments or []
    return {**{k: d[k] for k in ("note_id", "title", "state", "duration_ms", "recorded_at", "speakers", "summary", "meeting_id")},
            "segments": segs[offset:offset + limit], "segments_total": len(segs),
            "next_offset": offset + limit if offset + limit < len(segs) else None}


@agent_router.post("/sync", status_code=status.HTTP_202_ACCEPTED)
async def agent_sync(sid: AgentSession, db: Db) -> dict[str, Any]:
    if await _agent_work(db, sid):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "회사 세션에서는 개인 클로바노트 계정을 동기화하지 않아요.")
    job = await clova_jobs.enqueue(db, "sync", params={"by": "agent"})
    await db.commit()
    clova_jobs.kick()
    return {"job_id": str(job.id), "status": job.status, "note": "끝나면 list_clovanote_notes 로 확인하세요(보통 수십 초)."}


@agent_router.post("/notes/{note_id}/import", status_code=status.HTTP_202_ACCEPTED)
async def agent_import(note_id: str, body: AgentImportIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    if await _agent_work(db, sid) and body.scope != "work":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "회사 세션은 회사 회의록으로만 가져올 수 있어요.")
    n = await db.scalar(select(ClovaNote).where(ClovaNote.note_id == note_id))
    if n is not None and n.meeting_id is not None:
        return {"meeting_id": str(n.meeting_id), "note": "이미 가져온 노트예요. get_meeting 으로 읽으세요."}
    project_id = None
    if body.project:
        e = await find_project(db, body.project, scope=body.scope)
        if e is None:
            return {"error": "project_not_found", "note": f"'{body.project}' 프로젝트를 찾지 못했어요. PMS에서 이름을 확인하세요."}
        project_id = e.id
    try:
        await pipeline.check_project(db, project_id, body.scope)
    except pipeline.PipelineError as e:
        raise HTTPException(e.status, {"code": e.code, "message": str(e)}) from e
    job = await clova_jobs.enqueue(db, "import", note_id=note_id, params={
        "scope": body.scope, "project_id": str(project_id) if project_id else None, "analyze": body.analyze, "by": "agent"})
    await db.commit()
    clova_jobs.kick()
    return {"job_id": str(job.id), "status": job.status,
            "note": "가져오면 회의록이 생기고 분석이 시작돼요. 맥락 도서관 반영은 사용자가 앱에서 승인해요."}
