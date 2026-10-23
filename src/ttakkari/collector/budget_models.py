"""Absolute spend ledger, scoped result cache and runtime flags for the collector (CP-S2-05, CP-S2-10)."""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow


class CollectorBudgetBucket(Base):
    """One row per (day|job) bucket. The row is the lock: reservations serialize on it, day first then job."""
    __tablename__ = "collector_budget_buckets"
    __table_args__ = (CheckConstraint("kind IN ('day', 'job')", name="ck_collector_budget_buckets_kind"),
                      CheckConstraint("reserved_tokens >= 0 AND reserved_calls >= 0 AND reserved_micro_usd >= 0",
                                      name="ck_collector_budget_buckets_nonneg"))
    kind: Mapped[str] = mapped_column(String(8), primary_key=True)
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    limit_tokens: Mapped[int] = mapped_column(BigInteger)
    limit_calls: Mapped[int] = mapped_column(Integer)
    limit_micro_usd: Mapped[int] = mapped_column(BigInteger)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    reserved_calls: Mapped[int] = mapped_column(Integer, default=0)
    reserved_micro_usd: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectorBudgetReservation(Base):
    """Worst-case cost reserved and committed before any provider call. `unknown` keeps the full amount occupied."""
    __tablename__ = "collector_budget_reservations"
    __table_args__ = (
        UniqueConstraint("request_key", name="uq_collector_budget_reservations_key"),
        CheckConstraint("state IN ('reserved', 'settled', 'unknown')", name="ck_collector_budget_reservations_state"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    request_key: Mapped[str] = mapped_column(String(200))
    day: Mapped[str] = mapped_column(String(10), index=True)
    job_id: Mapped[str | None] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(24))
    engine: Mapped[str] = mapped_column(String(80))
    state: Mapped[str] = mapped_column(String(12), default="reserved")
    reserved_tokens: Mapped[int] = mapped_column(BigInteger)
    reserved_micro_usd: Mapped[int] = mapped_column(BigInteger)
    charged_tokens: Mapped[int] = mapped_column(BigInteger)
    charged_micro_usd: Mapped[int] = mapped_column(BigInteger)
    receipt: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CollectorCacheEntry(Base):
    """Result cache. The key already includes tenant/scope/text/model/prompt/policy/query, so a hit never crosses them."""
    __tablename__ = "collector_cache"
    __table_args__ = (CheckConstraint("kind IN ('positive', 'negative', 'unknown')", name="ck_collector_cache_kind"),)
    cache_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(10))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(String(200))
    hits: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CollectorRuntimeFlag(Base):
    """Operator switches that must change without a restart or a settings edit."""
    __tablename__ = "collector_runtime_flags"
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
