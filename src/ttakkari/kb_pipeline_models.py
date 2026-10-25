"""지식 도서관(KB v3) 무인 자동 지식화 파이프라인 모델.

자료 등록 및 수집 배달 트랜잭션 안에서 큐잉되어, 백그라운드 워커가 6단계
(parsing -> chunking -> extracting -> linking -> indexing -> canonicalizing)
를 실행하고 상태 전이, 임차권(lease), 재시도, 펜싱(attempt_token)을 관리한다.
불변 원장(kb_documents, kb_revisions)을 보존하기 위해 원장 외래키는 RESTRICT 로 결속된다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class KbPipelineJob(Base):
    """문서 리비전별 6단계 지식화 작업 상태 머신 및 임차/펜싱 제어."""

    __tablename__ = "kb_pipeline_jobs"
    __table_args__ = (
        UniqueConstraint("revision_id", name="uq_kb_pipeline_job_revision"),
        CheckConstraint(
            "status IN ('registered', 'parsing', 'chunking', 'extracting', 'linking', "
            "'indexing', 'canonicalizing', 'ready', 'degraded', 'held', 'failed')",
            name="ck_kb_pipeline_job_status",
        ),
        CheckConstraint(
            "current_stage IN ('parsing', 'chunking', 'extracting', 'linking', 'indexing', 'canonicalizing')",
            name="ck_kb_pipeline_job_stage",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_kb_pipeline_job_attempts"),
        CheckConstraint("max_attempts > 0", name="ck_kb_pipeline_job_max_attempts"),
        Index(
            "ix_kb_pipeline_jobs_claim",
            "status",
            "next_run_at",
        ),
        Index("ix_kb_pipeline_jobs_document_id", "document_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="RESTRICT"), nullable=False
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_revisions.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(24), default="registered")
    current_stage: Mapped[str] = mapped_column(String(24), default="parsing")
    pipeline_version: Mapped[str] = mapped_column(String(32), default="kb-v3.0")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(default=None)
    claimed_by: Mapped[str | None] = mapped_column(String(64), default=None)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    hold_reason: Mapped[str | None] = mapped_column(String(64), default=None)
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    error_detail: Mapped[str | None] = mapped_column(Text, default=None)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )


class KbPipelineStage(Base):
    """파이프라인 작업 내 개별 단계 실행 이력 및 메트릭."""

    __tablename__ = "kb_pipeline_stages"
    __table_args__ = (
        UniqueConstraint("job_id", "stage", name="uq_kb_pipeline_stage"),
        CheckConstraint(
            "stage IN ('parsing', 'chunking', 'extracting', 'linking', 'indexing', 'canonicalizing')",
            name="ck_kb_pipeline_stage_name",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'degraded', 'held', 'failed', 'skipped')",
            name="ck_kb_pipeline_stage_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_kb_pipeline_stage_attempts"),
        Index("ix_kb_pipeline_stages_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_pipeline_jobs.id", ondelete="CASCADE"), nullable=False
    )
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    processor_version: Mapped[str] = mapped_column(String(32), default="v1")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, default=None)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, default=None)


class KbL2BudgetReservation(Base):
    """Cluster-wide daily call allocation with a lease and idempotent owner key."""

    __tablename__ = "kb_l2_budget_reservations"
    __table_args__ = (
        UniqueConstraint("reservation_key", name="uq_kb_l2_budget_reservation_key"),
        CheckConstraint("reserved_calls > 0", name="ck_kb_l2_budget_reserved_positive"),
        CheckConstraint(
            "status IN ('active', 'completed', 'cancelled', 'expired')",
            name="ck_kb_l2_budget_reservation_status",
        ),
        Index("ix_kb_l2_budget_reservations_day_status", "budget_date", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    budget_date: Mapped[Any] = mapped_column(Date, nullable=False)
    reservation_key: Mapped[str] = mapped_column(String(160), nullable=False)
    reserved_calls: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_call_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KbL2BudgetUsage(Base):
    """Per-call ledger; uncertain in-flight calls remain charged after cancellation/crash."""

    __tablename__ = "kb_l2_budget_usage"
    __table_args__ = (
        UniqueConstraint("reservation_id", "call_no", name="uq_kb_l2_budget_usage_call"),
        CheckConstraint(
            "status IN ('in_flight', 'consumed', 'released')",
            name="ck_kb_l2_budget_usage_status",
        ),
        Index("ix_kb_l2_budget_usage_reservation", "reservation_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    reservation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("kb_l2_budget_reservations.id", ondelete="RESTRICT"), nullable=False
    )
    call_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="in_flight")
    error_code: Mapped[str | None] = mapped_column(String(64), default=None)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
