"""맥락 도서관 화면용 읽기 전용 API. 색인·수정·삭제는 비서 관리(assistant.py)에만 있다."""

from __future__ import annotations

import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ttakkari.api.deps import Db
from ttakkari.context import insight
from ttakkari.models import CtxChunk, CtxDocument, CtxEntity, CtxMemory, CtxSource
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/library", tags=["library"])
Scope = Literal["all", "personal", "work"]


class SearchLabIn(BaseModel):
    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=10, ge=1, le=30)
    limit: int | None = Field(default=None, ge=1, le=100)
    offset: int = Field(default=0, ge=0)
    scope: Scope = "all"


def _missing(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what}이(가) 없어요.")


@router.get("/overview")
async def overview(_: User, db: Db, scope: Scope = "all") -> dict[str, Any]:
    return await insight.overview(db, scope)


@router.get("/tree")
async def tree(_: User, db: Db, scope: Scope = "all") -> dict[str, Any]:
    return await insight.tree(db, scope)


@router.get("/sources/{sid}/documents")
async def source_documents(sid: uuid.UUID, _: User, db: Db, scope: Scope = "all") -> dict[str, Any]:
    s = await db.get(CtxSource, sid)
    if s is None or not _in_scope(s.scope, scope):
        raise _missing("출처")
    return await insight.source_documents(db, s)


@router.get("/documents/{did}/chunks")
async def document_chunks(did: uuid.UUID, _: User, db: Db, scope: Scope = "all",
                          limit: int = Query(default=12, ge=1, le=50)) -> dict[str, Any]:
    d = await db.get(CtxDocument, did)
    s = await db.get(CtxSource, d.source_id) if d else None
    if d is None or s is None or not _in_scope(s.scope, scope):
        raise _missing("문서")
    return await insight.document_chunks(db, d, limit)


@router.get("/graph")
async def graph(_: User, db: Db, scope: Scope = "all") -> dict[str, Any]:
    return await insight.graph(db, scope)


@router.get("/entities/{eid}")
async def entity(eid: uuid.UUID, _: User, db: Db, scope: Scope = "all") -> dict[str, Any]:
    e = await db.get(CtxEntity, eid)
    if e is None or not _in_scope(e.scope, scope):
        raise _missing("항목")
    return await insight.entity_evidence(db, e, scope)


@router.get("/quality")
async def quality(_: User, db: Db, scope: Scope = "all") -> dict[str, Any]:
    return await insight.quality(db, scope)


@router.post("/search-lab")
async def search_lab(body: SearchLabIn, _: User, db: Db) -> dict[str, Any]:
    """recall 과 같은 검색을 돌리고, 결과마다 걸린 단어·위치·경로(키워드/엔터티/유사)를 덧붙인다. 읽기 전용이다."""
    return await insight.search_lab(db, body.query, body.limit or body.k, body.scope, body.offset)


@router.get("/items/{kind}/{iid}")
async def item_detail(kind: Literal["memory", "chunk", "entity"], iid: uuid.UUID, _: User, db: Db,
                      scope: Scope = "all") -> dict[str, Any]:
    if kind == "memory":
        item = await db.get(CtxMemory, iid)
        if item is None or item.archived_at is not None or not _in_scope(item.scope, scope):
            raise _missing("항목")
        return await insight.memory_detail(db, item, scope)
    if kind == "chunk":
        item = await db.get(CtxChunk, iid)
        doc = await db.get(CtxDocument, item.document_id) if item else None
        source = await db.get(CtxSource, doc.source_id) if doc else None
        if item is None or doc is None or source is None or not _in_scope(source.scope, scope):
            raise _missing("항목")
        return await insight.chunk_detail(db, item, doc, source)
    item = await db.get(CtxEntity, iid)
    if item is None or not _in_scope(item.scope, scope):
        raise _missing("항목")
    return await insight.entity_detail_page(db, item, scope)


def _in_scope(value: str, scope: str) -> bool:
    return scope == "all" or value in (scope, "shared")
