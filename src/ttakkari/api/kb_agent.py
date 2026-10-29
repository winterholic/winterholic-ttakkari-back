"""Read-only general knowledge tools. The company-engine (work) pool is derived from the authenticated session; other sessions read the whole library."""
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
                 limit: int = Query(10, ge=1, le=30), offset: int = Query(0, ge=0),
                 document_id: uuid.UUID | None = None, current_only: bool = True,
                 mode: Literal["lexical", "hybrid"] = "lexical"):
    if mode == "hybrid":
        try:
            return await kb_retrieval.search(db, query=q, limit=limit, offset=offset,
                                              document_id=document_id, current_only=current_only,
                                              pool=ctx.pool)
        except RuntimeError as exc:
            raise HTTPException(503, "kb_semantic_temporarily_unavailable") from exc
    return await lib.search_passages(db, query=q, limit=limit, offset=offset,
                                      document_id=document_id, current_only=current_only, pool=ctx.pool)


@router.get("/summary")
async def summary(ctx: Access, db: Db):
    return await lib.summary(db, pool=ctx.pool)


@router.get("/observations")
async def observations(ctx: Access, db: Db, q: str | None = Query(default=None, max_length=200),
                       limit: int = Query(5, ge=1, le=10), offset: int = Query(0, ge=0),
                       revision_limit: int = Query(10, ge=1, le=10),
                       passage_limit: int = Query(5, ge=1, le=10)):
    return await lib.observation_inventory(
        db, pool=ctx.pool, query=q, limit=limit, offset=offset,
        revision_limit=revision_limit, passage_limit=passage_limit,
    )


@router.get("/documents")
async def documents(ctx: Access, db: Db, q: str | None = Query(default=None, max_length=200),
                    limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)):
    return await lib.list_documents(db, q=q, limit=limit, offset=offset, pool=ctx.pool)


@router.get("/documents/{document_id}")
async def document(document_id: uuid.UUID, ctx: Access, db: Db,
                   revision_id: uuid.UUID | None = None,
                   passage_limit: int = Query(100, ge=1, le=500),
                   passage_offset: int = Query(0, ge=0)):
    try:
        return await lib.get_document(db, document_id, revision_id=revision_id,
                                       passage_limit=passage_limit, passage_offset=passage_offset,
                                       pool=ctx.pool)
    except lib.KbNotFound as exc:
        raise HTTPException(404, "kb_document_not_found") from exc


@router.get("/passages/{passage_id}/evidence")
async def evidence(passage_id: uuid.UUID, ctx: Access, db: Db,
                   start: int | None = Query(None, ge=0), end: int | None = Query(None, ge=1)):
    try:
        return await lib.get_evidence(db, passage_id, start=start, end=end, pool=ctx.pool)
    except lib.KbNotFound as exc:
        raise HTTPException(404, "kb_passage_not_found") from exc
    except KbInputError as exc:
        raise HTTPException(422, str(exc)) from exc
