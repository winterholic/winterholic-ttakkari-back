"""비서 관리(ADR 0007): 따까리 전용 하네스(지침)·기억·온톨로지·지식(RAG 출처)을 사용자가 고친다."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.broker.policy import is_sensitive, is_within
from ttakkari.config import get_settings
from ttakkari.context import store
from ttakkari.context.indexer import index_source
from ttakkari.context.library import library_catalog
from ttakkari.db import sessionmaker
from ttakkari.models import (
    CtxChunk,
    CtxDocument,
    CtxEntity,
    CtxMemory,
    CtxRelation,
    CtxSource,
    utcnow,
)
from ttakkari.runs import instructions as instructions_mod
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant", tags=["assistant"])
log = logging.getLogger(__name__)
MAX_INSTRUCTIONS = 20_000

Kind = Literal["fact", "preference", "decision", "event", "instruction", "note"]
EType = Literal["person", "project", "org", "place", "tool", "topic", "preference", "other"]

# 비서 관리 > 도구 탭에 보이는 목록. 실제 등록은 orchestrator/mcp_server.py 가 한다.
TOOLS: list[dict[str, str]] = [
    {"name": "library_catalog", "group": "맥락", "who": "모든 실행", "desc": "맥락 도서관의 종류·지식/작업 기록 모음·프로젝트 이정표를 본다"},
    {"name": "recall", "group": "맥락", "who": "모든 실행", "desc": "나에 관한 기억·관계·색인된 문서를 찾는다"},
    {"name": "remember", "group": "맥락", "who": "모든 실행", "desc": "새 사실·취향·결정을 기억한다"},
    {"name": "get_entity", "group": "맥락", "who": "모든 실행", "desc": "사람·프로젝트 등의 요약과 관계를 본다"},
    {"name": "describe_entity", "group": "맥락", "who": "모든 실행", "desc": "엔터티를 만들거나 요약을 고친다"},
    {"name": "link", "group": "맥락", "who": "모든 실행", "desc": "두 엔터티 사이 관계를 기록한다"},
    {"name": "forget", "group": "맥락", "who": "모든 실행", "desc": "에이전트가 넣은 기억을 보관 처리한다"},
    {"name": "list_artifacts", "group": "파일", "who": "모든 실행", "desc": "보관함 파일 목록과 경로(파일·에이전트 파일 탭으로 거르기)"},
    {"name": "set_artifact_area", "group": "파일", "who": "모든 실행", "desc": "보관함 파일을 파일 ↔ 에이전트 파일 탭으로 옮긴다(사용자가 올린 파일은 제외)"},
    {"name": "share_file", "group": "파일", "who": "모든 실행", "desc": "파일을 보관함에 올려 폰에서 보게 한다"},
    {"name": "request_backend_deploy", "group": "서버", "who": "개인 실행", "desc": "커밋한 백엔드 배포를 예약한다. 독립 감독이 검사·작업 종료 대기 후 재시작한다"},
    {"name": "backend_deploy_status", "group": "서버", "who": "개인 실행", "desc": "배포 예약의 진행 상태·성공·실패 이유를 조회한다"},
    {"name": "server_resources", "group": "서버", "who": "모든 실행", "desc": "이 맥의 CPU·메모리·디스크·네트워크와 상위 프로세스"},
    {"name": "notify_user", "group": "알림", "who": "모든 실행", "desc": "폰으로 푸시 알림을 보낸다"},
    {"name": "list_sessions", "group": "대화", "who": "모든 실행", "desc": "최근 대화 목록"},
    {"name": "read_session", "group": "대화", "who": "모든 실행", "desc": "다른 대화의 최근 지시·결과"},
    {"name": "list_memo_categories", "group": "메모", "who": "모든 실행", "desc": "메모 카테고리 목록(순서대로)"},
    {"name": "list_memo_tags", "group": "메모", "who": "모든 실행", "desc": "메모 태그와 쓰인 수(많은 순)"},
    {"name": "list_memos", "group": "메모", "who": "모든 실행", "desc": "메모 목록(고정 먼저)과 발췌, 카테고리·태그로 거르기"},
    {"name": "search_memos", "group": "메모", "who": "모든 실행", "desc": "제목·본문·태그로 메모를 찾는다(카테고리·태그로 좁히기)"},
    {"name": "get_memo", "group": "메모", "who": "모든 실행", "desc": "메모 본문 전체를 읽는다"},
    {"name": "create_memo", "group": "메모", "who": "모든 실행", "desc": "새 메모를 쓴다(카테고리·태그 지정)"},
    {"name": "update_memo", "group": "메모", "who": "모든 실행", "desc": "메모에 덧붙이거나 태그·카테고리를 고친다(사용자 메모 본문은 덮어쓰지 못함)"},
    {"name": "delete_memo", "group": "메모", "who": "모든 실행", "desc": "에이전트가 만든 메모만 보관 처리"},
    {"name": "list_checklist", "group": "체크리스트", "who": "모든 실행", "desc": "날짜별 항목·완료 현황 조회"},
    {"name": "create_checklist_item", "group": "체크리스트", "who": "모든 실행", "desc": "날짜별 항목 추가"},
    {"name": "update_checklist_item", "group": "체크리스트", "who": "모든 실행", "desc": "날짜별 항목 수정·체크·체크 해제"},
    {"name": "delete_checklist_item", "group": "체크리스트", "who": "모든 실행", "desc": "해당 날짜의 항목 삭제"},
    {"name": "list_checklist_templates", "group": "체크리스트", "who": "모든 실행", "desc": "고정 항목 조회"},
    {"name": "create_checklist_template", "group": "체크리스트", "who": "모든 실행", "desc": "매일 반복할 고정 항목 추가"},
    {"name": "update_checklist_template", "group": "체크리스트", "who": "모든 실행", "desc": "앞으로 생성할 고정 항목 수정"},
    {"name": "delete_checklist_template", "group": "체크리스트", "who": "모든 실행", "desc": "고정 항목의 앞으로 반복 중단"},
    {"name": "list_events", "group": "캘린더", "who": "모든 실행", "desc": "기간의 일정과 한국 공휴일"},
    {"name": "create_event", "group": "캘린더", "who": "모든 실행", "desc": "일정을 만든다(종일·시간·반복)"},
    {"name": "update_event", "group": "캘린더", "who": "모든 실행", "desc": "일정을 고친다"},
    {"name": "delete_event", "group": "캘린더", "who": "모든 실행", "desc": "에이전트가 만든 일정만 보관 처리"},
    {"name": "list_meetings", "group": "회의록", "who": "모든 실행", "desc": "회의록 목록(제목·프로젝트·상태·요약). 읽기 전용"},
    {"name": "get_meeting", "group": "회의록", "who": "모든 실행", "desc": "회의록 한 건의 요약·논의·결정·액션·미해결 쟁점. 읽기 전용"},
    {"name": "search_meeting_items", "group": "회의록", "who": "모든 실행", "desc": "회의의 결정·액션 아이템을 찾는다(프로젝트·상태로 좁히기). 읽기 전용"},
    {"name": "search_meetings", "group": "회의록", "who": "모든 실행", "desc": "회의록·전사 구간을 뜻으로 찾는다(로컬 벡터 + 키워드). 읽기 전용"},
    {"name": "spawn_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션을 만들어 일을 맡긴다"},
    {"name": "account_usage", "group": "작업 세션", "who": "오케스트레이터", "desc": "계정별 모델·5시간/주간 잔여·Antigravity 모델 그룹 quota와 AI 크레딧·초기화·한도 대기·실행 중 Run 수"},
    {"name": "list_pms_projects", "group": "PMS", "who": "모든 실행", "desc": "PMS 프로젝트와 진행 상태를 찾는다(회사 실행은 회사 범위)"},
    {"name": "create_pms_task", "group": "PMS", "who": "모든 실행", "desc": "WBS 작업·계층·선행관계·담당·일정·진행률을 등록한다"},
    {"name": "manage_pms", "group": "PMS", "who": "모든 실행", "desc": "사용자를 대신해 프로젝트·작업 트리·선행관계·마일스톤·이슈를 생성·조회·수정·삭제한다"},
    {"name": "message_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션에 추가 지시"},
    {"name": "list_workers", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션 목록"},
    {"name": "get_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션 상세"},
    {"name": "stop_worker", "group": "작업 세션", "who": "오케스트레이터", "desc": "작업 세션 멈추기"},
    {"name": "my_context", "group": "작업 세션", "who": "오케스트레이터", "desc": "내 대화의 컨텍스트 사용률"},
    {"name": "reset_my_context", "group": "작업 세션", "who": "오케스트레이터", "desc": "다음 턴부터 내 대화를 새로 시작"},
]


class InstructionsIO(BaseModel):
    text: str = Field(default="", max_length=MAX_INSTRUCTIONS)


class InstructionsOut(InstructionsIO):
    role: instructions_mod.Role = "common"
    # 저장한 적 없이 기본값을 보여 주는 중(접수만 기본값이 있다)
    is_default: bool = False


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=4000)
    kind: Kind = "fact"
    entities: list[str] = Field(default_factory=list, max_length=10)
    pinned: bool = False
    scope: Literal["personal", "work", "shared"] = "personal"
    source_ref: str | None = Field(default=None, max_length=4096)


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=4000)
    kind: Kind | None = None
    pinned: bool | None = None


class EntityIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    type: EType = "other"
    summary: str | None = Field(default=None, max_length=2000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    pinned: bool = False
    scope: Literal["personal", "work", "shared"] = "personal"
    attributes: dict[str, Any] = Field(default_factory=dict)


class EntityPatch(BaseModel):
    summary: str | None = Field(default=None, max_length=2000)
    aliases: list[str] | None = Field(default=None, max_length=20)
    pinned: bool | None = None
    type: EType | None = None
    attributes: dict[str, Any] | None = None


class RelationIn(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=60)
    object: str = Field(min_length=1, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class SourceIn(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    pattern: str = Field(default="**/*.md", max_length=100)
    scope: Literal["personal", "work", "shared"] = "personal"
    category: Literal["projects", "work-history", "knowledge", "health"] = "knowledge"
    entity_id: uuid.UUID | None = None


class RecallIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=8, ge=1, le=30)
    scope: Literal["all", "personal", "work"] = "all"


def _mem(m: CtxMemory, names: dict[str, str] | None = None) -> dict[str, Any]:
    return {"id": str(m.id), "kind": m.kind, "content": m.content, "pinned": m.pinned, "source": m.source,
            "entities": [names.get(i, i) for i in m.entity_ids] if names else m.entity_ids,
            "created_at": m.created_at.isoformat(), "scope": m.scope, "source_ref": m.source_ref}


def _ent(e: CtxEntity) -> dict[str, Any]:
    return {"id": str(e.id), "type": e.type, "name": e.name, "aliases": e.aliases, "summary": e.summary,
            "pinned": e.pinned, "updated_at": e.updated_at.isoformat(), "scope": e.scope, "attributes": e.attributes}


def _bad(e: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": str(e)})


# ---- 상태·지침·도구

@router.get("/status")
async def status_(_: User, db: Db) -> dict[str, Any]:
    counts = {
        "memories": await db.scalar(select(func.count()).select_from(CtxMemory).where(CtxMemory.archived_at.is_(None))),
        "entities": await db.scalar(select(func.count()).select_from(CtxEntity)),
        "relations": await db.scalar(select(func.count()).select_from(CtxRelation)),
        "documents": await db.scalar(select(func.count()).select_from(CtxDocument)),
        "chunks": await db.scalar(select(func.count()).select_from(CtxChunk)),
    }
    return {"embedder": "none", "retrieval": "lexical", "model": None,
            "ready": True, "counts": counts,
            "brief": await store.pinned_brief(db)}


@router.get("/instructions", response_model=InstructionsOut)
async def get_instructions(_: User, role: instructions_mod.Role = "common") -> InstructionsOut:
    """역할별 지침(ADR 0013). common 은 접수를 뺀 모든 실행, desk·lead·worker 는 그 역할에만 붙는다."""
    return InstructionsOut(text=instructions_mod.read(role), role=role, is_default=instructions_mod.is_default(role))


@router.put("/instructions", response_model=InstructionsOut)
async def put_instructions(body: InstructionsIO, _: User, db: Db, role: instructions_mod.Role = "common") -> InstructionsOut:
    instructions_mod.write(role, body.text)
    record(db, action="assistant.instructions", detail={"role": role, "chars": len(body.text)})
    await db.commit()
    return InstructionsOut(text=body.text, role=role)


@router.delete("/instructions", response_model=InstructionsOut)
async def reset_instructions(_: User, db: Db, role: instructions_mod.Role = "common") -> InstructionsOut:
    """저장한 지침을 지워 기본값으로 되돌린다."""
    instructions_mod.reset(role)
    record(db, action="assistant.instructions.reset", detail={"role": role})
    await db.commit()
    return InstructionsOut(text=instructions_mod.read(role), role=role, is_default=True)


@router.get("/tools")
async def tools(_: User) -> dict[str, Any]:
    return {"tools": TOOLS}


@router.get("/library")
async def library(_: User, db: Db, scope: Literal["all", "personal", "work"] = "all") -> dict[str, Any]:
    return await library_catalog(db, scope=scope)


@router.post("/recall")
async def recall(body: RecallIn, _: User, db: Db) -> dict[str, Any]:
    hits = await store.recall(db, body.query, body.k, scope=body.scope)
    return {"results": [{"type": h.kind, "id": h.id, "score": round(h.score, 3), "text": h.text, **h.meta} for h in hits]}


# ---- 기억

@router.get("/memories")
async def memories(_: User, db: Db, q: str | None = None, kind: Kind | None = None, pinned: bool | None = None,
                   limit: int = 100) -> dict[str, Any]:
    stmt = select(CtxMemory).where(CtxMemory.archived_at.is_(None))
    if kind:
        stmt = stmt.where(CtxMemory.kind == kind)
    if pinned is not None:
        stmt = stmt.where(CtxMemory.pinned.is_(pinned))
    if q:
        stmt = stmt.where(CtxMemory.content.ilike(f"%{q}%"))
    rows = list(await db.scalars(stmt.order_by(CtxMemory.pinned.desc(), CtxMemory.created_at.desc()).limit(min(limit, 500))))
    ids = {i for m in rows for i in m.entity_ids}
    names = {str(e.id): e.name for e in await db.scalars(select(CtxEntity).where(CtxEntity.id.in_([uuid.UUID(i) for i in ids])))} if ids else {}
    return {"memories": [_mem(m, names) for m in rows]}


@router.post("/memories", status_code=201)
async def add_memory(body: MemoryIn, _: User, db: Db) -> dict[str, Any]:
    try:
        m = await store.remember(db, body.content, kind=body.kind, entities=body.entities, pinned=body.pinned,
                                 scope=body.scope, source_ref=body.source_ref)
    except store.ContextError as e:
        raise _bad(e) from e
    await db.commit()
    return _mem(m)


@router.patch("/memories/{mid}")
async def patch_memory(mid: uuid.UUID, body: MemoryPatch, _: User, db: Db) -> dict[str, Any]:
    m = await db.get(CtxMemory, mid)
    if m is None or m.archived_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기억이 없어요.")
    if body.content is not None and body.content != m.content:
        m.content = body.content
    if body.kind is not None:
        m.kind = body.kind
    if body.pinned is not None:
        m.pinned = body.pinned
    m.updated_at = utcnow()
    await db.commit()
    return _mem(m)


@router.delete("/memories/{mid}", status_code=204)
async def delete_memory(mid: uuid.UUID, _: User, db: Db) -> None:
    if not await store.forget(db, mid):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "기억이 없어요.")
    await db.commit()


# ---- 온톨로지

@router.get("/entities")
async def entities(_: User, db: Db, type: EType | None = None, q: str | None = None) -> dict[str, Any]:
    stmt = select(CtxEntity)
    if type:
        stmt = stmt.where(CtxEntity.type == type)
    if q:
        stmt = stmt.where(or_(CtxEntity.name.ilike(f"%{q}%"), CtxEntity.summary.ilike(f"%{q}%")))
    rows = await db.scalars(stmt.order_by(CtxEntity.pinned.desc(), CtxEntity.updated_at.desc()).limit(300))
    return {"entities": [_ent(e) for e in rows]}


@router.get("/entities/{eid}")
async def entity(eid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    e = await db.get(CtxEntity, eid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목이 없어요.")
    return await store.entity_detail(db, e)


@router.post("/entities", status_code=201)
async def add_entity(body: EntityIn, _: User, db: Db) -> dict[str, Any]:
    try:
        e = await store.upsert_entity(db, body.name, body.type, summary=body.summary, aliases=body.aliases, pinned=body.pinned,
                                      scope=body.scope, attributes=body.attributes)
    except store.ContextError as ex:
        raise _bad(ex) from ex
    await db.commit()
    return _ent(e)


@router.patch("/entities/{eid}")
async def patch_entity(eid: uuid.UUID, body: EntityPatch, _: User, db: Db) -> dict[str, Any]:
    e = await db.get(CtxEntity, eid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목이 없어요.")
    if body.type is not None:
        e.type = body.type
    if body.aliases is not None:
        e.aliases = sorted({a.strip() for a in body.aliases if a.strip()})
    e = await store.upsert_entity(db, e.name, e.type, summary=body.summary, pinned=body.pinned,
                                  scope=e.scope, attributes=body.attributes)
    await db.commit()
    return _ent(e)


@router.delete("/entities/{eid}", status_code=204)
async def delete_entity(eid: uuid.UUID, _: User, db: Db) -> None:
    e = await db.get(CtxEntity, eid)
    if e is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목이 없어요.")
    await db.delete(e)
    await db.commit()


@router.post("/relations", status_code=201)
async def add_relation(body: RelationIn, _: User, db: Db) -> dict[str, Any]:
    try:
        r = await store.link(db, body.subject, body.predicate, body.object, note=body.note)
    except store.ContextError as e:
        raise _bad(e) from e
    await db.commit()
    return {"id": str(r.id)}


@router.delete("/relations/{rid}", status_code=204)
async def delete_relation(rid: uuid.UUID, _: User, db: Db) -> None:
    r = await db.get(CtxRelation, rid)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "관계가 없어요.")
    await db.delete(r)
    await db.commit()


# ---- 지식(RAG 출처)

def _check_source(raw: str) -> Path:
    p = Path(raw).expanduser()
    if not p.is_absolute():
        raise ValueError("절대 경로나 ~ 로 시작하는 경로로 적어 주세요.")
    real = p.resolve()
    if not real.exists():
        raise ValueError("그런 폴더나 파일이 없어요.")
    s = get_settings()
    if not any(is_within(real, Path(r).expanduser().resolve()) for r in s.context_source_roots) \
            or is_within(real, s.home) or is_sensitive(real):
        raise ValueError("홈 폴더 안의 일반 폴더만 색인할 수 있어요(따까리 데이터·민감 폴더 제외).")
    return real


async def _source_out(db: Db, s: CtxSource) -> dict[str, Any]:
    docs = await db.scalar(select(func.count()).select_from(CtxDocument).where(CtxDocument.source_id == s.id))
    chunks = await db.scalar(select(func.coalesce(func.sum(CtxDocument.chunk_count), 0)).where(CtxDocument.source_id == s.id))
    return {"id": str(s.id), "path": s.path, "pattern": s.pattern, "enabled": s.enabled, "documents": docs,
            "chunks": int(chunks or 0), "last_indexed_at": s.last_indexed_at.isoformat() if s.last_indexed_at else None,
            "last_error": s.last_error, "indexing": str(s.id) in _indexing,
            "scope": s.scope, "category": s.category, "entity_id": str(s.entity_id) if s.entity_id else None}


_indexing: set[str] = set()
_tasks: set[asyncio.Task] = set()


def _start_index(source_id: uuid.UUID) -> None:
    sid = str(source_id)
    if sid in _indexing:
        return
    _indexing.add(sid)

    async def run() -> None:
        try:
            async with sessionmaker()() as db:
                src = await db.get(CtxSource, source_id)
                if src is not None:
                    await index_source(db, src)
        except Exception:
            log.exception("indexing failed")
        finally:
            _indexing.discard(sid)

    t = asyncio.create_task(run())
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


@router.get("/sources")
async def sources(_: User, db: Db) -> dict[str, Any]:
    rows = list(await db.scalars(select(CtxSource).order_by(CtxSource.created_at)))
    return {"sources": [await _source_out(db, s) for s in rows]}


@router.post("/sources", status_code=201)
async def add_source(body: SourceIn, _: User, db: Db) -> dict[str, Any]:
    try:
        real = _check_source(body.path)
    except ValueError as e:
        raise _bad(e) from e
    if await db.scalar(select(CtxSource).where(CtxSource.path == str(real))):
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 등록된 출처예요.")
    if body.entity_id is not None:
        entity = await db.get(CtxEntity, body.entity_id)
        if entity is None or entity.scope != body.scope:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "같은 범위의 프로젝트 항목을 지정해 주세요.")
    s = CtxSource(path=str(real), pattern=body.pattern or "**/*.md",
                  scope=body.scope, category=body.category, entity_id=body.entity_id)
    db.add(s)
    await db.commit()
    _start_index(s.id)
    return await _source_out(db, s)


@router.post("/sources/{sid}/reindex")
async def reindex(sid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    s = await db.get(CtxSource, sid)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "출처가 없어요.")
    _start_index(s.id)
    return await _source_out(db, s)


@router.delete("/sources/{sid}", status_code=204)
async def delete_source(sid: uuid.UUID, _: User, db: Db) -> None:
    s = await db.get(CtxSource, sid)
    if s is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "출처가 없어요.")
    await db.delete(s)
    await db.commit()


async def wait_indexing(timeout: float = 30) -> None:
    """테스트 전용: 백그라운드 색인이 끝날 때까지 기다린다."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(asyncio.gather(*list(_tasks)), timeout)
