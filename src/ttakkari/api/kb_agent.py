"""Read-only general knowledge tools. 세션 pool(personal/work)에서 접근 범위를 유도하고 kb_access 단일 규칙으로 거른다.

기존 정책 그대로: 개인 세션은 global 문서만, 회사 세션은 회사(work_only) 문서만 본다. 회사 세션에 개인 자료를 열지 않고
개인 세션에 회사 자료를 열지 않는다. 응답 필드는 기존 것을 보존하고 v3 필드(section_path·locator_meta·canonical_facts 등)만 더한다.
"""
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ttakkari import kb_library as lib
from ttakkari import kb_retrieval
from ttakkari.api.collect import agent_access
from ttakkari.api.deps import Db
from ttakkari.collector.contracts import AuthContext
from ttakkari.kb_access import KbAccessContext, from_legacy_scope
from ttakkari.kb_store import KbInputError

router = APIRouter(prefix="/api/agent/kb", tags=["kb-library"])
Access = Annotated[AuthContext, Depends(agent_access)]


def agent_kb_access(ctx: AuthContext) -> KbAccessContext:
    """세션 범위(ctx.scope: personal|work)를 kb_access 컨텍스트로. 'work' 만 회사 문서, 그 밖은 모두 개인(global) 기준이다."""
    return from_legacy_scope("work" if ctx.scope == "work" else "personal", tenant_id=ctx.tenant_id)


@router.get("/search")
async def search(ctx: Access, db: Db, q: str = Query(min_length=1, max_length=500),
                 limit: int = Query(10, ge=1, le=30), mode: Literal["lexical", "hybrid"] = "lexical",
                 include_candidates: bool = Query(False, description="후보(candidate) 관계를 '후보' 표시와 낮은 가중치로 1-hop 에 포함")):
    access = agent_kb_access(ctx)
    if mode == "hybrid":
        try:
            return await kb_retrieval.search(db, query=q, limit=limit, access_context=access, include_candidates=include_candidates)
        except RuntimeError as exc:
            raise HTTPException(503, "kb_semantic_temporarily_unavailable") from exc
    return await lib.search_passages(db, query=q, limit=limit, offset=0,
                                      document_id=None, current_only=True, access=access)


@router.get("/documents")
async def documents(ctx: Access, db: Db, q: str | None = Query(default=None, max_length=200),
                    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await lib.list_documents(db, q=q, limit=limit, offset=offset, access=agent_kb_access(ctx))


@router.get("/documents/{document_id}")
async def document(document_id: uuid.UUID, ctx: Access, db: Db):
    try:
        return await lib.get_document(db, document_id, revision_id=None, passage_limit=100,
                                       passage_offset=0, access=agent_kb_access(ctx))
    except lib.KbNotFound as exc:
        raise HTTPException(404, "kb_document_not_found") from exc


@router.get("/passages/{passage_id}/evidence")
async def evidence(passage_id: uuid.UUID, ctx: Access, db: Db,
                   start: int | None = Query(None, ge=0), end: int | None = Query(None, ge=1)):
    try:
        return await lib.get_evidence(db, passage_id, start=start, end=end, access=agent_kb_access(ctx))
    except lib.KbNotFound as exc:
        raise HTTPException(404, "kb_passage_not_found") from exc
    except KbInputError as exc:
        raise HTTPException(422, str(exc)) from exc
