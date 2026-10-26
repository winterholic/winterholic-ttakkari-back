"""Durable collector controls and KB delivery ledger. Existing immutable KB rows stay intact."""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DDL,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class CollectorSource(Base):
    __tablename__ = "collector_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope", "source_key", name="uq_collector_sources_identity"),
        CheckConstraint("scope IN ('personal', 'work')", name="ck_collector_sources_scope"),
        CheckConstraint("interval_minutes IS NULL OR interval_minutes >= 15", name="ck_collector_sources_interval"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32))
    pool: Mapped[str] = mapped_column(String(16), default="personal", server_default="personal")
    source_key: Mapped[str] = mapped_column(String(256))
    title: Mapped[str] = mapped_column(String(500))
    request: Mapped[dict[str, Any]] = mapped_column(JSONB)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_import: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("collector_jobs.id", ondelete="SET NULL"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class KbDocumentAccess(Base):
    __tablename__ = "kb_document_access"
    __table_args__ = (CheckConstraint("scope IN ('personal', 'work')", name="ck_kb_document_access_scope"),)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="RESTRICT"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), default="owner")
    scope: Mapped[str] = mapped_column(String(32))
    # 이 문서를 읽을 수 있는 가장 좁은 엔진 범위. work 만 회사 계정 엔진 세션에 열린다(collector.library).
    pool: Mapped[str] = mapped_column(String(16), default="personal", server_default="personal")
    hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class KbDocumentAlias(Base):
    """단일 도서관으로 합칠 때 같은 출처의 중복 문서를 canonical 하나로 잇는다. 리비전·패시지는 옮기거나 지우지 않는다.

    relation `covered`: 중복 문서의 모든 본문 해시가 canonical 에 이미 있다. 목록·검색에서 숨기고 조회는 canonical 로 보낸다.
    relation `variant`: 같은 출처지만 본문이 다른 이력이 있다. 둘 다 보이고 서로를 같은 출처로 표시한다.
    """
    __tablename__ = "kb_document_aliases"
    __table_args__ = (
        CheckConstraint("relation IN ('covered', 'variant')", name="ck_kb_document_aliases_relation"),
        CheckConstraint("document_id <> canonical_document_id", name="ck_kb_document_aliases_distinct"),
    )
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="RESTRICT"), primary_key=True)
    canonical_document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="RESTRICT"), index=True)
    relation: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectionScopeLedger(Base):
    """scope 병합이 바꾼 행의 이전 값. 되돌림·감사용이며 병합은 이 기록 없이는 행을 바꾸지 않는다."""
    __tablename__ = "collection_scope_ledger"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(String(64), index=True)
    table_name: Mapped[str] = mapped_column(String(64))
    row_key: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(48))
    before: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KbObservation(Base):
    __tablename__ = "kb_observations"
    __table_args__ = (
        UniqueConstraint("envelope_id", name="uq_kb_observations_envelope"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_documents.id", ondelete="RESTRICT"), index=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("kb_revisions.id", ondelete="RESTRICT"))
    envelope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class CollectorDelivery(Base):
    __tablename__ = "collector_deliveries"
    outbox_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector_outbox.id", ondelete="RESTRICT"), primary_key=True)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector_items.id", ondelete="RESTRICT"), index=True)
    revision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("kb_revisions.id", ondelete="RESTRICT"))
    outcome: Mapped[str] = mapped_column(String(32))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectorDailyBudget(Base):
    """Reserved page admission, retained across crashes and failed/retried jobs. No LLM spending."""
    __tablename__ = "collector_daily_budgets"
    day: Mapped[str] = mapped_column(String(10), primary_key=True)
    reserved_pages: Mapped[int] = mapped_column(Integer, default=0)


class CollectorReview(Base):
    __tablename__ = "collector_reviews"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    item_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector_items.id", ondelete="RESTRICT"), index=True)
    decision: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(String(2000))
    actor: Mapped[str] = mapped_column(String(32), default="owner")
    text_sha256: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectorQualityModel(Base):
    __tablename__ = "collector_quality_models"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32))
    digest: Mapped[str] = mapped_column(String(64))
    artifact: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# Source observations, reviews and trained artifacts are evidence; corrections append new rows.
for _model in (KbObservation, CollectorReview, CollectorQualityModel):
    event.listen(_model.__table__, "after_create", DDL("""
        CREATE OR REPLACE FUNCTION collector_evidence_immutable() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'collector evidence is immutable' USING ERRCODE='23514'; END;
        $$ LANGUAGE plpgsql
    """))
    event.listen(_model.__table__, "after_create", DDL(
        f"CREATE TRIGGER {_model.__tablename__}_immutable BEFORE UPDATE OR DELETE ON {_model.__tablename__} "
        "FOR EACH ROW EXECUTE FUNCTION collector_evidence_immutable()"))
