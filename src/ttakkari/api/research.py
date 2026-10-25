"""Research Lab user and session-scoped agent API. No arbitrary command runner."""

from __future__ import annotations

import asyncio
import hashlib
import json
import platform
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from ttakkari.api.agent import _session
from ttakkari.api.deps import Db
from ttakkari.models import utcnow
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.research import benchmark
from ttakkari.research import catalog_loader
from ttakkari.research import service as svc
from ttakkari.research import planner, runner, analyzer
from ttakkari.research.models import (
    Analysis,
    BestResult,
    Configuration,
    Experiment,
    Measurement,
    Method,
    MethodRelation,
    Node,
    Relation,
    ResearchArtifact,
    Run,
    Study,
    TaxonomyTerm,
)
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
        serialized.encode("utf-8")
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
    source_key: str = Field(min_length=1, max_length=460)
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
    seconds: int = Field(default=30, ge=1, le=86400)
    memory_mb: int = Field(default=256, ge=64, le=65536)


class ExperimentIn(Strict):
    title: str = Field(default="", max_length=240)
    name: str | None = None
    hypothesis: str = Field(default="", max_length=6000)
    rationale: str = Field(default="", max_length=6000)
    adapter: str = Field(default="retrieval_eval", max_length=100)
    adapter_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    environment: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] | Budget = Field(default_factory=dict)
    method_ids: list[uuid.UUID] = Field(default_factory=list, max_length=30)
    method_id: uuid.UUID | None = None
    compared_method_ids: list[uuid.UUID] = Field(default_factory=list)
    strategy: str = Field(default="single")
    search_space: dict[str, Any] = Field(default_factory=dict)
    fixed_inputs: dict[str, Any] = Field(default_factory=dict)
    primary_metric: str = Field(default="recall_at_k")
    secondary_metrics: list[str] = Field(default_factory=list)
    seed: int = 42
    auto_start: bool = False

    @model_validator(mode="after")
    def resolve_aliases(self):
        if not self.title and self.name:
            self.title = self.name
        if not self.title:
            self.title = "Untitled Experiment"
        if not self.adapter_id and self.adapter:
            self.adapter_id = self.adapter
        return self


class TaxonomyTermIn(Strict):
    axis: str = Field(min_length=1, max_length=32)
    code: str = Field(min_length=1, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    label_ko: str = Field(min_length=1, max_length=120)
    description: str = Field(default="")
    parent_code: str | None = None


class MethodIn(Strict):
    slug: str = Field(min_length=1, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    name_ko: str | None = Field(default=None, max_length=200)
    summary: str = Field(default="")
    principle: str = Field(default="")
    category: str = Field(default="general", max_length=64)
    targets: list[str] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)
    goals: list[str] = Field(default_factory=list)
    interventions: list[str] = Field(default_factory=list)
    research_methods: list[str] = Field(default_factory=list)
    parameters: list[dict[str, Any]] = Field(default_factory=list)
    compatibility: dict[str, Any] = Field(default_factory=dict)
    metrics: list[str] = Field(default_factory=list)
    expected_effects: list[str] = Field(default_factory=list)
    side_effects: list[str] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    evidence_status: str = Field(default="unverified", max_length=24)
    adapter_hints: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    source_study_id: uuid.UUID | None = None
    source_node_id: uuid.UUID | None = None
    lifecycle_status: str = Field(default="hypothesis", max_length=24)

    @model_validator(mode="after")
    def populate_defaults(self):
        if not self.name_ko and self.name:
            self.name_ko = self.name
        return self


class MethodPatch(Strict):
    name: str | None = None
    name_ko: str | None = None
    summary: str | None = None
    principle: str | None = None
    category: str | None = None
    targets: list[str] | None = None
    layers: list[str] | None = None
    goals: list[str] | None = None
    interventions: list[str] | None = None
    research_methods: list[str] | None = None
    parameters: list[dict[str, Any]] | None = None
    compatibility: dict[str, Any] | None = None
    metrics: list[str] | None = None
    expected_effects: list[str] | None = None
    side_effects: list[str] | None = None
    evidence: list[dict[str, Any]] | None = None
    adapter_hints: list[str] | None = None
    tags: list[str] | None = None
    lifecycle_status: str | None = None
    create_new_version: bool = False


class MethodPromoteIn(Strict):
    run_id: uuid.UUID
    decision_note: str = Field(min_length=1, max_length=6000)
    target_status: Literal["candidate", "validated"] = "validated"


class MethodRelationIn(Strict):
    to_method_id: uuid.UUID
    relation_type: str = Field(min_length=1, max_length=32)
    conditions: dict[str, Any] = Field(default_factory=dict)
    note: str = Field(default="", max_length=2000)


class ExperimentValidateIn(Strict):
    study_id: uuid.UUID
    method_id: uuid.UUID | None = None
    compared_method_ids: list[uuid.UUID] = Field(default_factory=list)
    adapter_id: str = Field(default="retrieval_eval")
    strategy: str = Field(default="single")
    search_space: dict[str, Any] = Field(default_factory=dict)
    fixed_inputs: dict[str, Any] = Field(default_factory=dict)
    primary_metric: str = Field(default="recall_at_k")
    budget: dict[str, Any] = Field(default_factory=dict)
    seed: int = 42


class ExperimentRunIn(Strict):
    configuration_ids: list[uuid.UUID] | None = None


class RunCancelIn(Strict):
    reason: str = Field(default="User cancelled run", max_length=500)


class ExternalReportIn(Strict):
    configuration_id: uuid.UUID | None = None
    provenance: Literal["external_benchmark", "manual_evaluation", "imported_log"] = Field(
        default="external_benchmark"
    )
    environment_snapshot: dict[str, Any] = Field(default_factory=dict)
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    raw_output: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class AnalyzeIn(Strict):
    baseline_configuration_id: uuid.UUID | None = None
    pareto_x_metric: str | None = None
    pareto_y_metric: str | None = None


class BestResultIn(Strict):
    experiment_id: uuid.UUID
    configuration_id: uuid.UUID
    run_id: uuid.UUID
    objective: str = Field(min_length=1, max_length=2000)
    primary_metric: str = Field(min_length=1, max_length=64)
    constraints: dict[str, Any] = Field(default_factory=dict)
    rationale: str = Field(min_length=1, max_length=6000)
    promote_method: bool = False
    status: Literal["adopted", "rejected"] = "adopted"


class ExportReportIn(Strict):
    experiment_ids: list[uuid.UUID] | None = None
    promote_to_library: bool = False


ExternalReportIn.model_rebuild()


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
    result = await svc.import_source(db, study, body, str(_))
    await db.commit()
    return result


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
        method = (
            await db.scalars(
                select(Node)
                .join(Study)
                .where(
                    Node.id == ident, Study.scope == study.scope, Node.kind == "method"
                )
            )
        ).one_or_none()
        if method is None:
            raise HTTPException(404, "research_method_not_found_in_scope")

    adapter_id = body.adapter_id or body.adapter or "retrieval_eval"
    raw_budget = (
        body.budget.model_dump()
        if hasattr(body.budget, "model_dump")
        else dict(body.budget)
    )
    budget_dict = {
        "seconds": 30,
        "memory_mb": 256,
        "max_runs": 20,
        "max_total_seconds": 300,
        **raw_budget,
    }

    serialized_method_ids = [str(m) for m in body.method_ids]
    serialized_compared_ids = [str(m) for m in body.compared_method_ids]

    row = Experiment(
        study_id=study.id,
        title=body.title,
        hypothesis=body.hypothesis,
        rationale=body.rationale,
        adapter=adapter_id,
        adapter_id=adapter_id,
        state="queued",
        config=body.config or {},
        environment=body.environment or {},
        budget=budget_dict,
        receipt={},
        method_ids=serialized_method_ids,
        method_id=body.method_id,
        compared_method_ids=serialized_compared_ids,
        strategy=body.strategy,
        search_space=body.search_space,
        fixed_inputs=body.fixed_inputs,
        primary_metric=body.primary_metric,
        secondary_metrics=body.secondary_metrics,
        created_at=utcnow(),
    )
    db.add(row)
    await db.flush()

    # Expand and create configurations
    expanded_configs, exp_errors, exp_warnings = planner.expand_configurations(
        strategy=body.strategy,
        search_space=body.search_space or ({"default": [body.config]} if body.config else {}),
        fixed_inputs=body.fixed_inputs or body.config or {},
        max_runs=budget_dict.get("max_runs", 20),
        seed=body.seed,
    )
    if not expanded_configs:
        cfg_params = body.config or body.fixed_inputs or {}
        expanded_configs = [
            {
                "label": "Baseline",
                "role": "baseline",
                "order_index": 0,
                "parameters": cfg_params,
                "config_hash": planner.canonical_json_hash(cfg_params),
                "ablation_factor": None,
            }
        ]

    created_configs = []
    baseline_cfg_id = None
    for c_data in expanded_configs:
        c_id = uuid.uuid4()
        if c_data.get("role") == "baseline" and baseline_cfg_id is None:
            baseline_cfg_id = c_id
        cfg_row = Configuration(
            id=c_id,
            experiment_id=row.id,
            label=c_data["label"],
            role=c_data.get("role", "candidate"),
            order_index=c_data.get("order_index", 0),
            parameters=c_data["parameters"],
            config_hash=c_data["config_hash"],
            ablation_factor=c_data.get("ablation_factor"),
            created_at=utcnow(),
        )
        db.add(cfg_row)
        created_configs.append(cfg_row)

    if baseline_cfg_id:
        row.baseline_configuration_id = baseline_cfg_id
    elif created_configs:
        row.baseline_configuration_id = created_configs[0].id

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

    created_runs = []
    if body.auto_start:
        created_runs = await runner.start_experiment_runs(
            db, experiment_id=row.id, background=True
        )

    await db.commit()

    # Flat experiment fields stay at the top level for older callers; `experiment` is the contract's wrapper.
    out_dict = svc.experiment_out(row)
    out_dict["experiment"] = svc.experiment_out(row)
    out_dict["configurations"] = [svc.out(c) for c in created_configs]
    out_dict["runs"] = [svc.run_out(r) for r in created_runs]
    return out_dict


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
            "input_sha256": hashlib.sha256(
                json.dumps(row.config, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest(),
            "adapter_sha256": benchmark.code_digest(),
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
async def compare(
    study_id: uuid.UUID,
    a: uuid.UUID,
    b: uuid.UUID,
    _: User,
    db: Db,
    vary: Annotated[list[str] | None, Query(max_length=20)] = None,
):
    await svc.get_study(db, study_id)
    left, right = await db.get(Experiment, a), await db.get(Experiment, b)
    if not left or not right or left.study_id != study_id or right.study_id != study_id:
        raise HTTPException(404, "research_experiment_not_found")
    return {
        **comparable(left.environment, right.environment, vary=vary or []),
        "a": svc.out(left),
        "b": svc.out(right),
    }


# ==========================================
# Research Lab v2 Endpoints
# ==========================================


async def ensure_catalog(db) -> None:
    """Lazy seed load for read endpoints. It must commit: the session is discarded without commit, which would
    roll the load back and hand every later request freshly generated method ids."""
    try:
        stats = await catalog_loader.ensure_catalog_loaded(db)
        if stats["methods"] or stats["terms"]:
            await db.commit()
    except IntegrityError:
        # A concurrent first request loaded the same seeds; read what it committed.
        await db.rollback()


@router.post("/catalog/sync")
async def sync_catalog(_: User, db: Db, force: bool = Query(False)):
    """Synchronize catalog seed JSON files idempotently."""
    stats = await catalog_loader.ensure_catalog_loaded(db, force=force)
    await db.commit()
    return {"status": "synced", **stats}


@router.get("/dashboard")
async def dashboard(
    _: User,
    db: Db,
    study_id: uuid.UUID | None = Query(None),
    scope: Literal["personal", "work"] = Query("personal"),
):
    """Aggregate dashboard view: counts, readiness, recent runs, best results, and templates."""
    await ensure_catalog(db)
    return await svc.get_study_dashboard(db, study_id=study_id, scope=scope)


@router.get("/taxonomy")
async def get_taxonomy(_: User, db: Db, axis: str | None = Query(None)):
    """Retrieve taxonomy dictionary grouped by axis."""
    await ensure_catalog(db)
    q = select(TaxonomyTerm).order_by(TaxonomyTerm.axis, TaxonomyTerm.sort_order)
    if axis:
        q = q.where(TaxonomyTerm.axis == axis)
    terms = (await db.scalars(q)).all()

    axes: dict[str, list[dict[str, Any]]] = {}
    for t in terms:
        axes.setdefault(t.axis, []).append(svc.out(t))
    return {"axes": axes}


@router.post("/taxonomy", status_code=201)
async def create_taxonomy_term(body: TaxonomyTermIn, _: User, db: Db):
    """Register or update a taxonomy term."""
    existing = (
        await db.execute(
            select(TaxonomyTerm).where(
                TaxonomyTerm.axis == body.axis, TaxonomyTerm.code == body.code
            )
        )
    ).scalar_one_or_none()

    if existing:
        existing.label = body.label
        existing.label_ko = body.label_ko
        existing.description = body.description
        existing.parent_code = body.parent_code
        await db.commit()
        return svc.out(existing)

    new_term = TaxonomyTerm(
        id=uuid.uuid4(),
        axis=body.axis,
        code=body.code,
        label=body.label,
        label_ko=body.label_ko,
        description=body.description,
        parent_code=body.parent_code,
        sort_order=0,
        is_system=False,
    )
    db.add(new_term)
    await db.commit()
    return svc.out(new_term)


@router.get("/methods")
async def list_methods(
    _: User,
    db: Db,
    q: str | None = Query(None),
    target: list[str] | None = Query(None),
    layer: list[str] | None = Query(None),
    goal: list[str] | None = Query(None),
    intervention: list[str] | None = Query(None),
    research_method: list[str] | None = Query(None),
    execution_status: list[str] | None = Query(None),
    lifecycle_status: list[str] | None = Query(None),
    origin: list[str] | None = Query(None),
    scope: Literal["personal", "work"] = Query("personal"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Search and filter methods across 6 dimensions with facet counts.

    A filter takes several values (`target=llm,tts` or `target=llm&target=tts`): OR inside an axis, AND across axes.
    """
    await ensure_catalog(db)
    return await svc.list_methods(
        db,
        scope=scope,
        q=q,
        target=target,
        layer=layer,
        goal=goal,
        intervention=intervention,
        research_method=research_method,
        execution_status=execution_status,
        lifecycle_status=lifecycle_status,
        origin=origin,
        limit=limit,
        offset=offset,
    )


@router.get("/methods/{id_or_slug}")
async def get_method(
    id_or_slug: str,
    _: User,
    db: Db,
    scope: Literal["personal", "work"] = Query("personal"),
):
    """Retrieve full method definition, relations, related experiments, and historical local measurements."""
    await ensure_catalog(db)
    return await svc.get_method_details(db, ident_or_slug=id_or_slug, scope=scope)


@router.post("/methods", status_code=201)
async def create_method(
    body: MethodIn,
    _: User,
    db: Db,
    scope: Literal["personal", "work"] = Query("personal"),
):
    """Register a new user method or hypothesis."""
    method = await svc.create_method(db, data=body.model_dump(), scope=scope)
    await db.commit()
    return svc.out(method)


@router.patch("/methods/{method_id}")
async def patch_method(
    method_id: uuid.UUID,
    body: MethodPatch,
    _: User,
    db: Db,
    scope: Literal["personal", "work"] = Query("personal"),
):
    """Update method in-place or fork into a new version when modifying system seeds."""
    method = await svc.update_method(
        db,
        method_id=method_id,
        patch_data=body.model_dump(exclude={"create_new_version"}, exclude_none=True),
        create_new_version=body.create_new_version,
        scope=scope,
    )
    await db.commit()
    return svc.out(method)


@router.post("/methods/{method_id}/promote")
async def promote_method(
    method_id: uuid.UUID,
    body: MethodPromoteIn,
    _: User,
    db: Db,
):
    """Promote hypothesis method to candidate or validated status upon succeeded run."""
    method = await svc.promote_method(
        db,
        method_id=method_id,
        run_id=body.run_id,
        decision_note=body.decision_note,
        target_status=body.target_status,
        actor=str(_),
    )
    await db.commit()
    return svc.out(method)


@router.post("/methods/{method_id}/relations", status_code=201)
async def create_relation(
    method_id: uuid.UUID,
    body: MethodRelationIn,
    _: User,
    db: Db,
    scope: Literal["personal", "work"] = Query("personal"),
):
    """Establish a relation (combines_with, conflicts_with, requires, etc.) between methods."""
    rel = await svc.create_method_relation(
        db,
        from_method_id=method_id,
        to_method_id=body.to_method_id,
        relation_type=body.relation_type,
        conditions=body.conditions,
        note=body.note,
        scope=scope,
    )
    await db.commit()
    return svc.out(rel)


@router.post("/experiments/validate")
async def validate_experiment_plan(body: ExperimentValidateIn, _: User, db: Db):
    """Preview expanded configurations and validate budget, adapter constraints, and combination relations."""
    res = await planner.validate_experiment_plan(
        db,
        study_id=body.study_id,
        method_id=body.method_id,
        compared_method_ids=body.compared_method_ids,
        adapter_id=body.adapter_id,
        strategy=body.strategy,
        search_space=body.search_space,
        fixed_inputs=body.fixed_inputs,
        budget=body.budget,
        seed=body.seed,
    )
    import dataclasses
    return dataclasses.asdict(res)


@router.get("/experiments/{experiment_id}")
async def get_experiment_details(experiment_id: uuid.UUID, _: User, db: Db):
    """Retrieve experiment definition, configurations, runs, measurements, and latest analysis."""
    exp = await db.get(Experiment, experiment_id)
    if not exp:
        raise HTTPException(404, "research_experiment_not_found")

    configs = (
        await db.execute(
            select(Configuration)
            .where(Configuration.experiment_id == experiment_id)
            .order_by(Configuration.order_index)
        )
    ).scalars().all()

    runs = (
        await db.execute(
            select(Run)
            .where(Run.experiment_id == experiment_id)
            .order_by(Run.created_at)
        )
    ).scalars().all()

    latest_analysis = (
        await db.execute(
            select(Analysis)
            .where(Analysis.experiment_id == experiment_id)
            .order_by(Analysis.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    artifacts = (
        await db.execute(
            select(ResearchArtifact).where(
                ResearchArtifact.experiment_id == experiment_id
            )
        )
    ).scalars().all()

    measurements = (
        await db.execute(
            select(Measurement)
            .where(Measurement.experiment_id == experiment_id)
            .order_by(Measurement.created_at, Measurement.id)
        )
    ).scalars().all()
    by_run: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for m in measurements:
        by_run.setdefault(m.run_id, []).append(svc.out(m))

    # Flat experiment fields stay at the top level for older callers; `experiment` is the contract's wrapper.
    out_dict = svc.experiment_out(exp)
    out_dict["experiment"] = svc.experiment_out(exp)
    out_dict["configurations"] = [svc.out(c) for c in configs]
    out_dict["runs"] = [{**svc.run_out(r), "measurements": by_run.get(r.id, [])} for r in runs]
    out_dict["measurements"] = [m for r in runs for m in by_run.get(r.id, [])]
    out_dict["latest_analysis"] = svc.out(latest_analysis) if latest_analysis else None
    out_dict["artifacts"] = [svc.out(a) for a in artifacts]
    return out_dict


@router.post("/experiments/{experiment_id}/run")
async def run_experiment(
    experiment_id: uuid.UUID,
    _: User,
    db: Db,
    body: ExperimentRunIn | None = None,
):
    """Start or enqueue run executions for configurations in an experiment."""
    runs = await runner.start_experiment_runs(
        db,
        experiment_id=experiment_id,
        configuration_ids=body.configuration_ids if body else None,
        background=True,
    )
    await db.commit()
    return {"runs": [svc.run_out(r) for r in runs]}


@router.get("/runs")
async def list_runs(
    _: User,
    db: Db,
    study_id: uuid.UUID | None = Query(None),
    experiment_id: uuid.UUID | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """List execution runs with optional study/experiment/status filters."""
    q = (
        select(Run, Experiment.title, Configuration.label, Study.title)
        .join(Experiment, Experiment.id == Run.experiment_id)
        .join(Configuration, Configuration.id == Run.configuration_id)
        .join(Study, Study.id == Run.study_id)
    )
    if study_id:
        q = q.where(Run.study_id == study_id)
    if experiment_id:
        q = q.where(Run.experiment_id == experiment_id)
    if status:
        q = q.where(Run.status == status)

    rows = (
        await db.execute(
            q.order_by(Run.created_at.desc()).limit(limit).offset(offset)
        )
    ).all()

    # Locks held right now: one entry per running run, named after what that adapter needs exclusively.
    lock_names = {
        "llm_inference": "host_gpu_lease",
        "tts_synthesis": "tts_lock",
        "retrieval_eval": "cpu_lock",
    }
    running = (
        await db.execute(select(Run).where(Run.status == "running"))
    ).scalars().all()
    active_locks = [
        {
            "lock": lock_names[r.adapter_id],
            "adapter_id": r.adapter_id,
            "run_id": str(r.id),
            "experiment_id": str(r.experiment_id),
        }
        for r in running
        if r.adapter_id in lock_names
    ]
    gpu_probe, gpu_lease = await svc.adapter_readiness("personal", only={"llm_inference"})
    llm = gpu_probe.get("llm_inference")
    host_gpu_status = (
        {
            "ready": llm["ready"],
            "reason": llm["reason"],
            "available_models": llm["available_models"],
            "lease_held": bool(gpu_lease and gpu_lease.get("held")),
        }
        if llm
        else None
    )

    return {
        "runs": [
            {
                **svc.run_out(r),
                "experiment_name": exp_title,
                "configuration_label": cfg_label,
                "study_title": study_title,
            }
            for r, exp_title, cfg_label, study_title in rows
        ],
        "host_gpu_status": host_gpu_status,
        "active_locks": active_locks,
    }


@router.get("/runs/{run_id}")
async def get_run_details(run_id: uuid.UUID, _: User, db: Db):
    """Get run details including raw output, environment snapshot, measurements, and artifacts."""
    run = await db.get(Run, run_id)
    if not run:
        raise HTTPException(404, "research_run_not_found")

    measurements = (
        await db.execute(select(Measurement).where(Measurement.run_id == run_id))
    ).scalars().all()

    artifacts = (
        await db.execute(
            select(ResearchArtifact).where(ResearchArtifact.run_id == run_id)
        )
    ).scalars().all()

    run_dict = svc.run_out(run)
    if isinstance(run.raw_output, dict) and "provenance" in run.raw_output:
        run_dict["provenance"] = run.raw_output["provenance"]
    run_dict["measurements"] = [svc.out(m) for m in measurements]
    run_dict["artifacts"] = [svc.out(a) for a in artifacts]
    return run_dict


@router.post("/runs/{run_id}/cancel")
async def cancel_run_endpoint(run_id: uuid.UUID, body: RunCancelIn, _: User, db: Db):
    """Cancel a running or queued run."""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "research_run_not_found")
    cancelled = await runner.cancel_run(run_id, reason=body.reason)
    # cancel_run commits on its own session; reload so `run` shows the stored state.
    await db.refresh(run)
    return {
        "cancelled": cancelled,
        "run_id": str(run_id),
        "status": run.status,
        "run": svc.run_out(run),
    }


@router.post("/experiments/{experiment_id}/external-report", status_code=201)
async def record_external_report(
    experiment_id: uuid.UUID,
    body: ExternalReportIn,
    _: User,
    db: Db,
):
    """Import external benchmark or manual evaluation metrics under a configuration."""
    exp = await db.get(Experiment, experiment_id)
    if not exp:
        raise HTTPException(404, "research_experiment_not_found")

    cfg_id = body.configuration_id or exp.baseline_configuration_id
    if not cfg_id:
        # Create default configuration if none exists
        cfg = Configuration(
            id=uuid.uuid4(),
            experiment_id=exp.id,
            label="External Report Config",
            role="candidate",
            order_index=0,
            parameters={},
            config_hash=planner.canonical_json_hash({}),
            created_at=utcnow(),
        )
        db.add(cfg)
        await db.flush()
        cfg_id = cfg.id

    raw_output = dict(body.raw_output)
    raw_output["provenance"] = body.provenance

    run = Run(
        id=uuid.uuid4(),
        experiment_id=exp.id,
        configuration_id=cfg_id,
        study_id=exp.study_id,
        adapter_id="external_report",
        execution_origin="external_report",
        repeat_index=1,
        seed=42,
        status="succeeded",
        progress=1.0,
        environment_snapshot=body.environment_snapshot,
        raw_output=raw_output,
        started_at=body.started_at or utcnow(),
        finished_at=body.finished_at or utcnow(),
        created_at=utcnow(),
    )
    db.add(run)
    await db.flush()

    measurement_rows = []
    for m in body.metrics:
        m_meta = dict(m.get("meta") or {})
        m_meta["provenance"] = body.provenance
        m_row = Measurement(
            id=uuid.uuid4(),
            run_id=run.id,
            experiment_id=exp.id,
            configuration_id=cfg_id,
            metric_key=m.get("metric_key", "score"),
            metric_label=m.get("metric_label", m.get("metric_key", "score")),
            value=float(m.get("value", 0.0)),
            unit=m.get("unit", ""),
            direction=m.get("direction", "higher_is_better"),
            is_proxy=m.get("is_proxy", False),
            aggregator=m.get("aggregator", "single"),
            slice_key=m.get("slice_key", "overall"),
            meta=m_meta,
            created_at=utcnow(),
        )
        db.add(m_row)
        measurement_rows.append(m_row)

    await db.commit()
    res = svc.run_out(run)
    if isinstance(run.raw_output, dict) and "provenance" in run.raw_output:
        res["provenance"] = run.raw_output["provenance"]
    res["measurements"] = [svc.out(m_row) for m_row in measurement_rows]
    return res


@router.post("/experiments/{experiment_id}/analyze")
async def analyze_experiment_endpoint(
    experiment_id: uuid.UUID,
    body: AnalyzeIn,
    _: User,
    db: Db,
):
    """Run full comparative analysis, generate deterministic Korean recommendation, and upsert analysis snapshot."""
    analysis = await analyzer.analyze_experiment(
        db,
        experiment_id=experiment_id,
        baseline_configuration_id=body.baseline_configuration_id,
        pareto_x_metric=body.pareto_x_metric,
        pareto_y_metric=body.pareto_y_metric,
    )
    await db.commit()
    return svc.out(analysis)


@router.get("/experiments/{experiment_id}/compare-series")
async def compare_series_endpoint(
    experiment_id: uuid.UUID,
    _: User,
    db: Db,
    baseline_config_id: uuid.UUID | None = Query(None),
    primary_metric: str | None = Query(None),
    secondary_metric: str | None = Query(None),
    pareto_x_metric: str | None = Query(None),
    pareto_y_metric: str | None = Query(None),
    metrics: str | None = Query(None),
):
    """Get chart-ready normalized series JSON for frontend SVG charts.

    `metrics` (comma separated) narrows the per-metric series; without it every measured metric is returned.
    """
    metrics_filter = [m.strip() for m in metrics.split(",") if m.strip()] if metrics else None
    return await analyzer.build_compare_series(
        db,
        experiment_id=experiment_id,
        baseline_config_id=baseline_config_id,
        primary_metric=primary_metric or pareto_y_metric,
        secondary_metric=secondary_metric,
        metrics_filter=metrics_filter,
        pareto_x_metric=pareto_x_metric,
        pareto_y_metric=pareto_y_metric,
    )


@router.post("/studies/{study_id}/best-results", status_code=201)
async def adopt_best_result_endpoint(
    study_id: uuid.UUID,
    body: BestResultIn,
    _: User,
    db: Db,
):
    """Adopt an optimal configuration run for a study, recording goal, metric, and constraints."""
    best_res = await svc.adopt_best_result(
        db,
        study_id=study_id,
        experiment_id=body.experiment_id,
        configuration_id=body.configuration_id,
        run_id=body.run_id,
        objective=body.objective,
        primary_metric=body.primary_metric,
        constraints=body.constraints,
        rationale=body.rationale,
        promote_method_flag=body.promote_method,
        status=body.status,
        actor=str(_),
    )
    await db.commit()
    out_dict = svc.out(best_res)
    node = await db.get(Node, best_res.decision_node_id)
    out_dict["decision_node"] = svc.out(node) if node else None
    # Same condition under which svc.adopt_best_result promoted the method.
    exp = await db.get(Experiment, body.experiment_id)
    promoted = body.promote_method and body.status == "adopted" and exp.method_id
    method = await db.get(Method, exp.method_id) if promoted else None
    out_dict["method"] = svc.out(method) if method else None
    return out_dict


@router.post("/studies/{study_id}/export-report")
async def export_study_report_endpoint(
    study_id: uuid.UUID,
    body: ExportReportIn,
    _: User,
    db: Db,
):
    """Generate Markdown report for study, register as artifact, and link."""
    result = await svc.export_study_report(
        db,
        study_id=study_id,
        experiment_ids=body.experiment_ids,
        promote_to_library=body.promote_to_library,
        actor=str(_),
    )
    await db.commit()
    return result


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
        # V2 operations
        "dashboard",
        "catalog_search",
        "method_get",
        "method_register",
        "method_promote",
        "experiment_validate",
        "experiment_create",
        "run_status",
        "run_cancel",
        "compare_series",
        "adopt_best_result",
        "export_report",
    ]
    study_id: uuid.UUID | None = None
    experiment_id: uuid.UUID | None = None
    run_id: uuid.UUID | None = None
    method_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def identifiers(self):
        v2_global_ops = (
            "list", "create", "discover", "dashboard", "catalog_search",
            "method_get", "method_register", "method_promote",
            "experiment_validate", "run_status", "run_cancel"
        )
        if (
            self.operation in ("execute", "report", "control", "compare_series")
            and not self.experiment_id
        ):
            raise ValueError("experiment_id_required")
        if (
            self.operation not in v2_global_ops
            and self.operation not in ("execute", "report", "control", "compare_series")
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

    # V2 Global operations
    if op == "dashboard":
        return await dashboard(actor, db, study_id=body.study_id, scope=scope)
    if op == "catalog_search":
        d = body.data
        return await list_methods(
            actor, db,
            q=d.get("q"), target=d.get("target"), layer=d.get("layer"),
            goal=d.get("goal"), intervention=d.get("intervention"),
            research_method=d.get("research_method"),
            execution_status=d.get("execution_status"),
            lifecycle_status=d.get("lifecycle_status"),
            origin=d.get("origin"),
            scope=scope,
            limit=d.get("limit", 50),
            offset=d.get("offset", 0),
        )
    if op == "method_get":
        target_slug = body.method_id or body.data.get("slug") or body.data.get("id")
        if not target_slug:
            raise HTTPException(422, "method_slug_or_id_required")
        return await get_method(str(target_slug), actor, db, scope=scope)
    if op == "method_register":
        return await create_method(validate(MethodIn, body.data), actor, db, scope=scope)
    if op == "method_promote":
        target_mid = uuid.UUID(str(body.method_id or body.data["method_id"]))
        return await promote_method(target_mid, validate(MethodPromoteIn, body.data), actor, db)
    if op == "experiment_validate":
        v_data = dict(body.data)
        if body.study_id:
            v_data["study_id"] = body.study_id
        return await validate_experiment_plan(validate(ExperimentValidateIn, v_data), actor, db)
    if op == "run_status":
        rid = body.run_id or (uuid.UUID(str(body.data["run_id"])) if "run_id" in body.data else None)
        if not rid:
            raise HTTPException(422, "run_id_required")
        return await get_run_details(rid, actor, db)
    if op == "run_cancel":
        rid = body.run_id or (uuid.UUID(str(body.data["run_id"])) if "run_id" in body.data else None)
        if not rid:
            raise HTTPException(422, "run_id_required")
        return await cancel_run_endpoint(rid, validate(RunCancelIn, body.data), actor, db)

    # Operations requiring experiment_id
    if op in ("execute", "report", "control", "compare_series"):
        exp = await get_experiment(db, body.experiment_id)
        await svc.get_study(db, exp.study_id, scope)
        if op == "execute":
            return await execute(exp.id, actor, db)
        if op == "compare_series":
            return await compare_series_endpoint(
                exp.id, actor, db,
                baseline_config_id=(
                    uuid.UUID(str(body.data["baseline_config_id"]))
                    if body.data.get("baseline_config_id")
                    else None
                ),
                primary_metric=body.data.get("primary_metric"),
                secondary_metric=None,
                pareto_x_metric=body.data.get("pareto_x_metric"),
                pareto_y_metric=body.data.get("pareto_y_metric"),
                metrics=body.data.get("metrics"),
            )
        return await (
            report(exp.id, validate(ReportIn, body.data), actor, db)
            if op == "report"
            else control(exp.id, validate(ControlIn, body.data), actor, db)
        )

    study = await svc.get_study(db, body.study_id, scope)
    if op == "resume":
        return await resume(study.id, actor, db)
    if op == "experiment_create":
        return await experiment(study.id, validate(ExperimentIn, body.data), actor, db)
    if op == "adopt_best_result":
        return await adopt_best_result_endpoint(study.id, validate(BestResultIn, body.data), actor, db)
    if op == "export_report":
        return await export_study_report_endpoint(study.id, validate(ExportReportIn, body.data), actor, db)

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
