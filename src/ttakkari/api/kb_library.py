"""범용 지식 도서관(kb) 사용자 API. 개인 맥락 도서관(/api/assistant/knowledge)과 라우터·테이블·검색 풀이 다르다(ADR 0063 D1).

kb 원장에는 아직 scope 열이 없다(v2 G8). 소유자 인증만 요구하며 개인/회사 scope 필터는 scope 열이 생기는 단위에서 건다.
읽기 전용 엔드포인트와 등록(POST /documents)만 있다. 등록은 기존 수집 함수(멱등·불변)를 그대로 쓴다.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import DBAPIError, IntegrityError

from ttakkari import kb_library as lib
from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.kb_store import KbInputError
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/kb", tags=["kb-library"])


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
async def kb_summary(_: User, db: Db) -> dict:
    return await lib.summary(db)


@router.get("/documents")
async def kb_documents(
    _: User, db: Db,
    q: str | None = Query(None, max_length=200),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    return await lib.list_documents(db, q=q, limit=limit, offset=offset)


@router.post("/documents", status_code=201)
async def kb_register(body: RegisterIn, _: User, db: Db) -> dict:
    try:
        result = await lib.register_text(
            db, title=body.title, text=body.text, source_key=body.source_key, lang=body.lang,
            locator=body.locator,
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
) -> dict:
    return await lib.search_passages(
        db, query=q, limit=limit, offset=offset, document_id=document_id, current_only=current_only
    )


@router.get("/passages/{passage_id}/evidence")
async def kb_evidence(
    passage_id: uuid.UUID, _: User, db: Db,
    start: int | None = Query(None, ge=0),
    end: int | None = Query(None, ge=1),
) -> dict:
    try:
        return await lib.get_evidence(db, passage_id, start=start, end=end)
    except lib.KbNotFound as exc:
        raise HTTPException(404, f"kb_{exc}_not_found") from exc
    except KbInputError as exc:
        raise HTTPException(422, str(exc)) from exc
