"""Collector Platform ORM models: collector_jobs and collector_outbox."""
from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class CollectorJob(Base):
    __tablename__ = "collector_jobs"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "scope",
            "source_key",
            "request_key",
            name="uq_collector_jobs_idempotency",
        ),
        CheckConstraint(
            "scope IN ('personal', 'work', 'shared')",
            name="ck_collector_jobs_scope",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled', 'retry_wait', 'budget_wait', 'policy_blocked', 'quarantined')",
            name="ck_collector_jobs_status",
        ),
        Index("ix_collector_jobs_status", "status"),
        Index("ix_collector_jobs_tenant_scope", "tenant_id", "scope"),
        Index("ix_collector_jobs_claim", "status", "not_before", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_key: Mapped[str] = mapped_column(String(256), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    envelope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    stage: Mapped[str] = mapped_column(String(32), nullable=False, default="intake")
    envelope: Mapped[dict[str, Any]] = mapped_column(nullable=False)
    owner_generation: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    not_before: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # S2-02(0060): 한 page 의 결과와 같은 트랜잭션으로 전진하는 재개 지점, 취소 요청, 마지막 오류, 소유 세대 heartbeat.
    cursor: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    heartbeat_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CollectorOutbox(Base):
    __tablename__ = "collector_outbox"
    __table_args__ = (
        # tenant 를 키에 넣어 다른 tenant 의 같은 envelope_id 가 서로를 막지 않게 한다.
        UniqueConstraint(
            "tenant_id", "sink", "scope", "envelope_id", name="uq_collector_outbox_tenant_sink_envelope"
        ),
        CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'delivery_unknown')",
            name="ck_collector_outbox_status",
        ),
        CheckConstraint(
            "scope IN ('personal', 'work', 'shared')",
            name="ck_collector_outbox_scope",
        ),
        Index("ix_collector_outbox_status", "status"),
        Index("ix_collector_outbox_job_id", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collector_jobs.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    sink: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    envelope_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    delivered_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
