"""Typed research aggregates plus extensible, evidence-bound knowledge nodes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class Study(Base):
    __tablename__ = "research_studies"
    __table_args__ = (
        CheckConstraint("scope in ('personal','work')", name="ck_research_scope"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(240))
    goal: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(20), index=True)
    phase: Mapped[str] = mapped_column(String(100), default="planning")
    environment: Mapped[dict[str, Any]] = mapped_column(default=dict)
    criteria: Mapped[list[Any]] = mapped_column(default=list)
    next_actions: Mapped[list[Any]] = mapped_column(default=list)
    version: Mapped[int] = mapped_column(Integer, default=1)
    primary_target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    best_result_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Node(Base):
    __tablename__ = "research_nodes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_studies.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(100), index=True)
    title: Mapped[str] = mapped_column(String(240))
    summary: Mapped[str] = mapped_column(Text)
    conditions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    details: Mapped[dict[str, Any]] = mapped_column(default=dict)
    epistemic_status: Mapped[str] = mapped_column(String(40))
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("kb_evidence.id"), index=True
    )
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_nodes.id")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Relation(Base):
    __tablename__ = "research_relations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_studies.id"), index=True
    )
    from_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_nodes.id"), index=True
    )
    to_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_nodes.id"), index=True
    )
    predicate: Mapped[str] = mapped_column(String(100))
    rationale: Mapped[str] = mapped_column(Text)
    conditions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class TaxonomyTerm(Base):
    __tablename__ = "research_taxonomy_terms"
    __table_args__ = (
        UniqueConstraint("axis", "code", name="uq_taxonomy_axis_code"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    axis: Mapped[str] = mapped_column(String(32), index=True)
    code: Mapped[str] = mapped_column(String(64), index=True)
    label: Mapped[str] = mapped_column(String(120))
    label_ko: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    parent_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_system: Mapped[bool] = mapped_column(Boolean, default=True)


class Method(Base):
    __tablename__ = "research_methods"
    __table_args__ = (
        UniqueConstraint("scope", "slug", "version", name="uq_method_scope_slug_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16), default="system", index=True)
    slug: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_methods.id"), nullable=True
    )
    origin: Mapped[str] = mapped_column(String(32), default="system_seed")
    lifecycle_status: Mapped[str] = mapped_column(String(24), default="validated", index=True)
    name: Mapped[str] = mapped_column(String(200))
    name_ko: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text)
    principle: Mapped[str] = mapped_column(Text, default="")
    category: Mapped[str] = mapped_column(String(64), default="general", index=True)
    targets: Mapped[list[str]] = mapped_column(JSONB, default=list)
    layers: Mapped[list[str]] = mapped_column(JSONB, default=list)
    goals: Mapped[list[str]] = mapped_column(JSONB, default=list)
    interventions: Mapped[list[str]] = mapped_column(JSONB, default=list)
    research_methods: Mapped[list[str]] = mapped_column(JSONB, default=list)
    parameters: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    compatibility: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    metrics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    expected_effects: Mapped[list[str]] = mapped_column(JSONB, default=list)
    side_effects: Mapped[list[str]] = mapped_column(JSONB, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    evidence_status: Mapped[str] = mapped_column(String(24), default="source_reported")
    adapter_hints: Mapped[list[str]] = mapped_column(JSONB, default=list)
    tags: Mapped[list[str]] = mapped_column(JSONB, default=list)
    source_study_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_studies.id"), nullable=True
    )
    source_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_nodes.id"), nullable=True
    )
    promoted_from_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MethodRelation(Base):
    __tablename__ = "research_method_relations"
    __table_args__ = (
        UniqueConstraint("from_method_id", "to_method_id", "relation_type", name="uq_method_relation"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    scope: Mapped[str] = mapped_column(String(16), default="system", index=True)
    from_method_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_methods.id", ondelete="CASCADE"), index=True
    )
    to_method_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_methods.id", ondelete="CASCADE"), index=True
    )
    relation_type: Mapped[str] = mapped_column(String(32), index=True)
    conditions: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")


class Experiment(Base):
    __tablename__ = "research_experiments"
    __table_args__ = (
        CheckConstraint(
            "state in ('queued','running','paused','blocked','completed','failed','cancelled','needs_verification')",
            name="ck_research_execution_state",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_studies.id"), index=True
    )
    title: Mapped[str] = mapped_column(String(240))
    hypothesis: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(30), default="queued", index=True)
    config: Mapped[dict[str, Any]] = mapped_column(default=dict)
    environment: Mapped[dict[str, Any]] = mapped_column(default=dict)
    budget: Mapped[dict[str, Any]] = mapped_column(default=dict)
    receipt: Mapped[dict[str, Any]] = mapped_column(default=dict)
    method_ids: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]

    # V2 확장 컬럼
    method_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_methods.id"), nullable=True, index=True
    )
    compared_method_ids: Mapped[list[Any]] = mapped_column(JSONB, default=list)
    adapter_id: Mapped[str] = mapped_column(String(64), default="retrieval_eval")
    strategy: Mapped[str] = mapped_column(String(32), default="single")
    search_space: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    fixed_inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    primary_metric: Mapped[str] = mapped_column(String(64), default="recall_at_k")
    secondary_metrics: Mapped[list[str]] = mapped_column(JSONB, default=list)
    baseline_configuration_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)


class Configuration(Base):
    __tablename__ = "research_configurations"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_experiments.id", ondelete="CASCADE"), index=True
    )
    label: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(24), default="candidate")
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    config_hash: Mapped[str] = mapped_column(String(64), index=True)
    ablation_factor: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Run(Base):
    __tablename__ = "research_runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_experiments.id", ondelete="CASCADE"), index=True
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_configurations.id", ondelete="CASCADE"), index=True
    )
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_studies.id", ondelete="CASCADE"), index=True
    )
    adapter_id: Mapped[str] = mapped_column(String(64))
    execution_origin: Mapped[str] = mapped_column(String(24), default="local_measured", index=True)
    repeat_index: Mapped[int] = mapped_column(Integer, default=1)
    seed: Mapped[int] = mapped_column(Integer, default=42)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    progress: Mapped[float | None] = mapped_column(Float, nullable=True)
    environment_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    raw_output: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    wall_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Measurement(Base):
    __tablename__ = "research_measurements"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_experiments.id", ondelete="CASCADE"), index=True
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_configurations.id", ondelete="CASCADE"), index=True
    )
    metric_key: Mapped[str] = mapped_column(String(64), index=True)
    metric_label: Mapped[str] = mapped_column(String(120))
    value: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(32), default="")
    direction: Mapped[str] = mapped_column(String(24), default="higher_is_better")
    is_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    aggregator: Mapped[str] = mapped_column(String(24), default="single")
    slice_key: Mapped[str] = mapped_column(String(64), default="overall")
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Analysis(Base):
    __tablename__ = "research_analyses"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_experiments.id", ondelete="CASCADE"), index=True
    )
    baseline_configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_configurations.id"), nullable=True
    )
    best_configuration_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_configurations.id"), nullable=True
    )
    primary_metric: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    recommendation_ko: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ResearchArtifact(Base):
    __tablename__ = "research_artifacts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_studies.id", ondelete="CASCADE"), index=True
    )
    experiment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_experiments.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    artifact_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("artifacts.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(200))
    promoted_to_library: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class BestResult(Base):
    __tablename__ = "research_best_results"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_studies.id", ondelete="CASCADE"), index=True
    )
    experiment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_experiments.id", ondelete="CASCADE"), index=True
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_configurations.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_runs.id", ondelete="CASCADE"), index=True
    )
    decision_node_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_nodes.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(String(24), default="adopted")
    objective: Mapped[str] = mapped_column(Text, default="")
    primary_metric: Mapped[str] = mapped_column(String(64), default="")
    constraints: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    rationale: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Event(Base):
    __tablename__ = "research_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    study_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_studies.id"), index=True
    )
    kind: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


IMMUTABLE = """
CREATE OR REPLACE FUNCTION research_immutable_row() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'research history is immutable; append a correction' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql
"""
CONSISTENCY = """
CREATE OR REPLACE FUNCTION research_validate_relation() RETURNS trigger AS $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM research_nodes WHERE id=NEW.from_id AND study_id=NEW.study_id)
    OR NOT EXISTS (SELECT 1 FROM research_nodes WHERE id=NEW.to_id AND study_id=NEW.study_id) THEN
    RAISE EXCEPTION 'research relation study mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
NODE_CONSISTENCY = """
CREATE OR REPLACE FUNCTION research_validate_node() RETURNS trigger AS $$
BEGIN
  IF NEW.kind IN ('claim','finding') AND NEW.evidence_id IS NULL THEN
    RAISE EXCEPTION 'source evidence required' USING ERRCODE='23514';
  END IF;
  IF NEW.evidence_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM kb_evidence e JOIN kb_passages u ON u.id=e.passage_id
    JOIN kb_revisions r ON r.id=u.revision_id JOIN kb_sources source ON source.id=r.source_id
    JOIN research_studies s ON s.id=NEW.study_id
    WHERE e.id=NEW.evidence_id AND source.origin_meta->>'research_scope'=s.scope) THEN
    RAISE EXCEPTION 'research evidence scope mismatch' USING ERRCODE='23514';
  END IF;
  IF NEW.supersedes_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM research_nodes WHERE id=NEW.supersedes_id AND study_id=NEW.study_id) THEN
    RAISE EXCEPTION 'research correction study mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
for _cls in (Node, Relation, Event, Measurement):
    event.listen(_cls.__table__, "after_create", DDL(IMMUTABLE))
    event.listen(
        _cls.__table__,
        "after_create",
        DDL(
            f"CREATE TRIGGER {_cls.__tablename__}_immutable BEFORE UPDATE OR DELETE ON {_cls.__tablename__} "
            "FOR EACH ROW EXECUTE FUNCTION research_immutable_row()"
        ),
    )
event.listen(Relation.__table__, "after_create", DDL(CONSISTENCY))
event.listen(
    Relation.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER research_relation_validate BEFORE INSERT ON research_relations "
        "FOR EACH ROW EXECUTE FUNCTION research_validate_relation()"
    ),
)
event.listen(Node.__table__, "after_create", DDL(NODE_CONSISTENCY))
event.listen(
    Node.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER research_node_validate BEFORE INSERT ON research_nodes "
        "FOR EACH ROW EXECUTE FUNCTION research_validate_node()"
    ),
)
