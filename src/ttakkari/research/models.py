"""Typed research aggregates plus extensible, evidence-bound knowledge nodes."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DDL, CheckConstraint, ForeignKey, Integer, String, Text, event
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
        ForeignKey("knowledge_evidence.id"), index=True
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
    SELECT 1 FROM knowledge_evidence e JOIN knowledge_units u ON u.id=e.unit_id
    JOIN knowledge_revisions r ON r.id=u.revision_id JOIN research_studies s ON s.id=NEW.study_id
    WHERE e.id=NEW.evidence_id AND r.scope=s.scope) THEN
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
for _cls in (Node, Relation, Event):
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
