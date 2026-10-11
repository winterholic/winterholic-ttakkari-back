"""모든 Run 의 MCP 서버가 부르는 API(ADR 0007). 세션 한정 토큰. 맥락·파일·알림·다른 대화 열람."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_, select

from ttakkari.api.deps import Db, broker_http
from ttakkari.artifacts import preview
from ttakkari.artifacts.kinds import needs_server_preview
from ttakkari.artifacts.service import register_file
from ttakkari.audit import record
from ttakkari.broker.policy import BrokerError, ResolvedPath, is_sensitive, is_within
from ttakkari.config import get_settings
from ttakkari.context import store
from ttakkari.models import Artifact, ChatSession, CtxMemory, Run, Workspace
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.push.service import send_to_all
from ttakkari.schemas import ArtifactOut

router = APIRouter(prefix="/api/agent", tags=["agent"])


class RecallIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=8, ge=1, le=30)
    kinds: list[Literal["memory", "chunk", "entity"]] | None = None


class RememberIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: Literal["fact", "preference", "decision", "event", "instruction", "note"] = "fact"
    entities: list[str] = Field(default_factory=list, max_length=10)


class LinkIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=60)
    object: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class EntityIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: str = "other"
    summary: str | None = Field(default=None, max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class ShareIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


class NotifyIn(BaseModel):
    title: str = Field(min_length=1, max_length=80)
    body: str = Field(default="", max_length=300)


async def _session(db: Db, sid: uuid.UUID) -> ChatSession:
    s = await db.get(ChatSession, sid)
    if s is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "세션이 없습니다.")
    return s


def _err(e: store.ContextError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": str(e)})


# ---- 맥락

@router.post("/recall")
async def recall(body: RecallIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    hits = await store.recall(db, body.query, body.k, kinds=tuple(body.kinds or ("memory", "chunk", "entity")))
    return {"results": [{"type": h.kind, "id": h.id, "score": round(h.score, 3), "text": h.text, **h.meta} for h in hits]}


@router.post("/remember")
async def remember(body: RememberIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    try:
        m = await store.remember(db, body.content, kind=body.kind, entities=body.entities, source="agent",
                                 source_ref=f"session:{sid}")
    except store.ContextError as e:
        raise _err(e) from e
    record(db, actor="agent", action="context.remember", target_type="memory", target_id=m.id)
    await db.commit()
    return {"id": str(m.id), "saved": m.content}


@router.post("/forget")
async def forget(body: dict[str, str], sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    try:
        mid = uuid.UUID(body.get("id", ""))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "id 가 올바르지 않습니다.") from e
    m = await db.get(CtxMemory, mid)
    # 사용자가 직접 넣은 기억은 에이전트가 지우지 못한다.
    if m is not None and m.source == "manual":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "사용자가 넣은 기억은 비서 관리에서만 지울 수 있어요.")
    ok = await store.forget(db, mid)
    await db.commit()
    return {"forgotten": ok}


@router.post("/entities")
async def upsert_entity(body: EntityIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    try:
        e = await store.upsert_entity(db, body.name, body.type, summary=body.summary, aliases=body.aliases)
    except store.ContextError as e2:
        raise _err(e2) from e2
    await db.commit()
    return await store.entity_detail(db, e)


@router.get("/entities/{name}")
async def get_entity(name: str, sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    e = await store.find_entity(db, name)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "그런 항목이 없어요. recall 로 찾아보세요.")
    return await store.entity_detail(db, e)


@router.post("/link")
async def link(body: LinkIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    try:
        r = await store.link(db, body.subject, body.predicate, body.object, note=body.note, source="agent")
    except store.ContextError as e:
        raise _err(e) from e
    await db.commit()
    return {"id": str(r.id), "relation": f"{body.subject} {r.predicate} {body.object}"}


# ---- 파일

@router.get("/artifacts")
async def artifacts(sid: AgentSession, db: Db, q: str | None = None, kind: str | None = None, limit: int = 20) -> dict[str, Any]:
    await _session(db, sid)
    stmt = select(Artifact).where(Artifact.deleted_at.is_(None))
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(or_(Artifact.filename.ilike(like), Artifact.rel_path.ilike(like)))
    rows = await db.scalars(stmt.order_by(Artifact.created_at.desc()).limit(min(limit, 100)))
    # 에이전트가 원본을 읽을 수 있게 원래 경로를 준다(관리 영역 복사본은 guard 보호 영역).
    return {"artifacts": [{"id": str(a.id), "filename": a.filename, "kind": a.kind, "path": a.original_path,
                           "size": a.size_bytes, "created_at": a.created_at.isoformat()} for a in rows]}


@router.post("/share")
async def share(body: ShareIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    """에이전트가 만든(또는 찾은) 파일을 사용자 보관함에 올린다. 실행 위치·권한 폴더·inbox 안의 파일만."""
    sess = await _session(db, sid)
    ws = await db.get(Workspace, sess.workspace_id)
    assert ws is not None
    s = get_settings()
    p = Path(body.path).expanduser()
    if not p.is_absolute():
        p = Path(ws.root_path) / p
    real = p.resolve()
    scope = [Path(ws.root_path).resolve(), s.inbox_root.resolve(), s.outbox_root.resolve(),
             *[Path(x).resolve() for x in ws.extra_writable_roots]]
    root = next((r for r in scope if is_within(real, r)), None)
    if root is None or is_sensitive(real) or not real.is_file():
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "outside_root",
                                                        "message": "실행 위치·권한 폴더 안의 일반 파일만 공유할 수 있어요."})
    run_id = await db.scalar(select(Run.id).where(Run.session_id == sid).order_by(Run.created_at.desc()).limit(1))
    try:
        art = await register_file(db, ResolvedPath(root=root, path=real), source="agent_output", workspace_id=ws.id,
                                  session_id=sid, run_id=run_id, rel_path=str(real.relative_to(root)),
                                  export_allowed=ws.export_allowed)
    except BrokerError as e:
        raise broker_http(e) from e
    await db.commit()
    if needs_server_preview(art.kind):
        preview.enqueue(art.id)
    return {"artifact": ArtifactOut.from_model(art).model_dump(mode="json")}


# ---- 알림·다른 대화

@router.post("/notify")
async def notify(body: NotifyIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    counts = await send_to_all({"title": body.title, "body": body.body, "url": f"/chat/{sid}", "tag": f"agent-{sid}"},
                               action="push.agent", target_id=sid)
    return counts


@router.get("/sessions")
async def sessions(sid: AgentSession, db: Db, limit: int = 20) -> dict[str, Any]:
    await _session(db, sid)
    rows = await db.scalars(select(ChatSession).where(ChatSession.archived_at.is_(None))
                            .order_by(ChatSession.updated_at.desc()).limit(min(limit, 100)))
    return {"sessions": [{"id": str(s.id), "title": s.title, "kind": s.kind, "updated_at": s.updated_at.isoformat()}
                         for s in rows]}


@router.get("/sessions/{session_id}")
async def read_session(session_id: uuid.UUID, sid: AgentSession, db: Db, last: int = 10) -> dict[str, Any]:
    await _session(db, sid)
    s = await db.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "세션이 없습니다.")
    runs = list(await db.scalars(select(Run).where(Run.session_id == session_id).order_by(Run.created_at.desc())
                                 .limit(min(last, 50))))
    return {"title": s.title, "kind": s.kind, "turns": [
        {"at": r.created_at.isoformat(), "origin": r.origin, "prompt": r.prompt[:2000],
         "result": (r.result_text or r.error or "")[:2000], "status": r.status} for r in reversed(runs)]}
