"""회의록 읽기 모델: 사용자 API 와 에이전트 MCP 도구가 같이 쓴다(읽기 전용)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.context.search import lexical, scope_clause, terms
from ttakkari.meetings.projects import find_project, project_filter
from ttakkari.models import (
    Artifact,
    ClovaJob,
    ClovaNote,
    CtxEntity,
    Meeting,
    MeetingItem,
    MeetingItemKind,
    MeetingProposal,
    PmsProject,
)

KINDS = tuple(k.value for k in MeetingItemKind)


def _iso(d) -> str | None:
    return d.isoformat() if d else None


def item_out(it: MeetingItem) -> dict[str, Any]:
    return {"id": str(it.id), "kind": it.kind, "position": it.position, "text": it.text, "detail": it.detail,
            "owner": it.owner, "due": it.due, "evidence": it.evidence, "status": it.status, "conflicts": it.conflicts}


def proposal_out(p: MeetingProposal) -> dict[str, Any]:
    return {"id": str(p.id), "item_id": str(p.item_id) if p.item_id else None, "kind": p.kind, "status": p.status,
            "reason": p.reason, "payload": p.payload, "result": p.result, "created_at": _iso(p.created_at),
            "resolved_at": _iso(p.resolved_at)}


async def meeting_rows(db: AsyncSession, meetings: list[Meeting]) -> list[dict[str, Any]]:
    """목록용 요약. 프로젝트 이름·녹음 파일·항목 수·대기 제안 수를 한꺼번에 모은다."""
    ids = [m.id for m in meetings]
    ents = {e.id: e for e in await db.scalars(select(CtxEntity).where(CtxEntity.id.in_([m.entity_id for m in meetings if m.entity_id])))} \
        if any(m.entity_id for m in meetings) else {}
    projects = {p.id: p for p in await db.scalars(select(PmsProject).where(PmsProject.id.in_([m.pms_project_id for m in meetings if m.pms_project_id])))} if any(m.pms_project_id for m in meetings) else {}
    arts = {a.id: a for a in await db.scalars(select(Artifact).where(Artifact.id.in_([m.audio_artifact_id for m in meetings if m.audio_artifact_id])))} \
        if any(m.audio_artifact_id for m in meetings) else {}
    counts: dict[uuid.UUID, dict[str, int]] = {i: {k: 0 for k in KINDS} for i in ids}
    pending: dict[uuid.UUID, int] = {}
    if ids:
        for mid, kind, n in (await db.execute(select(MeetingItem.meeting_id, MeetingItem.kind, func.count())
                                              .where(MeetingItem.meeting_id.in_(ids)).group_by(MeetingItem.meeting_id, MeetingItem.kind))).all():
            counts[mid][kind] = n
        pending = dict((await db.execute(select(MeetingProposal.meeting_id, func.count()).where(
            MeetingProposal.meeting_id.in_(ids), MeetingProposal.status == "pending").group_by(MeetingProposal.meeting_id))).all())
    out = []
    for m in meetings:
        e, a = projects.get(m.pms_project_id) or ents.get(m.entity_id), arts.get(m.audio_artifact_id)
        out.append({
            "id": str(m.id), "title": m.title, "scope": m.scope, "status": m.status, "failed_stage": m.failed_stage,
            "error": m.error, "project": {"id": str(e.id), "name": e.name} if e else None, "held_at": _iso(m.held_at),
            "participants": m.participants, "source": m.source, "provider": m.provider,
            "audio": {"artifact_id": str(a.id), "filename": a.filename, "size_bytes": a.size_bytes} if a else None,
            "transcript_chars": len(m.transcript or ""), "transcript_source": m.transcript_source, "summary": m.summary,
            "counts": {**counts[m.id], "pending_proposals": pending.get(m.id, 0)},
            "analysis_run_id": str(m.analysis_run_id) if m.analysis_run_id else None,
            "analysis_session_id": str(m.analysis_session_id) if m.analysis_session_id else None,
            "vector": {"status": m.vector_status, "chunks": m.vector_chunks, "error": m.vector_error, "embedded_at": _iso(m.embedded_at)},
            "created_at": _iso(m.created_at), "updated_at": _iso(m.updated_at), "analyzed_at": _iso(m.analyzed_at)})
    return out


async def meeting_detail(db: AsyncSession, m: Meeting) -> dict[str, Any]:
    items = await db.scalars(select(MeetingItem).where(MeetingItem.meeting_id == m.id).order_by(MeetingItem.kind, MeetingItem.position))
    props = await db.scalars(select(MeetingProposal).where(MeetingProposal.meeting_id == m.id).order_by(MeetingProposal.created_at, MeetingProposal.id))
    context = [{"ref": ref, **{k: v for k, v in c.items() if k != "id"}} for ref, c in (m.analysis_context or {}).items()]
    from ttakkari.meetings import clova_jobs

    job = await db.scalar(select(ClovaJob).where(ClovaJob.meeting_id == m.id).order_by(ClovaJob.created_at.desc()).limit(1))
    note = await db.scalar(select(ClovaNote).where(ClovaNote.meeting_id == m.id).limit(1))
    clova = {"job": clova_jobs.job_out(job) if job else None,
             "note": clova_jobs.note_out(note) if note else None} if (job or note) else None
    return {**(await meeting_rows(db, [m]))[0], "transcript": m.transcript, "items": [item_out(i) for i in items],
            "proposals": [proposal_out(p) for p in props], "analysis_context": context, "clovanote": clova}


def meeting_filter(*, scope: str, status: str | None = None, entity_id: uuid.UUID | None = None):
    clauses = [scope_clause(Meeting.scope, scope)]
    if status:
        clauses.append(Meeting.status == status)
    if entity_id:
        clauses.append(project_filter(entity_id))
    return clauses


async def list_meetings(db: AsyncSession, *, scope: str = "all", q: str | None = None, status: str | None = None,
                        entity_id: uuid.UUID | None = None, limit: int = 30, offset: int = 0) -> list[Meeting]:
    stmt = select(Meeting).where(*meeting_filter(scope=scope, status=status, entity_id=entity_id))
    words = terms(q or "")
    if words:
        like = [f"%{w.replace('%', r'\%').replace('_', r'\_')}%" for w in words]
        hit_item = select(MeetingItem.meeting_id).where(or_(*[MeetingItem.text.ilike(x) for x in like]))
        stmt = stmt.where(or_(*[Meeting.title.ilike(x) for x in like], *[func.coalesce(Meeting.summary, "").ilike(x) for x in like],
                              Meeting.id.in_(hit_item)))
    stmt = stmt.order_by(func.coalesce(Meeting.held_at, Meeting.created_at).desc(), Meeting.created_at.desc())
    return list(await db.scalars(stmt.limit(max(1, min(limit, 100))).offset(max(0, offset))))


async def search_items(db: AsyncSession, *, scope: str = "all", q: str | None = None, kind: str | None = None,
                       status: str | None = None, project: str | None = None, limit: int = 20) -> dict[str, Any]:
    """결정·액션·논의·쟁점 항목을 찾는다. 질의가 있으면 어휘 검색 순위, 없으면 최신순. 읽기 전용이다."""
    entity_id = None
    if project:
        e = await find_project(db, project, scope=scope)
        if e is None:
            return {"items": [], "note": f"'{project}' 프로젝트를 찾지 못했어요. PMS에서 이름을 확인하세요."}
        entity_id = e.id
    stmt = (select(MeetingItem, Meeting).join(Meeting, Meeting.id == MeetingItem.meeting_id)
            .where(*meeting_filter(scope=scope, entity_id=entity_id)))
    if kind:
        stmt = stmt.where(MeetingItem.kind == kind)
    if status:
        stmt = stmt.where(MeetingItem.status == status)
    words = terms(q or "")
    if words:
        pred, score = lexical(func.concat_ws(" ", MeetingItem.text, MeetingItem.detail), q or "", words)
        stmt = stmt.where(pred).order_by(score.desc(), Meeting.created_at.desc())
    else:
        stmt = stmt.order_by(Meeting.created_at.desc(), MeetingItem.position)
    rows = (await db.execute(stmt.limit(max(1, min(limit, 50))))).all()
    names = {e.id: e.name for e in await db.scalars(select(CtxEntity).where(CtxEntity.id.in_([m.entity_id for _, m in rows if m.entity_id])))} \
        if rows else {}
    names.update({p.id: p.name for p in await db.scalars(select(PmsProject).where(PmsProject.id.in_([m.pms_project_id for _, m in rows if m.pms_project_id])))})
    return {"items": [{**item_out(i), "meeting": {"id": str(m.id), "title": m.title, "scope": m.scope, "status": m.status,
                                                   "held_at": _iso(m.held_at), "project": names.get(m.pms_project_id or m.entity_id)}} for i, m in rows]}


def agent_meeting(row: dict[str, Any], items: list[MeetingItem]) -> dict[str, Any]:
    """에이전트에게 주는 회의 한 건. 전사 원문·승인 대기 제안 내용은 주지 않는다(원문은 앱에서, 반영 여부는 사용자가 정한다)."""
    keep = ("id", "title", "scope", "status", "project", "held_at", "participants", "summary", "analyzed_at")
    grouped: dict[str, list[dict[str, Any]]] = {k: [] for k in KINDS}
    for it in items:
        d = item_out(it)
        grouped[it.kind].append({k: d[k] for k in ("id", "text", "detail", "owner", "due", "status", "evidence")})
    return {**{k: row[k] for k in keep}, "pending_proposals": row["counts"]["pending_proposals"], "items": grouped}
