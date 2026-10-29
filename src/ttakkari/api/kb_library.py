"""Owner KB API: one library of documents, citations, lexical search and optional dense fusion.

C2: personal/work 구획은 없다. scope 파라미터는 옛 클라이언트 호환으로 받아도 무시한다. 소유자는 전체를 읽는다.
회사 계정 엔진 세션이 읽는 범위만 문서별 접근 속성(pool)이 정하며 PUT /documents/{id}/engine-access 로 소유자가 바꾼다.
Model rankings are evidence candidates; KB7 quality approval is separate.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import DBAPIError, IntegrityError

from ttakkari import kb_library as lib
from ttakkari import kb_retrieval
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.collector.library import pool_for_scope
from ttakkari.kb_store import KbInputError
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/kb", tags=["kb-library"])
LegacyScope = Annotated[str | None, Query(deprecated=True, description="무시한다. 도서관은 하나다.")]


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str | None = Field(default=None, deprecated=True)  # 무시한다. 옛 클라이언트가 보내도 거부하지 않는다.
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(min_length=1, max_length=lib.MAX_TEXT_CHARS)
    source_key: str | None = Field(default=None, min_length=1, max_length=500)
    lang: str | None = Field(default=None, max_length=16)
    locator: str | None = Field(default=None, max_length=4000)

    @field_validator("title", "text", "source_key", "lang", "locator")
    @classmethod
    def valid_text(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if "\x00" in value:
            raise ValueError("unsupported_nul")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid_unicode") from exc
        return value

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("blank_title")
        return value.strip()


@router.get("/summary")
async def kb_summary(_: User, db: Db, scope: LegacyScope = None) -> dict:
    return {**await lib.summary(db), "retrieval": await kb_retrieval.status(db)}


@router.get("/observations")
async def kb_observations(
    _: User, db: Db,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(5, ge=1, le=10),
    offset: int = Query(0, ge=0),
    revision_limit: int = Query(10, ge=1, le=10),
    passage_limit: int = Query(5, ge=1, le=10),
) -> dict:
    """Read bounded KB visibility/provenance metadata; never returns document or passage bodies."""
    return await lib.observation_inventory(
        db, pool="personal", principal="owner", query=q, limit=limit, offset=offset,
        revision_limit=revision_limit, passage_limit=passage_limit,
    )


@router.get("/documents")
async def kb_documents(
    _: User, db: Db,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    scope: LegacyScope = None,
) -> dict:
    return await lib.list_documents(db, q=q, limit=limit, offset=offset)


@router.post("/documents", status_code=201)
async def kb_register(body: RegisterIn, _: User, db: Db) -> dict:
    try:
        result = await lib.register_text(
            db, title=body.title, text=body.text, source_key=body.source_key, lang=body.lang,
            locator=body.locator, pool=pool_for_scope(body.scope),
        )
        record(
            db, actor="user", action="kb.register", target_type="kb_revision", target_id=result.revision_id,
            detail={"chars": len(body.text), "created": result.created},
        )
        await db.commit()
    except KbInputError as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc
    except IntegrityError as exc:
        # 같은 바이트를 동시에 등록하면 한쪽이 unique 충돌이다. 재시도하면 기존 행을 받는다(멱등).
        await db.rollback()
        raise HTTPException(409, "kb_register_conflict") from exc
    except DBAPIError as exc:
        await db.rollback()
        raise HTTPException(422, "kb_register_rejected") from exc
    return {
        "document_id": str(result.document_id),
        "revision_id": str(result.revision_id),
        "rev_no": result.rev_no,
        "created": result.created,
        "passage_count": result.passage_count,
    }


@router.get("/documents/{document_id}")
async def kb_document(
    document_id: uuid.UUID, _: User, db: Db,
    revision_id: uuid.UUID | None = None,
    passage_limit: int = Query(100, ge=1, le=500),
    passage_offset: int = Query(0, ge=0),
    scope: LegacyScope = None,
) -> dict:
    try:
        return await lib.get_document(
            db, document_id, revision_id=revision_id, passage_limit=passage_limit, passage_offset=passage_offset
        )
    except lib.KbNotFound as exc:
        raise HTTPException(404, f"kb_{exc}_not_found") from exc


@router.get("/search")
async def kb_search(
    _: User, db: Db,
    q: str = Query(min_length=1, max_length=500),
    limit: int = Query(10, ge=1, le=30),
    offset: int = Query(0, ge=0),
    document_id: uuid.UUID | None = None,
    current_only: bool = True,
    scope: LegacyScope = None,
    mode: Literal["lexical", "hybrid"] = "lexical",
) -> dict:
    if mode == "hybrid":
        try:
            return await kb_retrieval.search(db, query=q, limit=limit, offset=offset,
                                             document_id=document_id, current_only=current_only)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, "kb_semantic_temporarily_unavailable") from exc
    return await lib.search_passages(
        db, query=q, limit=limit, offset=offset, document_id=document_id, current_only=current_only
    )


@router.get("/passages/{passage_id}/evidence")
async def kb_evidence(
    passage_id: uuid.UUID, _: User, db: Db,
    start: int | None = Query(None, ge=0),
    end: int | None = Query(None, ge=1),
    scope: LegacyScope = None,
) -> dict:
    try:
        return await lib.get_evidence(db, passage_id, start=start, end=end)
    except lib.KbNotFound as exc:
        raise HTTPException(404, f"kb_{exc}_not_found") from exc
    except KbInputError as exc:
        raise HTTPException(422, str(exc)) from exc


class EngineAccessIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    company_engine: bool


@router.put("/documents/{document_id}/engine-access")
async def kb_engine_access(document_id: uuid.UUID, body: EngineAccessIn, _: User, db: Db) -> dict:
    """회사 계정 엔진(work 세션)이 이 문서를 읽을 수 있는지 소유자가 정한다. 도서관 구획이 아니라 접근 권한이다."""
    try:
        pool = await lib.set_engine_access(db, document_id, company_engine=body.company_engine)
    except lib.KbNotFound as exc:
        raise HTTPException(404, f"kb_{exc}_not_found") from exc
    record(db, actor="user", action="kb.engine_access", target_type="kb_document", target_id=document_id,
           detail={"company_engine": body.company_engine})
    await db.commit()
    return {"document_id": str(document_id), "company_engine": pool == "work"}
