"""Versioned knowledge ledger, separate from disposable retrieval projections."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class KnowledgeRevision(Base):
    __tablename__ = "knowledge_revisions"
    __table_args__ = (
        UniqueConstraint(
            "source_key", "sha256", "parser_version", name="uq_knowledge_revision_input"
        ),
        CheckConstraint(
            "scope in ('personal','work','shared')", name="ck_knowledge_revision_scope"
        ),
        Index(
            "uq_knowledge_revision_active",
            "source_key",
            unique=True,
            postgresql_where=text("active"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_key: Mapped[str] = mapped_column(String(500), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(60))
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ctx_sources.id", ondelete="SET NULL"), index=True
    )
    origin: Mapped[str] = mapped_column(String(40), default="text")
    scope: Mapped[str] = mapped_column(String(20))
    sensitivity: Mapped[str] = mapped_column(String(20), default="normal")
    locator: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KnowledgeUnit(Base):
    __tablename__ = "knowledge_units"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "external_key", name="uq_knowledge_unit_occurrence"
        ),
        CheckConstraint(
            "outcome in ('pending_extraction','no_message','unsupported_content','invalid')",
            name="ck_knowledge_unit_outcome",
        ),
        CheckConstraint("source_offset >= 0", name="ck_knowledge_unit_offset"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_revisions.id", ondelete="CASCADE"), index=True
    )
    external_key: Mapped[str] = mapped_column(String(500))
    stable_key: Mapped[str] = mapped_column(String(64), index=True)
    locator: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    source_offset: Mapped[int] = mapped_column(Integer, default=0)
    speaker: Mapped[str] = mapped_column(String(30), default="document")
    active_branch: Mapped[bool | None] = mapped_column(Boolean)
    event_time: Mapped[datetime | None]
    outcome: Mapped[str] = mapped_column(String(30), default="pending_extraction")
    reason: Mapped[str | None] = mapped_column(String(100))
    provenance: Mapped[dict[str, Any]] = mapped_column(
        default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KnowledgeEvidence(Base):
    __tablename__ = "knowledge_evidence"
    __table_args__ = (
        UniqueConstraint("unit_id", "start", "end", name="uq_knowledge_evidence_span"),
        CheckConstraint(
            'start >= 0 AND "end" > start', name="ck_knowledge_evidence_span"
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    unit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_units.id", ondelete="CASCADE"), index=True
    )
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KnowledgeAssertion(Base):
    __tablename__ = "knowledge_assertions"
    __table_args__ = (
        UniqueConstraint("fingerprint", name="uq_knowledge_assertion_fingerprint"),
        CheckConstraint(
            "scope in ('personal','work','shared')", name="ck_knowledge_assertion_scope"
        ),
        CheckConstraint(
            "state in ('candidate','active','disputed','superseded','retracted')",
            name="ck_knowledge_assertion_state",
        ),
        CheckConstraint(
            "review_level in ('candidate','source_reviewed','user_confirmed')",
            name="ck_knowledge_assertion_review",
        ),
        CheckConstraint(
            "modality in ('fact','proposal','question','inference','decision','preference')",
            name="ck_knowledge_assertion_modality",
        ),
        CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="ck_knowledge_assertion_time",
        ),
        CheckConstraint("version > 0", name="ck_knowledge_assertion_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    fingerprint: Mapped[str] = mapped_column(String(64))
    subject: Mapped[str] = mapped_column(String(200), index=True)
    predicate: Mapped[str] = mapped_column(String(100), index=True)
    object: Mapped[str] = mapped_column(Text)
    subject_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ctx_entities.id", ondelete="SET NULL"), index=True
    )
    object_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ctx_entities.id", ondelete="SET NULL"), index=True
    )
    scope: Mapped[str] = mapped_column(String(20))
    modality: Mapped[str] = mapped_column(String(20), default="fact")
    polarity: Mapped[bool] = mapped_column(Boolean, default=True)
    state: Mapped[str] = mapped_column(String(20), default="candidate", index=True)
    review_level: Mapped[str] = mapped_column(String(20), default="candidate")
    version: Mapped[int] = mapped_column(Integer, default=1)
    valid_from: Mapped[datetime | None]
    valid_to: Mapped[datetime | None]
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("knowledge_assertions.id"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class KnowledgeAssertionEvidence(Base):
    __tablename__ = "knowledge_assertion_evidence"
    __table_args__ = (
        CheckConstraint(
            "stance in ('supports','refutes')", name="ck_knowledge_evidence_stance"
        ),
    )
    assertion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_assertions.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_evidence.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    stance: Mapped[str] = mapped_column(String(10), default="supports")


class KnowledgeReviewEvent(Base):
    __tablename__ = "knowledge_review_events"
    __table_args__ = (
        UniqueConstraint("assertion_id", "version", name="uq_knowledge_review_version"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    assertion_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_assertions.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    actor: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(20))
    review_level: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class KnowledgeExtractionJob(Base):
    __tablename__ = "knowledge_extraction_jobs"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "extractor_version", name="uq_knowledge_extraction_input"
        ),
        CheckConstraint(
            "status in ('queued','running','succeeded','partial','failed','cancelled')",
            name="ck_knowledge_extraction_status",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("knowledge_revisions.id", ondelete="CASCADE"), index=True
    )
    extractor_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL")
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(default=dict)
    counts: Mapped[dict[str, Any]] = mapped_column(default=dict)
    error: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


# The immutable ledger cannot be rewritten through another application or SQL writer.
# Keep this DDL synchronized with the static Alembic migration; the current projection
# (revision.active / assertion state) remains mutable through explicit transactions.
from sqlalchemy import DDL, event

IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION knowledge_immutable_row() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'knowledge ledger rows are immutable' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql
"""
SPAN_FUNCTION = """
CREATE OR REPLACE FUNCTION knowledge_validate_span() RETURNS trigger AS $$
DECLARE original text;
BEGIN
  SELECT body INTO original FROM knowledge_units WHERE id=NEW.unit_id;
  IF original IS NULL OR NEW.start < 0 OR NEW.\"end\" <= NEW.start
      OR NEW.\"end\" > char_length(original)
      OR encode(sha256(convert_to(substring(original FROM NEW.start+1 FOR NEW.\"end\"-NEW.start), 'UTF8')), 'hex') <> NEW.sha256 THEN
    RAISE EXCEPTION 'knowledge evidence span or digest mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""
for _table in (
    KnowledgeUnit.__table__,
    KnowledgeEvidence.__table__,
    KnowledgeReviewEvent.__table__,
):
    event.listen(_table, "after_create", DDL(IMMUTABLE_FUNCTION))
    event.listen(
        _table,
        "after_create",
        DDL(
            f"CREATE TRIGGER {_table.name}_immutable BEFORE UPDATE ON {_table.name} "
            "FOR EACH ROW EXECUTE FUNCTION knowledge_immutable_row()"
        ),
    )
event.listen(KnowledgeEvidence.__table__, "after_create", DDL(SPAN_FUNCTION))
event.listen(
    KnowledgeEvidence.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER knowledge_evidence_validate BEFORE INSERT ON knowledge_evidence "
        "FOR EACH ROW EXECUTE FUNCTION knowledge_validate_span()"
    ),
)
