from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID, uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "gateway_projects"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    name: Mapped[str] = mapped_column(String(240))
    description: Mapped[str] = mapped_column(Text, default="")
    latest_snapshot_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    sharing_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Snapshot(Base):
    __tablename__ = "gateway_snapshots"
    __table_args__ = (
        Index("ix_gateway_snapshots_project_published", "project_id", "published_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("gateway_projects.id"), index=True)
    source_refs: Mapped[list[str]] = mapped_column(JSONB)
    source_revision: Mapped[str] = mapped_column(String(255))
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(String(700))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class APIKey(Base):
    __tablename__ = "gateway_api_keys"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(String(160))
    credential_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KeyACL(Base):
    __tablename__ = "gateway_key_acls"

    key_id: Mapped[UUID] = mapped_column(
        ForeignKey("gateway_api_keys.id", ondelete="CASCADE"), primary_key=True
    )
    project_id: Mapped[UUID] = mapped_column(
        ForeignKey("gateway_projects.id", ondelete="CASCADE"), primary_key=True
    )
    scopes: Mapped[list[str]] = mapped_column(JSONB)


class Session(Base):
    __tablename__ = "gateway_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key_id: Mapped[UUID] = mapped_column(ForeignKey("gateway_api_keys.id"))
    credential_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    window_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEvent(Base):
    __tablename__ = "gateway_audit_events"
    __table_args__ = (Index("ix_gateway_audit_created", "created_at"),)
    # Public workers may INSERT audit events but must not SELECT them. Fetching
    # the server timestamp via implicit RETURNING would require SELECT too.
    __mapper_args__: ClassVar[dict[str, bool]] = {"eager_defaults": False}

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key_id: Mapped[UUID | None] = mapped_column(ForeignKey("gateway_api_keys.id"), nullable=True)
    session_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("gateway_sessions.id"), nullable=True
    )
    event: Mapped[str] = mapped_column(String(80))
    project_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    detail: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SiteControlState(Base):
    """One durable site-wide switch; row id 1 is seeded enabled for old installations."""

    __tablename__ = "gateway_site_control"

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SiteControlAudit(Base):
    __tablename__ = "gateway_site_control_audit"
    __table_args__ = (Index("ix_gateway_site_control_audit_created", "created_at"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    event: Mapped[str] = mapped_column(String(32))
    enabled: Mapped[bool] = mapped_column(Boolean)
    actor: Mapped[str] = mapped_column(String(32), default="control_token")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
