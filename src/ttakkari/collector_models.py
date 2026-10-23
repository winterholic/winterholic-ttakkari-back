"""Collector Platform ORM models: collector_jobs and collector_outbox."""
from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
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
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    not_before: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)


class CollectorSourcePolicy(Base):
    """Append-only, server-registered rights/usage policy of a source. The highest version is the active one."""

    __tablename__ = "collector_source_policies"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope", "source_key", "version", name="uq_collector_source_policies_version"),
        CheckConstraint("scope IN ('personal', 'work', 'shared')", name="ck_collector_source_policies_scope"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(256), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    spec_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CollectorHostState(Base):
    """Public, tenant-independent per-origin state: robots.txt cache, durable cooldown and request pacing."""

    __tablename__ = "collector_host_state"

    origin: Mapped[str] = mapped_column(String(300), primary_key=True)
    robots_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    robots_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    robots_fetched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    robots_expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_allowed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class CollectorFetchCache(Base):
    """Validators of the last successful fetch of a URL, for conditional requests (ETag / Last-Modified)."""

    __tablename__ = "collector_fetch_cache"
    __table_args__ = (
        UniqueConstraint("tenant_id", "scope", "source_key", "url_key", name="uq_collector_fetch_cache_url"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(256), nullable=False)
    url_key: Mapped[str] = mapped_column(String(64), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    etag: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_modified: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_raw_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_item_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    last_fetched_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CollectorItem(Base):
    """One URL's journey through a job: raw bytes -> text -> decision. Occurrences are never merged."""

    __tablename__ = "collector_items"
    __table_args__ = (
        UniqueConstraint("job_id", "url_key", name="uq_collector_items_job_url"),
        CheckConstraint("scope IN ('personal', 'work', 'shared')", name="ck_collector_items_scope"),
        CheckConstraint("kind IN ('document', 'discovery')", name="ck_collector_items_kind"),
        CheckConstraint(
            "status IN ('accepted', 'held', 'blocked', 'failed', 'not_modified', 'discovered')",
            name="ck_collector_items_status",
        ),
        Index("ix_collector_items_text", "tenant_id", "scope", "text_sha256"),
        Index("ix_collector_items_url", "tenant_id", "scope", "source_key", "url_key"),
        Index("ix_collector_items_job", "job_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("collector_jobs.id", ondelete="CASCADE"), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    source_key: Mapped[str] = mapped_column(String(256), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="document")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(24), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    url_key: Mapped[str] = mapped_column(String(64), nullable=False)
    final_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discovered_from: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    raw_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    raw_handle: Mapped[str | None] = mapped_column(String(100), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    language: Mapped[str | None] = mapped_column(String(16), nullable=True)
    language_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    minhash: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    exact_of: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    cluster_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile: Mapped[str | None] = mapped_column(String(32), nullable=True)
    decision_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reason_codes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    signals: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    envelope_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    envelope: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    normalizer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fetched_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class CollectorDedupeBand(Base):
    """LSH band keys of an item's MinHash signature. Candidate lookup stays inside one tenant and scope."""

    __tablename__ = "collector_dedupe_bands"
    __table_args__ = (Index("ix_collector_dedupe_bands_lookup", "tenant_id", "scope", "band", "band_hash"),)

    item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collector_items.id", ondelete="CASCADE"), primary_key=True
    )
    band: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    scope: Mapped[str] = mapped_column(String(32), nullable=False)
    band_hash: Mapped[str] = mapped_column(String(16), nullable=False)
