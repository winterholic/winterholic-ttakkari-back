"""모든 Run 의 MCP 서버가 부르는 API(ADR 0007). 세션 한정 토큰. 맥락·파일·알림·다른 대화 열람."""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_, select

from ttakkari import sysinfo
from ttakkari.api.deps import Db, broker_http
from ttakkari.api.planner import (
    EventIn,
    EventPatch,
    FromDate,
    MemoIn,
    MemoPatch,
    ToDate,
    new_event,
)
from ttakkari.artifacts import preview
from ttakkari.artifacts.keep_evidence import (
    build_evidence,
    creator_run_id,
    find_user_quotes,
    has_keep_expression,
    is_keep_request,
    is_negated,
    made_by_request_run,
    object_binding,
    same_basename_paths,
)
from ttakkari.artifacts.kinds import needs_server_preview
from ttakkari.artifacts.service import refresh_from_original, register_file
from ttakkari.audit import record
from ttakkari.broker.policy import BrokerError, ResolvedPath, is_sensitive, is_within
from ttakkari.config import get_settings
from ttakkari.context import store
from ttakkari.context.library import library_catalog
from ttakkari.models import (
    Artifact,
    ArtifactArea,
    ArtifactKeepReason,
    ArtifactOrigin,
    AuditLog,
    ChatSession,
    CtxMemory,
    Memo,
    PreviewStatus,
    Run,
    RunEvent,
    RunStatus,
    RuntimeLease,
    Workspace,
    utcnow,
)
from ttakkari.orchestrator.runtime import OrchestratorRun
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.planner import kr_holidays
from ttakkari.planner import service as planner
from ttakkari.push.service import PushKind
from ttakkari.push.service import notify as push_notify
from ttakkari.schemas import ArtifactAreaIn, ArtifactKeepIn, ArtifactOut

router = APIRouter(prefix="/api/agent", tags=["agent"])


class RecallIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=8, ge=1, le=30)
    kinds: list[Literal["memory", "chunk", "entity", "meeting"]] | None = None


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


def _artifact_scope(session: ChatSession):
    """Work agents see user-shared files and work-session artifacts in their workspace."""
    if session.pool != "work":
        return ()
    work_sessions = select(ChatSession.id).where(
        ChatSession.pool == "work",
        ChatSession.workspace_id == session.workspace_id,
    )
    return (
        Artifact.workspace_id == session.workspace_id,
        or_(
            and_(Artifact.session_id.is_(None), Artifact.source.in_(("upload", "user_registered"))),
            Artifact.session_id.in_(work_sessions),
        ),
    )


def _err(e: store.ContextError) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": str(e)})


# ---- 맥락

@router.get("/library")
async def library(sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    return await library_catalog(db, scope="work" if session.pool == "work" else "all")


@router.post("/recall")
async def recall(body: RecallIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    hits = await store.recall(db, body.query, body.k, kinds=tuple(body.kinds or ("memory", "chunk", "entity", "meeting")),
                              scope="work" if session.pool == "work" else "all")
    result = {"results": [{"type": h.kind, "id": h.id, "score": round(h.score, 3), "text": h.text, **h.meta} for h in hits]}
    if body.kinds is None or set(body.kinds) & {"chunk", "entity"}:
        from ttakkari.knowledge import store as knowledge
        result["knowledge"] = await knowledge.recall(db, body.query, k=min(body.k,30),
                                                       scope="work" if session.pool == "work" else "all")
    return result


@router.post("/remember")
async def remember(body: RememberIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    try:
        m = await store.remember(db, body.content, kind=body.kind, entities=body.entities, source="agent",
                                 source_ref=f"session:{sid}", scope=session.pool)
    except store.ContextError as e:
        raise _err(e) from e
    record(db, actor="agent", action="context.remember", target_type="memory", target_id=m.id)
    await db.commit()
    return {"id": str(m.id), "saved": m.content}


@router.post("/forget")
async def forget(body: dict[str, str], sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    try:
        mid = uuid.UUID(body.get("id", ""))
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "id 가 올바르지 않습니다.") from e
    m = await db.get(CtxMemory, mid)
    if m is not None and session.pool == "work" and m.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기억을 찾을 수 없습니다.")
    # 사용자가 직접 넣은 기억, 사용자가 승인해 만든 회의 결정(source=meeting)은 에이전트가 지우지 못한다.
    if m is not None and m.source in ("manual", "meeting"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "사용자가 넣거나 승인한 기억은 비서 관리에서만 지울 수 있어요.")
    ok = await store.forget(db, mid)
    await db.commit()
    return {"forgotten": ok}


@router.post("/entities")
async def upsert_entity(body: EntityIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    try:
        e = await store.upsert_entity(db, body.name, body.type, summary=body.summary, aliases=body.aliases, scope=session.pool)
    except store.ContextError as e2:
        raise _err(e2) from e2
    await db.commit()
    return await store.entity_detail(db, e, scope="work" if session.pool == "work" else "all")


@router.get("/entities/{name}")
async def get_entity(name: str, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    scope = "work" if session.pool == "work" else "all"
    e = await store.find_entity(db, name, scope=scope)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "그런 항목이 없어요. recall 로 찾아보세요.")
    return await store.entity_detail(db, e, scope=scope)


@router.post("/link")
async def link(body: LinkIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    try:
        r = await store.link(db, body.subject, body.predicate, body.object, note=body.note, source="agent",
                             scope="work" if session.pool == "work" else "all")
    except store.ContextError as e:
        raise _err(e) from e
    await db.commit()
    return {"id": str(r.id), "relation": f"{body.subject} {r.predicate} {body.object}"}


# ---- 파일

@router.get("/artifacts")
async def artifacts(sid: AgentSession, db: Db, q: str | None = None, kind: str | None = None, limit: int = 20,
                    category: str | None = None, area: Literal["files", "agent"] | None = None) -> dict[str, Any]:
    session = await _session(db, sid)
    stmt = select(Artifact).where(Artifact.deleted_at.is_(None), *_artifact_scope(session))
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    if category:
        stmt = stmt.where(Artifact.category == category)
    if area:
        stmt = stmt.where(Artifact.area == area)
        if area == "files":
            stmt = stmt.where(Artifact.keep_reason.is_not(None))
    if q:
        like = f"%{q.replace('%', r'\%').replace('_', r'\_')}%"
        stmt = stmt.where(or_(Artifact.filename.ilike(like), Artifact.rel_path.ilike(like)))
    rows = await db.scalars(stmt.order_by(Artifact.created_at.desc()).limit(min(limit, 100)))
    # 에이전트가 원본을 읽을 수 있게 원래 경로를 준다(관리 영역 복사본은 guard 보호 영역).
    return {"artifacts": [{"id": str(a.id), "filename": a.filename, "kind": a.kind, "area": a.area, "path": a.original_path,
                           "size": a.size_bytes, "created_at": a.created_at.isoformat()} for a in rows]}


@router.patch("/artifacts/{art_id}")
async def move_artifact(art_id: uuid.UUID, body: ArtifactAreaIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    """보관함 분류를 옮긴다(내 보관함 ↔ AI 작업 파일).
    에이전트는 이 경로로 내 보관함(files)으로 승격할 수 없으며(keep_artifact 사용),
    사용자가 보관한 파일(keep_reason 존재)이나 사용자가 올린 파일은 뺄 수 없다."""
    session = await _session(db, sid)
    art = await db.scalar(select(Artifact).where(
        Artifact.id == art_id,
        Artifact.deleted_at.is_(None),
        *_artifact_scope(session),
    ))
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Artifact 가 없습니다.")
    if body.area == ArtifactArea.files:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "use_keep_artifact", "message":
                            "에이전트는 이 경로로 내 보관함에 넣을 수 없습니다. 사용자 요청 근거와 함께 keep_artifact 를 쓰세요."})
    if art.source in ("upload", "user_registered") or art.origin == ArtifactOrigin.user:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "user_owned", "message":
                            "사용자가 올린 파일의 분류는 앱에서만 바꿀 수 있어요."})
    if art.keep_reason is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "user_kept", "message":
                            "사용자가 보관한 파일은 에이전트가 내 보관함에서 뺄 수 없어요. 내 보관함에서 빼기는 사용자 앱에서만 가능합니다."})
    if art.area != body.area:
        record(db, actor="agent", action="artifact.move", target_type="artifact", target_id=art.id,
               detail={"from": art.area, "to": body.area, "session": str(sid)})
        art.area = body.area
        art.keep_reason = None
        art.keep_evidence = None
        await db.commit()
    return {"id": str(art.id), "filename": art.filename, "area": art.area}


@router.post("/share")
async def share(body: ShareIn, caller: OrchestratorRun, db: Db) -> dict[str, Any]:
    """에이전트가 만든(또는 찾은) 파일을 대화에서 사용자에게 전달한다(링크·미리보기·다운로드 제공).
    내 보관함(files)에 자동으로 영구 보관되지 않으며 AI 작업 파일(agent)로 등록/유지된다.
    실행 위치·권한 폴더·inbox 안의 파일만."""
    sid, token_run_id = caller
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
    # 수집·공유 사이의 조회-후-생성 경합을 막으려고 세션 행을 잠근다(dispatch 와 같은 순서: 세션 먼저).
    await db.execute(select(ChatSession.id).where(ChatSession.id == sid).with_for_update())
    # 세션의 최신 Run 은 대기 중인 다음 Run 일 수 있어 호출한 Run 이 아니다. 토큰에 묶인 Run 을 쓰고,
    # Run 에 묶이지 않은 레거시 토큰만 실행 중인 가장 최근 Run 으로 대신한다.
    run_id = token_run_id or await db.scalar(select(Run.id).where(
        Run.session_id == sid, Run.status == RunStatus.running).order_by(Run.created_at.desc()).limit(1))
    # 이미 이 세션에 등록된 경로(outbox 자동 수집 등)는 새 행을 만들지 않는다.
    # 사용자가 보관함에서 뺀 파일을 share_file 로 재공유하더라도 다시 files 로 승격되지 않는다!
    existing = await db.scalar(select(Artifact).where(
        Artifact.deleted_at.is_(None), Artifact.session_id == sid,
        Artifact.original_path == str(real)).order_by(Artifact.created_at.desc()).limit(1))
    if existing is not None:
        try:
            changed = await refresh_from_original(existing, real)
        except BrokerError as e:
            raise broker_http(e) from e
        if changed:
            await db.commit()
        if needs_server_preview(existing.kind) and (
                changed or existing.preview_status not in (PreviewStatus.ready, PreviewStatus.processing)):
            preview.enqueue(existing.id)
        return {"artifact": ArtifactOut.from_model(existing).model_dump(mode="json")}
    try:
        art = await register_file(db, ResolvedPath(root=root, path=real), source="agent_output", area=ArtifactArea.agent,
                                  origin=ArtifactOrigin.agent, keep_reason=None,
                                  workspace_id=ws.id, session_id=sid, run_id=run_id, rel_path=str(real.relative_to(root)),
                                  export_allowed=ws.export_allowed)
    except BrokerError as e:
        raise broker_http(e) from e
    await db.commit()
    if needs_server_preview(art.kind):
        preview.enqueue(art.id)
    return {"artifact": ArtifactOut.from_model(art).model_dump(mode="json")}


@router.post("/keep")
async def keep_artifact(body: ArtifactKeepIn, caller: OrchestratorRun, db: Db) -> dict[str, Any]:
    """사용자가 대화에서 보관을 요청했을 때 대상 파일을 내 보관함(files)에 영구 보관한다.

    body.quote 는 사용자가 보관을 요청한 말의 그대로 인용이다. 이 세션의 실제 사용자 발화(사용자 메시지 또는 접수가 넘긴 사용자 요청)에
    그 인용이 없으면 422(quote_not_found)로 거부하고 아무것도 바꾸지 않는다. 근거(발화 Run·세션·시각)는 keep_evidence 에 남는다."""
    sid, token_run_id = caller
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
                                                        "message": "실행 위치·권한 폴더 안의 일반 파일만 보관할 수 있어요."})
    await db.execute(select(ChatSession.id).where(ChatSession.id == sid).with_for_update())
    run_id = token_run_id
    if not run_id:
        run_id = await db.scalar(select(Run.id).where(
            Run.session_id == sid, Run.status == RunStatus.running).order_by(Run.created_at.desc()).limit(1))
    if not run_id:
        run_id = await db.scalar(select(Run.id).where(
            Run.session_id == sid).order_by(Run.created_at.desc()).limit(1))
    run = await db.get(Run, run_id) if run_id else None
    if run is None or run.session_id != sid:
        raise HTTPException(status.HTTP_409_CONFLICT, {"code": "run_not_found", "message": "보관 근거를 남길 Run을 찾을 수 없습니다."})

    existing = await db.scalar(select(Artifact).where(
        Artifact.deleted_at.is_(None), Artifact.session_id == sid,
        Artifact.original_path == str(real)).order_by(Artifact.created_at.desc()).limit(1))

    matches = await find_user_quotes(db, sid, run, body.quote)
    if not matches:
        raise HTTPException(422, {"code": "quote_not_found", "message":
                            "이 세션의 사용자 발화에서 그 인용을 찾지 못했어요. 사용자가 보관을 요청한 말을 문장 하나(또는 한 줄) 그대로, "
                            "공백 제외 8자 이상으로 인용하세요. 단어 일부만 인용하면 안 됩니다. 기획이 작업 세션에 쓴 지시는 사용자 발화가 아니므로 "
                            "근거가 될 수 없고, 작업 세션이면 기획 세션이 직접 보관해야 합니다."})
    if has_keep_expression(body.quote) and is_negated(body.quote):
        raise HTTPException(422, {"code": "quote_negated", "message":
                            "인용한 문장에 부정·금지·제외 표현이 있어요. 사용자가 앱의 '내 보관함에 보관' 버튼으로 직접 보관할 수 있어요."})
    if not is_keep_request(body.quote):
        raise HTTPException(422, {"code": "quote_no_keep_request", "message":
                            "인용한 문장이 허용된 보관 요청 템플릿이 아니에요. 파일명·따옴표 안의 말·'…라는' 앞 구절을 뺀 문장이 "
                            "'…보관해 줘', '…보관함에 넣어 줘', '…저장해 줘'(영어는 Please keep X in my library)처럼 끝나야 해요. "
                            "사용자가 보관을 요청한 문장을 인용하거나, 사용자에게 앱의 '내 보관함에 보관' 버튼을 안내하세요."})
    match, binding, binding_detail = None, "", ""
    mention, names_another = object_binding(body.quote, real)  # filename 결속은 보관 서술어의 직접 목적어 자리에서만 성립한다
    others = await same_basename_paths(db, session_id=sid, path=real, outbox_root=s.outbox_root)
    if mention is not None:
        if mention.path_token is not None:
            clash = [o for o in others if o.casefold().endswith("/" + mention.path_token.lstrip("/")) or o.casefold() == mention.path_token]
        else:
            clash = others
        if not clash:
            match, binding, binding_detail = matches[0], "filename", mention.path_token or mention.token
    if match is None and (mention is not None or not names_another):
        created_by = creator_run_id(real, s.outbox_root, existing.run_id if existing is not None else None)
        for cand in matches:
            if await made_by_request_run(db, session_id=sid, match=cand, path=real, created_by=created_by):
                match, binding, binding_detail = cand, "same_run", str(created_by)
                break
    if match is None and mention is not None and others:
        raise HTTPException(422, {"code": "quote_ambiguous_file", "message":
                            f"같은 이름 {real.name} 파일이 여러 개라 어느 파일인지 문장만으로 정할 수 없어요. 폴더가 들어간 경로가 적힌 "
                            "문장을 인용하거나, 사용자에게 앱의 '내 보관함에 보관' 버튼을 안내하세요. 후보: " + ", ".join(others[:3])})
    if match is None:
        raise HTTPException(422, {"code": "quote_not_bound", "message":
                            f"그 인용 문장이 {real.name} 을(를) 가리키지 않아요. 문장에 확장자까지 포함한 파일 이름이 있거나, 이 파일이 "
                            "그 발화를 처리한 Run(또는 그 Run 중에 띄운 작업 세션)의 결과물이어야 합니다. 로그·스크린샷 같은 부산물은 "
                            "파일 이름을 직접 쓴 문장이 필요해요."})
    evidence = build_evidence(match, session_id=sid, run=run, quote=body.quote, kept_at=utcnow(),
                              binding=binding, binding_detail=binding_detail)

    if existing is not None:
        if existing.area == ArtifactArea.files and existing.keep_reason is not None:
            # 이미 내 보관함에 있다. 처음의 보관 사유·근거를 덮어쓰지 않는다(업로드·직접 등록이 chat_request 로 바뀌면 안 된다).
            return {"artifact": ArtifactOut.from_model(existing).model_dump(mode="json")}
        removed_at = await _last_user_removal(db, existing.id)
        if removed_at is not None and match.run.created_at <= removed_at:
            raise HTTPException(status.HTTP_409_CONFLICT, {"code": "removed_after_request", "message":
                                "사용자가 이 요청 뒤에 내 보관함에서 뺀 파일이에요. 새로 보관을 요청한 말이 필요합니다."})
        try:
            changed = await refresh_from_original(existing, real)
        except BrokerError as e:
            raise broker_http(e) from e
        existing.area = ArtifactArea.files
        existing.keep_reason = ArtifactKeepReason.chat_request
        existing.keep_evidence = evidence
        record(db, actor="agent", action="artifact.keep", target_type="artifact", target_id=existing.id,
               detail={"via": "chat_request", "session": str(sid), "run_id": str(run.id), "message_id": evidence["message_id"]})
        await db.commit()
        if needs_server_preview(existing.kind) and (
                changed or existing.preview_status not in (PreviewStatus.ready, PreviewStatus.processing)):
            preview.enqueue(existing.id)
        return {"artifact": ArtifactOut.from_model(existing).model_dump(mode="json")}
    try:
        art = await register_file(db, ResolvedPath(root=root, path=real), source="agent_output", area=ArtifactArea.files,
                                  origin=ArtifactOrigin.agent, keep_reason=ArtifactKeepReason.chat_request,
                                  keep_evidence=evidence,
                                  workspace_id=ws.id, session_id=sid, run_id=run.id, rel_path=str(real.relative_to(root)),
                                  export_allowed=ws.export_allowed)
        record(db, actor="agent", action="artifact.keep", target_type="artifact", target_id=art.id,
               detail={"via": "chat_request", "session": str(sid), "run_id": str(run.id), "message_id": evidence["message_id"]})
    except BrokerError as e:
        raise broker_http(e) from e
    await db.commit()
    if needs_server_preview(art.kind):
        preview.enqueue(art.id)
    return {"artifact": ArtifactOut.from_model(art).model_dump(mode="json")}


async def _last_user_removal(db: Db, art_id: uuid.UUID) -> datetime | None:
    """사용자가 앱에서 이 파일을 내 보관함에서 뺀 마지막 시각."""
    return await db.scalar(select(AuditLog.at).where(
        AuditLog.action == "artifact.move", AuditLog.actor == "user", AuditLog.outcome == "ok",
        AuditLog.target_id == str(art_id), AuditLog.detail["to"].as_string() == ArtifactArea.agent)
        .order_by(AuditLog.at.desc()).limit(1))


# ---- 서버

@router.get("/system/resources")
async def system_resources(sid: AgentSession, db: Db) -> dict[str, Any]:
    """이 맥의 CPU·메모리·디스크·네트워크와 상위 프로세스(이름만, 명령줄 없음). 사용자 화면과 같은 값이다."""
    await _session(db, sid)
    return await sysinfo.snapshot()


# ---- 알림·다른 대화

@router.post("/notify")
async def notify(body: NotifyIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    return await push_notify(PushKind.agent, body.title, body.body, url=f"/chat/{sid}", tag=f"agent-{sid}", target_id=sid)


@router.get("/sessions")
async def sessions(sid: AgentSession, db: Db, limit: int = 20) -> dict[str, Any]:
    curr = await _session(db, sid)
    stmt = select(ChatSession).where(ChatSession.archived_at.is_(None))
    if curr.pool == "work":
        stmt = stmt.where(ChatSession.pool == "work")
    rows = await db.scalars(stmt.order_by(ChatSession.updated_at.desc()).limit(min(limit, 100)))
    return {"sessions": [{"id": str(s.id), "title": s.title, "kind": s.kind, "updated_at": s.updated_at.isoformat()}
                         for s in rows]}


@router.get("/sessions/{session_id}")
async def read_session(session_id: uuid.UUID, sid: AgentSession, db: Db, last: int = 10) -> dict[str, Any]:
    curr = await _session(db, sid)
    s = await db.get(ChatSession, session_id)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "세션이 없습니다.")
    if curr.pool == "work" and s.pool != "work":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "회사 세션에서는 개인 세션을 열람할 수 없습니다.")
    from ttakkari.runs.diagnostics import session_execution

    runs = list(await db.scalars(select(Run).where(Run.session_id == session_id).order_by(Run.created_at.desc())
                                 .limit(min(last, 50))))
    run_ids = [r.id for r in runs]
    last_seq = select(RunEvent.run_id, func.max(RunEvent.seq).label("seq")).where(
        RunEvent.run_id.in_(run_ids)).group_by(RunEvent.run_id).subquery()
    latest_types = dict((await db.execute(select(RunEvent.run_id, RunEvent.type).join(last_seq, and_(
        RunEvent.run_id == last_seq.c.run_id, RunEvent.seq == last_seq.c.seq)))).all()) if run_ids else {}
    leased = set(await db.scalars(select(RuntimeLease.run_id).where(RuntimeLease.run_id.in_(run_ids)))) if run_ids else set()
    execution, local_dispatch = await session_execution(db, s, runs)
    return {"title": s.title, "kind": s.kind,
        "execution": execution,
        "routing": {"engine": s.engine, "model": s.model, "task_type": s.worker_task_type,
                    "strict_engine": s.strict_engine, "pool": s.pool}, "turns": [
        {"id": str(r.id), "at": r.created_at.isoformat(), "origin": r.origin,
         "engine": r.engine, "model": r.model,
         "resume_after": r.resume_after.isoformat() if r.resume_after else None,
         "last_event_type": latest_types.get(r.id), "lease_owned": r.id in leased,
         "local_dispatch": local_dispatch[r.id],
         "prompt": r.prompt[:2000],
         "result": (r.result_text or r.error or "")[:2000], "status": r.status} for r in reversed(runs)]}


# ---- 메모·캘린더(ADR 0008)
# 삭제는 되돌릴 수 있는 보관 처리이고, 에이전트가 만든 항목만 지운다(forget 과 같은 정책).
# 사용자가 쓴 메모 본문은 덮어쓰지 못하고 덧붙이기만 한다.

# 카테고리는 이름으로 주고받는다(에이전트가 id 를 따로 찾지 않게). 없는 이름은 만들지 않고 422 로 있는 이름을 알려 준다.

class AgentMemoIn(MemoIn):
    category: str | None = Field(default=None, max_length=60)


class AgentMemoPatch(MemoPatch):
    append: str | None = Field(default=None, max_length=50_000)
    # 빈 문자열이면 미분류로 옮긴다.
    category: str | None = Field(default=None, max_length=60)


async def _memos_out(db: Db, memos: list[Memo], *, body: bool) -> list[dict[str, Any]]:
    names = {c.id: c.name for c in await planner.list_categories(db)}
    return [{**planner.memo_out(m, body=body), "category": names.get(m.category_id)} for m in memos]


@router.get("/memo-categories")
async def agent_list_memo_categories(sid: AgentSession, db: Db) -> dict[str, Any]:
    await _session(db, sid)
    return {"categories": [{"name": c.name, "color": c.color} for c in await planner.list_categories(db)]}


@router.get("/memos/tags")
async def agent_list_memo_tags(sid: AgentSession, db: Db, category: str | None = None) -> dict[str, Any]:
    """메모 태그와 쓰인 메모 수(많은 순). category: 카테고리 이름, "none" 이면 미분류."""
    session = await _session(db, sid)
    stmt = select(Memo.tags).where(Memo.deleted_at.is_(None))
    if session.pool == "work":
        stmt = stmt.where(Memo.scope == "work")
    cat = None if not category else planner.NONE if category.strip().lower() == "none" else await planner.resolve_category(db, category)
    if cat is planner.NONE:
        stmt = stmt.where(Memo.category_id.is_(None))
    elif cat is not None:
        stmt = stmt.where(Memo.category_id == cat)
    counts: dict[str, int] = {}
    for tags in await db.scalars(stmt):
        for t in tags:
            counts[t] = counts.get(t, 0) + 1
    return {"tags": [{"name": n, "count": c} for n, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]}


@router.get("/memos")
async def agent_list_memos(sid: AgentSession, db: Db, q: str | None = None, tag: Annotated[list[str] | None, Query()] = None,
                           category: str | None = None, limit: int = 30) -> dict[str, Any]:
    """category: 카테고리 이름, "none" 이면 미분류."""
    session = await _session(db, sid)
    cat = None if not category else planner.NONE if category.strip().lower() == "none" else await planner.resolve_category(db, category)
    scope = "work" if session.pool == "work" else None
    rows = await db.scalars(planner.memo_query(q, tag, category=cat, scope=scope).limit(max(1, min(limit, 100))))
    return {"memos": await _memos_out(db, list(rows), body=False)}


@router.get("/memos/{memo_id}")
async def agent_get_memo(memo_id: uuid.UUID, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    m = await planner.get_memo(db, memo_id)
    if session.pool == "work" and m.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "메모가 없어요."})
    return (await _memos_out(db, [m], body=True))[0]


@router.post("/memos", status_code=status.HTTP_201_CREATED)
async def agent_create_memo(body: AgentMemoIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    cat = await planner.resolve_category(db, body.category) if body.category else body.category_id
    scope = session.pool if session.pool in ("work", "personal") else "personal"
    m = Memo(title=body.title.strip(), body=body.body, tags=planner.clean_tags(body.tags), pinned=body.pinned, source="agent",
             category_id=(await planner.get_category(db, cat)).id if cat else None,
             scope=scope)
    db.add(m)
    record(db, actor="agent", action="memo.create", target_type="memo", target_id=m.id, detail={"session": str(sid)})
    await db.commit()
    return (await _memos_out(db, [m], body=True))[0]


@router.patch("/memos/{memo_id}")
async def agent_patch_memo(memo_id: uuid.UUID, body: AgentMemoPatch, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    m = await planner.get_memo(db, memo_id)
    planner.check_rev(m, body.rev)
    if m.source == "user" and (body.body is not None or body.title is not None):
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "user_owned", "message":
                            "사용자가 쓴 메모의 제목·본문은 고칠 수 없어요. append 로 덧붙이거나 새 메모를 만드세요."})
    if session.pool == "work" and m.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "메모가 없어요."})
    if body.title is not None:
        m.title = body.title.strip()
    if body.body is not None:
        m.body = body.body
    if body.append:
        m.body = (m.body.rstrip("\n") + "\n\n" if m.body else "") + body.append
    if body.tags is not None:
        m.tags = planner.clean_tags(body.tags)
    if body.pinned is not None:
        m.pinned = body.pinned
    if body.category is not None:
        m.category_id = await planner.resolve_category(db, body.category)
    elif "category_id" in body.model_fields_set:
        m.category_id = (await planner.get_category(db, body.category_id)).id if body.category_id else None
    m.rev += 1
    m.updated_at = utcnow()
    record(db, actor="agent", action="memo.update", target_type="memo", target_id=m.id, detail={"session": str(sid)})
    await db.commit()
    return (await _memos_out(db, [m], body=True))[0]


@router.delete("/memos/{memo_id}")
async def agent_delete_memo(memo_id: uuid.UUID, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    m = await planner.get_memo(db, memo_id)
    if m.source != "agent":
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "user_owned", "message":
                            "사용자가 쓴 메모는 앱에서만 지울 수 있어요."})
    if session.pool == "work" and m.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "메모가 없어요."})
    m.deleted_at = utcnow()
    record(db, actor="agent", action="memo.archive", target_type="memo", target_id=m.id, detail={"session": str(sid)})
    await db.commit()
    return {"archived": True}


@router.get("/events")
async def agent_list_events(sid: AgentSession, db: Db, from_: FromDate, to: ToDate,
                            q: str | None = None) -> dict[str, Any]:
    """구간의 일정(반복 일정은 펼쳐서)과 그 구간의 한국 공휴일."""
    session = await _session(db, sid)
    scope = "work" if session.pool == "work" else None
    return {"events": await planner.list_events(db, from_, to, q, scope=scope), "holidays": kr_holidays.holidays_between(from_, to)}


@router.post("/events", status_code=status.HTTP_201_CREATED)
async def agent_create_event(body: EventIn, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    scope = session.pool if session.pool in ("work", "personal") else "personal"
    ev = new_event(body, "agent", scope=scope)
    db.add(ev)
    record(db, actor="agent", action="calendar.create", target_type="calendar_event", target_id=ev.id,
           detail={"session": str(sid)})
    await db.commit()
    return planner.event_out(ev)


@router.patch("/events/{event_id}")
async def agent_patch_event(event_id: uuid.UUID, body: EventPatch, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    ev = await planner.get_event(db, event_id)
    if session.pool == "work" and ev.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "일정이 없어요."})
    planner.apply_event_fields(ev, body.model_dump(exclude_unset=True))
    ev.updated_at = utcnow()
    record(db, actor="agent", action="calendar.update", target_type="calendar_event", target_id=ev.id,
           detail={"session": str(sid)})
    await db.commit()
    return planner.event_out(ev)


@router.delete("/events/{event_id}")
async def agent_delete_event(event_id: uuid.UUID, sid: AgentSession, db: Db) -> dict[str, Any]:
    session = await _session(db, sid)
    ev = await planner.get_event(db, event_id)
    if ev.source != "agent":
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "user_owned", "message":
                            "사용자가 만든 일정은 앱에서만 지울 수 있어요."})
    if session.pool == "work" and ev.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "not_found", "message": "일정이 없어요."})
    ev.deleted_at = utcnow()
    record(db, actor="agent", action="calendar.archive", target_type="calendar_event", target_id=ev.id,
           detail={"session": str(sid)})
    await db.commit()
    return {"archived": True}
