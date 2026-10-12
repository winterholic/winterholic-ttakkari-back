from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
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


class SessionKind(enum.StrEnum):
    chat = "chat"
    orchestrator = "orchestrator"
    worker = "worker"


class RunOrigin(enum.StrEnum):
    user = "user"
    # 작업 세션이 끝나 오케스트레이터에게 들어가는 결과 보고
    report = "report"
    # 오케스트레이터가 도구로 작업 세션에 보낸 지시
    orchestrator = "orchestrator"


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
    # 엔진 프로필 이름 → 그 프로필의 엔진 대화 ID. 프로필을 오가도 각자 이어간다(ADR 0006).
    engine_sessions: Mapped[dict[str, Any]] = mapped_column(default=dict)
    kind: Mapped[str] = mapped_column(String(16), default=SessionKind.chat, index=True)
    # 이 대화가 쓸 계정 묶음(personal | work). 작업 세션은 오케스트레이터 것을 물려받는다.
    pool: Mapped[str] = mapped_column(String(16), default="personal", index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None]


class EngineAccount(Base):
    """엔진 계정(프로필). priority 오름차순이 폴백 순서다(ADR 0006). config_dir 로 계정을 고른다."""

    __tablename__ = "engine_accounts"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(60), unique=True)
    engine: Mapped[str] = mapped_column(String(16))
    # claude 면 CLAUDE_CONFIG_DIR, codex 면 CODEX_HOME. 비우면 엔진 기본 위치(~/.claude, ~/.codex).
    config_dir: Mapped[str | None] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    # personal | work. 회사 업무 탭에서 만든 대화만 work 계정을 쓴다(순서·켜기도 따로).
    pool: Mapped[str] = mapped_column(String(16), default="personal", index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AppSetting(Base):
    """앱 단위 설정 한 줄(키-값). 예) 계정 임시 우선순위."""

    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(default=dict)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class TransferToken(Base):
    """아이폰 단축어처럼 앱 밖에서 파일을 올릴 때 쓰는 업로드 전용 토큰. 원문은 만들 때 한 번만 보여 주고 해시만 둔다."""

    __tablename__ = "transfer_tokens"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(60))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


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
    origin: Mapped[str] = mapped_column(String(16), default=RunOrigin.user)
    # 실제로 실행한 엔진 프로필(폴백하면 마지막 프로필)
    profile: Mapped[str | None] = mapped_column(String(40))
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


class PushSubscription(Base):
    """브라우저 Web Push 구독. endpoint 가 기기 식별자 역할을 하므로 unique 로 upsert 한다."""

    __tablename__ = "push_subscriptions"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    endpoint: Mapped[str] = mapped_column(String(2048), unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    last_success_at: Mapped[datetime | None]


# ---- 메모·캘린더 (ADR 0008)
class Memo(Base):
    """마크다운 메모. rev 는 낙관적 잠금(자동 저장·에이전트 수정이 서로 덮어쓰지 않게). deleted_at 은 에이전트의 보관 삭제."""

    __tablename__ = "memos"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[Any]] = mapped_column(default=list)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    rev: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)
    deleted_at: Mapped[datetime | None]


class CalendarEvent(Base):
    """일정. 시각은 Asia/Seoul 기준이다. 종일 일정은 start_at=시작일 0시, end_at=마지막 날 다음 날 0시(끝 제외, iCal 방식).
    rrule 이 있으면 start_at 을 DTSTART 로 하는 반복 일정이고, 조회할 때 구간만 펼친다."""

    __tablename__ = "calendar_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    start_at: Mapped[datetime] = mapped_column(index=True)
    end_at: Mapped[datetime]
    rrule: Mapped[str | None] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None]


# ---- 비서 맥락: 온톨로지 + RAG (ADR 0007)
EMBED_DIM = 1024


class CtxEntity(Base):
    """나와 관련된 것(사람·프로젝트·조직·장소·도구·주제·취향). 온톨로지의 노드."""

    __tablename__ = "ctx_entities"
    __table_args__ = (UniqueConstraint("type", "name"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[Any]] = mapped_column(default=list)
    summary: Mapped[str | None] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    embedding: Mapped[Any] = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class CtxRelation(Base):
    """(주어, 술어, 목적어). 예) winterholic —works_on→ Ttakkari."""

    __tablename__ = "ctx_relations"
    __table_args__ = (UniqueConstraint("subject_id", "predicate", "object_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    subject_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_entities.id", ondelete="CASCADE"), index=True)
    predicate: Mapped[str] = mapped_column(String(60))
    object_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_entities.id", ondelete="CASCADE"), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CtxMemory(Base):
    """사실·취향·결정·사건·지침·메모 한 줄. pinned 는 모든 Run 의 시스템 프롬프트에 들어간다."""

    __tablename__ = "ctx_memories"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(20), default="fact", index=True)
    content: Mapped[str] = mapped_column(Text)
    entity_ids: Mapped[list[Any]] = mapped_column(default=list)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    source_ref: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(EMBED_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    archived_at: Mapped[datetime | None]


class CtxSource(Base):
    """색인할 폴더(또는 파일)와 패턴."""

    __tablename__ = "ctx_sources"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    path: Mapped[str] = mapped_column(Text, unique=True)
    pattern: Mapped[str] = mapped_column(String(100), default="**/*.md")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_indexed_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class CtxDocument(Base):
    __tablename__ = "ctx_documents"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_sources.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64))
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    indexed_at: Mapped[datetime] = mapped_column(default=utcnow)


class CtxChunk(Base):
    __tablename__ = "ctx_chunks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_documents.id", ondelete="CASCADE"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    embedding: Mapped[Any] = mapped_column(Vector(EMBED_DIM), nullable=True)

