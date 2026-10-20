"""Research Lab user and session-scoped agent API. No arbitrary command runner."""

from __future__ import annotations

import asyncio
import json
import platform
import uuid
from typing import Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select, text

from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.knowledge import store
from ttakkari.knowledge_models import KnowledgeUnit
from ttakkari.models import utcnow
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.research import benchmark
from ttakkari.research import service as svc
from ttakkari.research.models import Experiment, Node, Relation, Study
from ttakkari.research.rules import comparable, rank_methods
from ttakkari.security.auth import User

router = APIRouter(prefix="/api/research", tags=["research"])
agent_router = APIRouter(prefix="/api/agent/research", tags=["agent"])


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def bounded(self):
        serialized = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, allow_nan=False
        )
        if len(serialized) > 300_000 or "\\u0000" in serialized or "\0" in serialized:
            raise ValueError("research_payload_limit_or_nul")
        return self


class StudyIn(Strict):
    title: str = Field(min_length=1, max_length=240)
    goal: str = Field(min_length=1, max_length=6000)
    scope: Literal["personal", "work"] = "personal"
    environment: dict[str, Any] = Field(default_factory=dict)
    criteria: list[str] = Field(default_factory=list, max_length=30)


class StudyPatch(Strict):
    expected_version: int = Field(ge=1)
    phase: str | None = Field(default=None, min_length=1, max_length=100)
    environment: dict[str, Any] | None = None
    next_actions: list[str] | None = Field(default=None, max_length=100)


class SourceIn(Strict):
    source_key: str = Field(min_length=1, max_length=500)
    title: str = Field(min_length=1, max_length=240)
    locator: str = Field(min_length=1, max_length=2000)
    body: str = Field(min_length=1, max_length=200_000)


class NodeIn(Strict):
    kind: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=240)
    summary: str = Field(default="", max_length=6000)
    conditions: dict[str, Any] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    unit_id: uuid.UUID | None = None
    start: int | None = Field(default=None, ge=0)
    quote: str | None = Field(default=None, min_length=1, max_length=6000)
    supersedes_id: uuid.UUID | None = None


class RelationIn(Strict):
    from_id: uuid.UUID
    to_id: uuid.UUID
    predicate: str = Field(min_length=1, max_length=100)
    rationale: str = Field(min_length=1, max_length=6000)
    conditions: dict[str, Any] = Field(default_factory=dict)


class Budget(Strict):
    seconds: int = Field(default=30, ge=1, le=600)
    memory_mb: int = Field(default=256, ge=64, le=4096)


class ExperimentIn(Strict):
    title: str = Field(min_length=1, max_length=240)
    hypothesis: str = Field(min_length=1, max_length=6000)
    rationale: str = Field(min_length=1, max_length=6000)
    adapter: str = Field(default="external", min_length=1, max_length=100)
    config: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    budget: Budget = Field(default_factory=Budget)
    method_ids: list[uuid.UUID] = Field(default_factory=list, max_length=30)


class ReportIn(Strict):
    result: dict[str, Any]
    interpretation: str = Field(min_length=1, max_length=6000)
    next_actions: list[str] = Field(default_factory=list, max_length=100)


class SelectionIn(Strict):
    query: str = Field(default="", max_length=500)
    budget_seconds: int = Field(default=60, ge=1, le=86400)
    environment: dict[str, Any] = Field(default_factory=dict)


@router.get("/studies")
async def studies(
    _: User,
    db: Db,
    scope: Literal["all", "personal", "work"] = "all",
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    q = select(Study)
    if scope != "all":
        q = q.where(Study.scope == scope)
    return [
        svc.out(s)
        for s in (
            await db.scalars(
                q.order_by(Study.updated_at.desc()).offset(offset).limit(limit)
            )
        ).all()
    ]


@router.post("/studies", status_code=201)
async def create_study(body: StudyIn, _: User, db: Db):
    row = Study(**body.model_dump())
    db.add(row)
    await db.flush()
    svc.event(db, row.id, "study_created", str(_), goal=row.goal)
    await db.commit()
    return svc.out(row)


@router.patch("/studies/{study_id}")
async def patch_study(study_id: uuid.UUID, body: StudyPatch, _: User, db: Db):
    row = await svc.get_study(db, study_id, lock=True)
    if row.version != body.expected_version:
        raise HTTPException(409, "research_stale_version")
    changes = body.model_dump(exclude={"expected_version"}, exclude_none=True)
    for key, value in changes.items():
        setattr(row, key, value)
    row.version += 1
    svc.event(db, row.id, "study_updated", str(_), version=row.version, changes=changes)
    await db.commit()
    return svc.out(row)


@router.get("/studies/{study_id}/resume")
async def resume(study_id: uuid.UUID, _: User, db: Db):
    return await svc.snapshot(db, await svc.get_study(db, study_id))


@router.post("/studies/{study_id}/sources", status_code=201)
async def add_source(study_id: uuid.UUID, body: SourceIn, _: User, db: Db):
    study = await svc.get_study(db, study_id)
    try:
        revision = await store.import_text(
            db,
            source_key=body.source_key,
            locator=body.locator,
            body=body.body,
            scope=study.scope,
            origin="research",
        )
    except store.KnowledgeError as exc:
        raise HTTPException(exc.status, exc.code) from exc
    units = (
        await db.scalars(
            select(KnowledgeUnit).where(KnowledgeUnit.revision_id == revision.id)
        )
    ).all()
    svc.event(
        db,
        study.id,
        "source_imported",
        str(_),
        revision_id=str(revision.id),
        title=body.title,
        locator=body.locator,
        extraction="pending_review",
    )
    await db.commit()
    return {
        "revision": store.revision_out(revision),
        "units": [store.unit_out(u, revision) for u in units],
        "extraction": "pending_review",
        "note": "Source imported; no claims have been verified.",
    }


@router.post("/studies/{study_id}/nodes", status_code=201)
async def add_node(study_id: uuid.UUID, body: NodeIn, _: User, db: Db):
    result = await svc.add_node(db, await svc.get_study(db, study_id), body, str(_))
    await db.commit()
    return result


@router.post("/studies/{study_id}/relations", status_code=201)
async def relation(study_id: uuid.UUID, body: RelationIn, _: User, db: Db):
    study = await svc.get_study(db, study_id)
    await svc.get_node(db, study, body.from_id)
    await svc.get_node(db, study, body.to_id)
    row = Relation(study_id=study.id, **body.model_dump())
    db.add(row)
    await db.flush()
    svc.event(db, study.id, "relation_proposed", str(_), relation=svc.out(row))
    await db.commit()
    return svc.out(row)


@router.post("/studies/{study_id}/select")
async def select_methods(study_id: uuid.UUID, body: SelectionIn, _: User, db: Db):
    study = await svc.get_study(db, study_id)
    # Search all same-scope studies; failed experiences remain available across research projects.
    q = (
        select(Node)
        .join(Study)
        .where(Study.scope == study.scope, Node.kind == "method")
    )
    if body.query:
        q = q.where(
            func.concat(Node.title, " ", Node.summary).ilike(
                "%" + body.query.replace("%", "\\%").replace("_", "\\_") + "%"
            )
        )
    nodes = (await db.scalars(q.order_by(Node.created_at.desc()).limit(200))).all()
    methods = [
        {
            **svc.out(n),
            "cost_seconds": n.details.get("cost_seconds"),
            "evidence_count": int(n.evidence_id is not None),
        }
        for n in nodes
    ]
    ranked = rank_methods(
        methods, body.environment or study.environment, body.budget_seconds
    )
    svc.event(
        db,
        study.id,
        "selection_recorded",
        str(_),
        request=body.model_dump(),
        candidates=[
            {"id": n["id"], "eligibility": n["eligibility"], "reason": n["reason"]}
            for n in ranked
        ],
    )
    await db.commit()
    return {
        "candidates": ranked,
        "limit": 200,
        "ranking": "deterministic investigation priority; no predicted effect",
        "combination_policy": "Individual effects do not establish combined effects; propose a separate hypothesis and experiment.",
    }


@router.post("/studies/{study_id}/experiments", status_code=201)
async def experiment(study_id: uuid.UUID, body: ExperimentIn, _: User, db: Db):
    study = await svc.get_study(db, study_id)
    for ident in body.method_ids:
        await svc.get_node(db, study, ident)
    row = Experiment(study_id=study.id, **body.model_dump(mode="json"))
    db.add(row)
    await db.flush()
    svc.event(
        db,
        study.id,
        "experiment_planned",
        str(_),
        id=str(row.id),
        title=row.title,
        rationale=row.rationale,
    )
    await db.commit()
    return svc.out(row)


async def get_experiment(db, ident):
    row = (
        await db.scalars(
            select(Experiment).where(Experiment.id == ident).with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise HTTPException(404, "research_experiment_not_found")
    return row


@router.post("/experiments/{experiment_id}/execute")
async def execute(experiment_id: uuid.UUID, _: User, db: Db):
    # Nonblocking transaction advisory lock serializes bounded CPU adapters across generations.
    if not await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(hashtextextended('research-cpu',0))")
    ):
        raise HTTPException(409, "research_executor_busy")
    row = await get_experiment(db, experiment_id)
    if row.state != "queued":
        raise HTTPException(409, "research_experiment_not_queued")
    if row.adapter != "retrieval_eval":
        raise HTTPException(422, "adapter_not_locally_executable_use_external_report")
    started = utcnow()
    row.state = "running"
    svc.event(db, row.study_id, "execution_started", str(_), id=str(row.id))
    try:
        result = await asyncio.to_thread(
            benchmark.evaluate, row.config, row.budget["seconds"]
        )
        row.state = "completed"
        row.receipt = {
            "origin": "server_executed",
            "adapter_version": "retrieval-eval-v1",
            "started_at": started.isoformat(),
            "finished_at": utcnow().isoformat(),
            "input_sha256": store.digest(
                json.dumps(row.config, sort_keys=True, ensure_ascii=False)
            ),
            "runtime": {
                "python": platform.python_version(),
                "platform": platform.platform(),
            },
            "result": result,
        }
        observation = Node(
            study_id=row.study_id,
            kind="observation",
            title=row.title,
            summary="CPU retrieval evaluation measured",
            conditions=row.environment,
            details={"experiment_id": str(row.id), "result": result},
            epistemic_status="locally_measured",
        )
        db.add(observation)
    except (ValueError, TimeoutError) as exc:
        row.state = "failed"
        row.receipt = {
            "origin": "server_executed",
            "error": str(exc),
            "started_at": started.isoformat(),
            "result": None,
        }
    row.finished_at = utcnow()
    svc.event(
        db, row.study_id, "execution_finished", str(_), id=str(row.id), state=row.state
    )
    await db.commit()
    return svc.out(row)


@router.post("/experiments/{experiment_id}/report")
async def report(experiment_id: uuid.UUID, body: ReportIn, _: User, db: Db):
    row = await get_experiment(db, experiment_id)
    if row.state not in ("queued", "blocked", "paused"):
        raise HTTPException(409, "research_receipt_already_recorded")
    row.state = "needs_verification"
    row.receipt = {
        "origin": "external_report",
        **body.model_dump(),
        "reported_at": utcnow().isoformat(),
    }
    svc.event(
        db,
        row.study_id,
        "external_report_added",
        str(_),
        id=str(row.id),
        next_actions=body.next_actions,
    )
    await db.commit()
    return svc.out(row)


class ControlIn(Strict):
    action: Literal["pause", "resume", "cancel", "block"]
    reason: str = Field(min_length=1, max_length=2000)


@router.post("/experiments/{experiment_id}/control")
async def control(experiment_id: uuid.UUID, body: ControlIn, _: User, db: Db):
    row = await get_experiment(db, experiment_id)
    transitions = {
        "pause": ("queued", "paused"),
        "resume": ("paused", "queued"),
        "cancel": ("queued", "cancelled"),
        "block": ("queued", "blocked"),
    }
    before, after = transitions[body.action]
    if row.state != before:
        raise HTTPException(409, "invalid_execution_transition")
    row.state = after
    svc.event(
        db,
        row.study_id,
        "experiment_control",
        str(_),
        id=str(row.id),
        **body.model_dump(),
    )
    await db.commit()
    return svc.out(row)


@router.get("/studies/{study_id}/compare")
async def compare(study_id: uuid.UUID, a: uuid.UUID, b: uuid.UUID, _: User, db: Db):
    await svc.get_study(db, study_id)
    left, right = await db.get(Experiment, a), await db.get(Experiment, b)
    if not left or not right or left.study_id != study_id or right.study_id != study_id:
        raise HTTPException(404, "research_experiment_not_found")
    return {
        **comparable(left.environment, right.environment),
        "a": svc.out(left),
        "b": svc.out(right),
    }


@router.get("/discover")
async def discover(_: User, q: str = Query(min_length=1, max_length=240)):
    from ttakkari.research.discovery import search_arxiv

    try:
        return await search_arxiv(q)
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            502, "research_discovery_failed_no_results_inferred"
        ) from exc


class AgentIn(Strict):
    operation: Literal[
        "list",
        "create",
        "resume",
        "update",
        "source",
        "node",
        "relation",
        "select",
        "experiment",
        "execute",
        "report",
        "control",
        "discover",
    ]
    study_id: uuid.UUID | None = None
    experiment_id: uuid.UUID | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identifiers(self):
        if (
            self.operation in ("execute", "report", "control")
            and not self.experiment_id
        ):
            raise ValueError("experiment_id_required")
        if (
            self.operation
            not in ("list", "create", "discover", "execute", "report", "control")
            and not self.study_id
        ):
            raise ValueError("study_id_required")
        return self


def validate(schema, value):
    try:
        return schema.model_validate(value)
    except ValidationError as exc:
        raise HTTPException(
            422,
            {
                "code": "research_invalid_fields",
                "errors": exc.errors(include_input=False, include_context=False),
            },
        ) from exc


@agent_router.post("")
async def agent_manage(body: AgentIn, sid: AgentSession, db: Db):
    session = await _session(db, sid)
    if session.archived_at is not None:
        raise HTTPException(403, "archived_session")
    scope, actor = ("work" if session.pool == "work" else "personal"), f"agent:{sid}"
    op = body.operation
    if op == "list":
        return await studies(actor, db, scope, 50, 0)
    if op == "create":
        if body.data.get("scope", scope) != scope:
            raise HTTPException(403, "research_scope_mismatch")
        return await create_study(
            validate(StudyIn, {**body.data, "scope": scope}), actor, db
        )
    if op == "discover":
        return await discover(actor, str(body.data.get("q", ""))[:240])
    if op in ("execute", "report", "control"):
        exp = await get_experiment(db, body.experiment_id)
        await svc.get_study(db, exp.study_id, scope)
        if op == "execute":
            return await execute(exp.id, actor, db)
        return await (
            report(exp.id, validate(ReportIn, body.data), actor, db)
            if op == "report"
            else control(exp.id, validate(ControlIn, body.data), actor, db)
        )
    study = await svc.get_study(db, body.study_id, scope)
    if op == "resume":
        return await resume(study.id, actor, db)
    operations = {
        "update": (patch_study, StudyPatch),
        "source": (add_source, SourceIn),
        "node": (add_node, NodeIn),
        "relation": (relation, RelationIn),
        "select": (select_methods, SelectionIn),
        "experiment": (experiment, ExperimentIn),
    }
    fn, schema = operations[op]
    return await fn(study.id, validate(schema, body.data), actor, db)
