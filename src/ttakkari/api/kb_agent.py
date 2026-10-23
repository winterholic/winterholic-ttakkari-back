"""Read-only general knowledge tools. Personal/work scope is derived from the authenticated session."""
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from ttakkari import kb_library as lib
from ttakkari import kb_retrieval
from ttakkari.api.collect import agent_access
from ttakkari.api.deps import Db
from ttakkari.collector.contracts import AuthContext
from ttakkari.kb_store import KbInputError

router = APIRouter(prefix="/api/agent/kb", tags=["kb-library"])
Access = Annotated[AuthContext, Depends(agent_access)]


@router.get("/search")
async def search(ctx: Access, db: Db, q: str = Query(min_length=1, max_length=500),
                 limit: int = Query(10, ge=1, le=30), mode: Literal["lexical", "hybrid"] = "lexical"):
    if mode == "hybrid":
        try:
            return await kb_retrieval.search(db, query=q, limit=limit, scope=ctx.scope)
        except RuntimeError as exc:
            raise HTTPException(503, "kb_semantic_temporarily_unavailable") from exc
    return await lib.search_passages(db, query=q, limit=limit, offset=0,
                                      document_id=None, current_only=True, scope=ctx.scope)


@router.get("/documents")
async def documents(ctx: Access, db: Db, q: str | None = Query(default=None, max_length=200),
                    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await lib.list_documents(db, q=q, limit=limit, offset=offset, scope=ctx.scope)


@router.get("/documents/{document_id}")
async def document(document_id: uuid.UUID, ctx: Access, db: Db):
    try:
        return await lib.get_document(db, document_id, revision_id=None, passage_limit=100,
                                       passage_offset=0, scope=ctx.scope)
    except lib.KbNotFound as exc:
        raise HTTPException(404, "kb_document_not_found") from exc


@router.get("/passages/{passage_id}/evidence")
async def evidence(passage_id: uuid.UUID, ctx: Access, db: Db,
                   start: int | None = Query(None, ge=0), end: int | None = Query(None, ge=1)):
    try:
        return await lib.get_evidence(db, passage_id, start=start, end=end, scope=ctx.scope)
    except lib.KbNotFound as exc:
        raise HTTPException(404, "kb_passage_not_found") from exc
    except KbInputError as exc:
        raise HTTPException(422, str(exc)) from exc
