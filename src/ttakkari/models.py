from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONB, list[Any]: JSONB, datetime: DateTime(timezone=True)}  # noqa: RUF012


class Engine(enum.StrEnum):
    claude = "claude"
    codex = "codex"


class RunStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    # 서버 재시작 등으로 프로세스를 잃은 경우. 실패와 구분해 사용자가 이어서 지시할 수 있게 한다.
    interrupted = "interrupted"


TERMINAL_RUN_STATUSES = {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled, RunStatus.interrupted}


class PreviewStatus(enum.StrEnum):
    not_required = "not_required"
    pending = "pending"
    processing = "processing"
    ready = "ready"
    failed = "failed"
    unavailable = "unavailable"


class ExportPolicy(enum.StrEnum):
    allow = "allow"
    deny = "deny"
    sensitive = "sensitive"


class Credential(Base):
    """단일 사용자 앱이라 계정 테이블 대신 비밀번호 한 행만 둔다."""

    __tablename__ = "credentials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    family_id: Mapped[uuid.UUID] = mapped_column(index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    replaced_by: Mapped[uuid.UUID | None]
    user_agent: Mapped[str | None] = mapped_column(String(512))
    ip: Mapped[str | None] = mapped_column(String(64))


class Workspace(Base):
    __tablename__ = "workspaces"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    is_git: Mapped[bool] = mapped_column(Boolean, default=False)
    default_engine: Mapped[str] = mapped_column(String(16), default=Engine.claude)
    # codex OS 샌드박스 모드. workspace-write 가 기본 보험이다.
    codex_sandbox: Mapped[str] = mapped_column(String(32), default="workspace-write")
    extra_writable_roots: Mapped[list[Any]] = mapped_column(default=list)
    export_allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    archived_at: Mapped[datetime | None]


class ChatSession(Base):
    __tablename__ = "sessions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    engine: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(100))
    effort: Mapped[str | None] = mapped_column(String(16))
    # 엔진 쪽 대화 ID(claude session_id / codex thread_id). 후속 지시를 resume 하는 데 쓴다.
    engine_session_id: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None]


class Run(Base):
    """사용자 지시 1회 = 에이전트 프로세스 1회."""

    __tablename__ = "runs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    prompt: Mapped[str] = mapped_column(Text)
    context_artifact_ids: Mapped[list[Any]] = mapped_column(default=list)
    engine: Mapped[str] = mapped_column(String(16))
    model: Mapped[str | None] = mapped_column(String(100))
    effort: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=RunStatus.queued, index=True)
    pid: Mapped[int | None]
    exit_code: Mapped[int | None]
    error: Mapped[str | None] = mapped_column(Text)
    result_text: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    usage: Mapped[dict[str, Any]] = mapped_column(default=dict)
    checkpoint_ref: Mapped[str | None] = mapped_column(String(100))
    outbox_path: Mapped[str | None] = mapped_column(Text)
    last_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_created", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"), index=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"), index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), index=True)
    filename: Mapped[str] = mapped_column(String(500))
    mime_type: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(30))
    # 서버 내부 전용. API 응답에는 rel_path 만 내보낸다.
    original_path: Mapped[str] = mapped_column(Text)
    rel_path: Mapped[str | None] = mapped_column(Text)
    storage_mode: Mapped[str] = mapped_column(String(10))
    stored_path: Mapped[str] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    preview_status: Mapped[str] = mapped_column(String(16), default=PreviewStatus.not_required)
    preview_path: Mapped[str | None] = mapped_column(Text)
    preview_mime: Mapped[str | None] = mapped_column(String(100))
    preview_error: Mapped[str | None] = mapped_column(Text)
    export_policy: Mapped[str] = mapped_column(String(16), default=ExportPolicy.allow)
    retention_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None]


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(16))  # user | agent | system
    action: Mapped[str] = mapped_column(String(60), index=True)
    outcome: Mapped[str] = mapped_column(String(16))  # ok | denied | error
    target_type: Mapped[str | None] = mapped_column(String(30))
    target_id: Mapped[str | None] = mapped_column(String(100))
    detail: Mapped[dict[str, Any]] = mapped_column(default=dict)
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(512))
