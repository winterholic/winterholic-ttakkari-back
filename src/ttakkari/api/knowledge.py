"""Authenticated knowledge ledger APIs; append-only history and scoped evidence."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, text

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.knowledge import store
from ttakkari.knowledge_models import KnowledgeAssertion as A
from ttakkari.knowledge_models import KnowledgeRevision as R
from ttakkari.knowledge_models import KnowledgeUnit as U
from ttakkari.models import CtxSource
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/assistant/knowledge", tags=["knowledge"])
Scope = Literal["all", "personal", "work"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("*")
    @classmethod
    def exact_text(cls, value):
        if isinstance(value, str):
            if "\0" in value:
                raise ValueError("unsupported_nul_in_source")
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError("invalid_unicode_source") from exc
        return value


class ImportIn(Strict):
    source_key: str = Field(min_length=1, max_length=500)
    body: str = Field(max_length=2_000_000)
    scope: Literal["personal", "work", "shared"] = "personal"
    source_id: uuid.UUID | None = None
    locator: str | None = Field(default=None, max_length=4000)
    sensitivity: Literal["normal", "sensitive"] = "normal"
    speaker: Literal["document", "user", "assistant", "system", "tool"] = "document"
    event_time: AwareDatetime | None = None


class AssertionIn(Strict):
    unit_id: uuid.UUID
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=1600)
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    object: str = Field(min_length=1, max_length=4000)
    modality: Literal[
        "fact", "proposal", "question", "inference", "decision", "preference"
    ] = "fact"
    polarity: bool = True
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None


class ReviewIn(Strict):
    expected_version: int = Field(ge=1)
    state: Literal["candidate", "active", "disputed", "superseded", "retracted"]
    review_level: Literal["candidate", "source_reviewed", "user_confirmed"] | None = (
        None
    )
    reason: str = Field(min_length=1, max_length=2000)


class RecallIn(Strict):
    query: str = Field(min_length=1, max_length=1000)
    scope: Scope = "all"
    k: int = Field(default=8, ge=1, le=30)
    hops: int = Field(default=1, ge=1, le=2)
    as_of: AwareDatetime | None = None


@asynccontextmanager
async def transaction(db: Db):
    try:
        yield
        await db.commit()
    except store.KnowledgeError as e:
        await db.rollback()
        raise HTTPException(e.status, e.code) from e


@router.post("/imports", status_code=201)
async def import_text(body: ImportIn, _: User, db: Db) -> dict:
    async with transaction(db):
        revision = await store.import_text(db, **body.model_dump())
        result = store.revision_out(revision)
        record(
            db,
            actor="user",
            action="knowledge.import",
            target_type="knowledge_revision",
            target_id=revision.id,
            detail={"body_chars": len(body.body)},
        )
    return result


@router.get("/revisions")
async def revisions(
    _: User,
    db: Db,
    scope: Scope = "all",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    q = (
        select(R)
        .outerjoin(CtxSource, CtxSource.id == R.source_id)
        .where(*store.visible(scope, current=False))
    )
    rows = list(
        await db.scalars(
            q.order_by(R.created_at.desc(), R.id).offset(offset).limit(limit)
        )
    )
    return {
        "items": [store.revision_out(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


@router.get("/units")
async def units(
    _: User,
    db: Db,
    scope: Scope = "all",
    revision_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    q = (
        select(U, R)
        .join(R, R.id == U.revision_id)
        .outerjoin(CtxSource, CtxSource.id == R.source_id)
    )
    q = q.where(*store.visible(scope, current=revision_id is None))
    if revision_id:
        q = q.where(R.id == revision_id)
    total = await db.scalar(select(func.count()).select_from(q.subquery()))
    rows = (
        await db.execute(q.order_by(U.source_offset, U.id).offset(offset).limit(limit))
    ).all()
    return {
        "items": [store.unit_out(u, r) for u, r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/assertions", status_code=201)
async def add_assertion(body: AssertionIn, _: User, db: Db) -> dict:
    async with transaction(db):
        a = await store.add_assertion(db, **body.model_dump())
        result = store.assertion_out(a)
        record(
            db,
            actor="user",
            action="knowledge.assertion_candidate",
            target_type="knowledge_assertion",
            target_id=a.id,
        )
    return result


@router.get("/assertions")
async def assertions(
    _: User,
    db: Db,
    scope: Scope = "all",
    state: str | None = None,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    from ttakkari.context.search import scope_clause

    q = (
        select(A)
        .where(scope_clause(A.scope, scope))
        .order_by(A.created_at.desc(), A.id)
    )
    if state:
        q = q.where(A.state == state)
    rows = list(await db.scalars(q.offset(offset).limit(limit)))
    visible = []
    for a in rows:
        try:
            visible.append(await store.detail(db, a.id, scope))
        except store.KnowledgeError as exc:
            if exc.status != 404:
                raise
    return {"items": visible, "limit": limit, "offset": offset}


@router.get("/assertions/{aid}")
async def assertion(aid: uuid.UUID, _: User, db: Db, scope: Scope = "all") -> dict:
    async with transaction(db):
        return await store.detail(db, aid, scope)


@router.post("/assertions/{aid}/reviews")
async def review(aid: uuid.UUID, body: ReviewIn, _: User, db: Db) -> dict:
    async with transaction(db):
        a = await store.review(db, aid, **body.model_dump())
        result = store.assertion_out(a)
        record(
            db,
            actor="user",
            action="knowledge.review",
            target_type="knowledge_assertion",
            target_id=a.id,
            detail={"state": a.state, "version": a.version},
        )
    return result


@router.post("/recall")
async def recall(body: RecallIn, _: User, db: Db) -> dict:
    return await store.recall(db, **body.model_dump())


@router.get("/status")
async def status(_: User, db: Db, request: Request, scope: Scope = "all") -> dict:
    from ttakkari.context.search import scope_clause

    revisions = list(
        await db.scalars(
            select(R)
            .outerjoin(CtxSource, CtxSource.id == R.source_id)
            .where(*store.visible(scope, current=False))
        )
    )
    ids = [r.id for r in revisions]
    outcomes = (
        await db.execute(
            select(U.outcome, func.count())
            .where(U.revision_id.in_(ids))
            .group_by(U.outcome)
        )
    ).all()
    states = (
        await db.execute(
            select(A.state, func.count())
            .where(scope_clause(A.scope, scope))
            .group_by(A.state)
        )
    ).all()
    identity = (
        await db.execute(text("SELECT current_database(), inet_server_port()"))
    ).one()
    return {
        "database": identity[0],
        "database_port": identity[1],
        "runtime_revision": getattr(request.app.state, "revision", None),
        "counts": {
            "revisions": len(ids),
            "active_revisions": sum(r.active for r in revisions),
            "units": sum(n for _, n in outcomes),
            "outcomes": dict(outcomes),
            "assertion_states": dict(states),
        },
        "scope": scope,
        "limitations": [
            "No corpus-wide semantic retention estimate",
            "Candidate assertions require evidence review",
        ],
    }


class ExtractionIn(Strict):
    revision_id: uuid.UUID
    extractor_version: str = Field(
        default="evidence-extraction-v2", min_length=1, max_length=100
    )


class ClaimIn(Strict):
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: str = Field(min_length=1, max_length=1600)
    subject: str = Field(min_length=1, max_length=200)
    predicate: str = Field(min_length=1, max_length=100)
    object: str = Field(min_length=1, max_length=4000)
    modality: Literal[
        "fact", "proposal", "question", "inference", "decision", "preference"
    ] = "fact"
    polarity: bool = True
    valid_from: AwareDatetime | None = None
    valid_to: AwareDatetime | None = None


class ExtractionResultIn(Strict):
    unit_id: uuid.UUID
    outcome: Literal["candidates", "no_claim", "unsupported", "error"]
    claims: list[ClaimIn] = Field(default_factory=list, max_length=30)
    reason: str | None = Field(default=None, max_length=500)


class ExtractionBatchIn(Strict):
    expected_cursor: int = Field(ge=0)
    results: list[ExtractionResultIn] = Field(max_length=100)


@router.post("/extractions", status_code=201)
async def start_extraction(body: ExtractionIn, _: User, db: Db) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        job = await extraction.start(db, **body.model_dump())
        result = extraction.job_out(job)
    return result


@router.get("/extractions/{jid}/batch")
async def next_extraction(
    jid: uuid.UUID,
    _: User,
    db: Db,
    scope: Scope = "all",
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        return await extraction.next_batch(db, jid, scope=scope, limit=limit)


@router.post("/extractions/{jid}/batches")
async def submit_extraction(
    jid: uuid.UUID, body: ExtractionBatchIn, _: User, db: Db
) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        job = await extraction.submit(db, jid, **body.model_dump())
        result = extraction.job_out(job)
    return result


class CorrectionIn(Strict):
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1, max_length=2000)
    replacement: AssertionIn


@router.post("/assertions/{aid}/corrections")
async def correction(aid: uuid.UUID, body: CorrectionIn, _: User, db: Db) -> dict:
    async with transaction(db):
        result = await store.correct(db, aid, **body.model_dump())
        record(
            db,
            actor="user",
            action="knowledge.correct",
            target_type="knowledge_assertion",
            target_id=aid,
        )
    return result


@router.get("/assertions/{aid}/impact")
async def impact(aid: uuid.UUID, _: User, db: Db, scope: Scope = "all") -> dict:
    async with transaction(db):
        return await store.impact(db, aid, scope)


# Agent routes clamp scope from the session, never from model-supplied arguments.
from ttakkari.orchestrator.runtime import (
    OrchestratorSession as AgentSession,
)

agent_router = APIRouter(prefix="/api/agent/knowledge", tags=["agent-knowledge"])


async def agent_scope(sid, db):
    from ttakkari.api.agent import _session

    session = await _session(db, sid)
    return "work" if session.pool == "work" else "all"


@agent_router.post("/recall")
async def agent_recall(body: RecallIn, sid: AgentSession, db: Db) -> dict:
    return await store.recall(
        db, scope=await agent_scope(sid, db), **body.model_dump(exclude={"scope"})
    )


@agent_router.get("/assertions/{aid}")
async def agent_assertion(aid: uuid.UUID, sid: AgentSession, db: Db) -> dict:
    async with transaction(db):
        return await store.detail(db, aid, await agent_scope(sid, db))


@agent_router.post("/assertions/{aid}/reviews")
async def agent_review(
    aid: uuid.UUID, body: ReviewIn, sid: AgentSession, db: Db
) -> dict:
    if body.review_level == "user_confirmed":
        raise HTTPException(403, "user_confirmation_required")
    async with transaction(db):
        a = await store.review(
            db,
            aid,
            actor="agent",
            scope=await agent_scope(sid, db),
            **body.model_dump(),
        )
        result = store.assertion_out(a)
        record(
            db,
            actor="agent",
            action="knowledge.review",
            target_type="knowledge_assertion",
            target_id=aid,
            detail={"state": a.state, "version": a.version},
        )
    return result


@agent_router.post("/extractions", status_code=201)
async def agent_start_extraction(body: ExtractionIn, sid: AgentSession, db: Db) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        job = await extraction.start(
            db,
            scope=await agent_scope(sid, db),
            execution=await extraction.execution_context(db, sid),
            **body.model_dump(),
        )
        result = extraction.job_out(job)
    return result


@agent_router.get("/extractions/{jid}/batch")
async def agent_next_extraction(
    jid: uuid.UUID, sid: AgentSession, db: Db, limit: int = Query(20, ge=1, le=100)
) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        return await extraction.next_batch(
            db, jid, scope=await agent_scope(sid, db), limit=limit
        )


@agent_router.post("/extractions/{jid}/batches")
async def agent_submit_extraction(
    jid: uuid.UUID, body: ExtractionBatchIn, sid: AgentSession, db: Db
) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        job = await extraction.submit(
            db,
            jid,
            scope=await agent_scope(sid, db),
            execution=await extraction.execution_context(db, sid),
            **body.model_dump(),
        )
        result = extraction.job_out(job)
    return result


@agent_router.get("/revisions")
async def agent_revisions(
    sid: AgentSession,
    db: Db,
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> dict:
    scope = await agent_scope(sid, db)
    q = (
        select(R)
        .outerjoin(CtxSource, CtxSource.id == R.source_id)
        .where(*store.visible(scope))
    )
    rows = list(
        await db.scalars(
            q.order_by(R.created_at.desc(), R.id).offset(offset).limit(limit)
        )
    )
    return {
        "items": [store.revision_out(r) for r in rows],
        "scope": scope,
        "limit": limit,
        "offset": offset,
    }


@router.get("/extractions/{jid}")
async def extraction_details(
    jid: uuid.UUID, _: User, db: Db, scope: Scope = "all"
) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        return await extraction.details(db, jid, scope)


@router.post("/extractions/{jid}/retry", status_code=201)
async def retry_extraction(jid: uuid.UUID, body: Strict, _: User, db: Db) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        return extraction.job_out(await extraction.retry_failed(db, jid))


@agent_router.get("/extractions/{jid}")
async def agent_extraction_details(jid: uuid.UUID, sid: AgentSession, db: Db) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        return await extraction.details(db, jid, await agent_scope(sid, db))


@agent_router.post("/extractions/{jid}/retry", status_code=201)
async def agent_retry_extraction(jid: uuid.UUID, sid: AgentSession, db: Db) -> dict:
    from ttakkari.knowledge import extraction

    async with transaction(db):
        return extraction.job_out(
            await extraction.retry_failed(
                db,
                jid,
                scope=await agent_scope(sid, db),
                execution=await extraction.execution_context(db, sid),
            )
        )
