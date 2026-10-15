from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
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
    # 엔진을 직접 돌리며 작업 세션을 부리는 세션. parent_id 가 접수(desk)면 접수가 넘긴 일을 맡은 기획 세션이다(ADR 0013).
    orchestrator = "orchestrator"
    worker = "worker"
    # 사용자가 대화하는 접수. 가벼운 모델이 바로 답하거나 기획 세션에 넘긴다. 세션 대기열 없이 메시지마다 따로 돈다.
    desk = "desk"


class ArtifactArea(enum.StrEnum):
    # 사용자가 올렸거나 사용자에게 전달하려고 낸 결과물
    files = "files"
    # 에이전트가 작업 중 만든 중간 산출물·자동 등록 파일
    agent = "agent"


def default_artifact_area(source: str) -> str:
    """등록 경로(source)만으로 정하는 기본 분류. 사용자가 직접 넣은 것만 파일, 나머지는 에이전트 쪽이다."""
    return ArtifactArea.files if source in ("upload", "user_registered") else ArtifactArea.agent


class RunOrigin(enum.StrEnum):
    user = "user"
    # 작업 세션이 끝나 오케스트레이터에게 들어가는 결과 보고
    report = "report"
    # 오케스트레이터가 도구로 작업 세션에 보낸 지시
    orchestrator = "orchestrator"
    # 접수가 기획 세션에 넘긴 사용자 요청
    desk = "desk"


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
    # 이 대화가 쓸 수 있는 계정 범위(personal=전체 목록, work=회사 그룹만). 작업 세션은 오케스트레이터 것을 물려받는다.
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
    # 신규 작업 배정은 5시간·주간 중 하나라도 이 사용률 이상이면 초기화까지 쉰다.
    max_used_percent: Mapped[int] = mapped_column(Integer, default=100, server_default="100")
    # 회사 모드 포함 여부(personal | work). 모든 계정은 개인 목록에 있고, work 는 그중 회사 모드에도 쓰려고 고른 계정이다.
    pool: Mapped[str] = mapped_column(String(16), default="personal", index=True)
    # 회사 모드 전용 켜기·순서. enabled·priority(개인 모드)와 독립이고, pool=work 인 계정에만 의미가 있다.
    work_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    work_priority: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class AccountUsageSample(Base):
    """계정 사용량 측정값. 계정별 가장 최근 행이 '최신값', 나머지는 추세 판단용 이력이다(보존 기간 제한)."""

    __tablename__ = "account_usage_samples"
    __table_args__ = (Index("ix_account_usage_samples_account_measured", "account_id", "measured_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("engine_accounts.id", ondelete="CASCADE"))
    measured_at: Mapped[datetime] = mapped_column(index=True)
    source: Mapped[str] = mapped_column(String(16))  # api | stream | session_log
    plan: Mapped[str | None] = mapped_column(String(40))
    # [{key, percent, resets_at(unix 초)}]. 토큰은 어디에도 두지 않는다.
    windows: Mapped[list[Any]] = mapped_column(default=list)


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
    heartbeat_at: Mapped[datetime | None]
    resume_attempts: Mapped[int] = mapped_column(Integer, default=0)
    resume_after: Mapped[datetime | None]
    resume_handoff: Mapped[str | None] = mapped_column(Text)
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


class DeskFeedback(Base):
    """접수 분류에 대한 사용자 판정. 실제 메시지로 접수 지침·모델을 재는 평가 문항이 된다(ADR 0013).

    message·decided 는 판정 시점에 복사해 둔다. Run 이 지워지면 판정도 같이 지운다(평가 문항도 사라진다)."""

    __tablename__ = "desk_feedback"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), unique=True)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    message: Mapped[str] = mapped_column(Text)
    decided: Mapped[str] = mapped_column(String(16))  # answer | delegate
    expected: Mapped[str] = mapped_column(String(16))  # answer | delegate
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)


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
    # 보관함 기본 목록(files)과 에이전트 파일(agent)의 구분. source 는 출처, area 는 사용자에게 보이는 분류다.
    area: Mapped[str] = mapped_column(String(10), default=ArtifactArea.agent, server_default=ArtifactArea.agent, index=True)
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
class MemoCategory(Base):
    """메모 카테고리. 메모는 0~1개에 속하고, 카테고리를 지우면 메모는 미분류(category_id NULL)로 남는다.
    color 는 디자인 토큰의 범주 색 키(planner/service.CATEGORY_COLORS)이고 없어도 된다."""

    __tablename__ = "memo_categories"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(30), unique=True)
    color: Mapped[str | None] = mapped_column(String(20))
    position: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Memo(Base):
    """마크다운 메모. rev 는 낙관적 잠금(자동 저장·에이전트 수정이 서로 덮어쓰지 않게). deleted_at 은 에이전트의 보관 삭제."""

    __tablename__ = "memos"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[Any]] = mapped_column(default=list)
    category_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("memo_categories.id", ondelete="SET NULL"), index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    rev: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow, index=True)
    deleted_at: Mapped[datetime | None]


class CalendarEvent(Base):
    """일정. 시각은 Asia/Seoul 기준이다. 종일 일정은 start_at=시작일 0시, end_at=마지막 날 다음 날 0시(끝 제외, iCal 방식).
    rrule 이 있으면 start_at 을 DTSTART 로 하는 반복 일정이고, 조회할 때 구간만 펼친다.
    NOTE: 반복 입력은 보류(재설계 예정, ADR 0008). 컬럼은 남겨 기존 값만 읽는다.
    reminder 는 알림 옵션(planner/reminders.py), remind_at 은 보낼 시각, reminded_at 은 보냈거나 건너뛴 시각이다."""

    __tablename__ = "calendar_events"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    start_at: Mapped[datetime] = mapped_column(index=True)
    end_at: Mapped[datetime]
    rrule: Mapped[str | None] = mapped_column(String(500))
    reminder: Mapped[str | None] = mapped_column(String(20))
    remind_at: Mapped[datetime | None] = mapped_column(index=True)
    reminded_at: Mapped[datetime | None]
    source: Mapped[str] = mapped_column(String(20), default="user")  # user | agent
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None]


# ---- 비서 맥락: 온톨로지 + RAG (ADR 0007)
# 기존 벡터 데이터의 비파괴 호환용. 현재 검색·색인은 임베딩을 생성하거나 사용하지 않는다(ADR 0009).
EMBED_DIM = 1024


class CtxEntity(Base):
    """나와 관련된 것(사람·프로젝트·조직·장소·도구·주제·취향). 온톨로지의 노드."""

    __tablename__ = "ctx_entities"
    __table_args__ = (UniqueConstraint("type", "name"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    type: Mapped[str] = mapped_column(String(30), index=True)
    name: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(20), default="personal", server_default="personal", index=True)
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
    scope: Mapped[str] = mapped_column(String(20), default="personal", server_default="personal", index=True)
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
    scope: Mapped[str] = mapped_column(String(20), default="personal", server_default="personal", index=True)
    category: Mapped[str] = mapped_column(String(30), default="knowledge", server_default="knowledge")
    entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ctx_entities.id", ondelete="SET NULL"), index=True)
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


# ---- 회의록 (ADR 0010)
class MeetingStatus(enum.StrEnum):
    uploaded = "uploaded"  # 녹음 보관됨, 전사 전
    transcribing = "transcribing"  # 전사 Provider 가 처리 중
    transcribed = "transcribed"  # 전사 텍스트 확보, 분석 전
    analyzing = "analyzing"  # 분석 Run 진행
    analyzed = "analyzed"  # 분석 끝, 대기 중인 제안 없음
    review = "review"  # 분석 끝, 승인 대기 제안 있음
    failed = "failed"  # failed_stage 가 어느 단계인지 알려 준다


class MeetingItemKind(enum.StrEnum):
    discussion = "discussion"
    decision = "decision"
    action = "action"
    open_issue = "open_issue"


class Meeting(Base):
    __tablename__ = "meetings"
    __table_args__ = (Index("ix_meetings_created", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(200))
    scope: Mapped[str] = mapped_column(String(20), default="personal", server_default="personal", index=True)
    status: Mapped[str] = mapped_column(String(16), default=MeetingStatus.uploaded, index=True)
    # 연결한 프로젝트(ctx_entities). 없어도 된다.
    entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ctx_entities.id", ondelete="SET NULL"), index=True)
    pms_project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pms_projects.id", ondelete="SET NULL"), index=True)
    held_at: Mapped[datetime | None]
    participants: Mapped[list[Any]] = mapped_column(default=list)
    source: Mapped[str] = mapped_column(String(10), default="text")  # audio | text
    audio_artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("artifacts.id", ondelete="SET NULL"))
    # 전사 Provider 요청 기록. 인증 정보는 여기에 두지 않는다.
    provider: Mapped[str | None] = mapped_column(String(30))
    provider_job_id: Mapped[str | None] = mapped_column(String(200))
    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_source: Mapped[str | None] = mapped_column(String(40))  # paste | file | provider:<id>
    summary: Mapped[str | None] = mapped_column(Text)
    analysis_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    analysis_session_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sessions.id", ondelete="SET NULL"))
    # 분석 프롬프트에 넣은 기존 맥락 후보 {"M1": {type, id, text}, "A1": ...}. 충돌 참조는 이 표로만 해석한다.
    analysis_context: Mapped[dict[str, Any]] = mapped_column(default=dict)
    failed_stage: Mapped[str | None] = mapped_column(String(20))  # transcription | analysis
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    analyzed_at: Mapped[datetime | None]
    # 맥락 도서관 벡터 저장(ADR 0021). none | pending | ready | failed. 조각은 meeting_chunks.
    vector_status: Mapped[str] = mapped_column(String(10), default="none", server_default="none")
    vector_chunks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    vector_error: Mapped[str | None] = mapped_column(Text)
    embedded_at: Mapped[datetime | None]


class LocalSttJob(Base):
    """Immutable transcription attempts; child jobs are candidates, never replacements."""
    __tablename__ = "local_stt_jobs"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    audio_artifact_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("artifacts.id", ondelete="SET NULL"))
    parent_job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("local_stt_jobs.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    params: Mapped[dict[str, Any]] = mapped_column(default=dict)
    result_metadata: Mapped[dict[str, Any]] = mapped_column(default=dict)
    transcript: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class LocalSttSegment(Base):
    __tablename__ = "local_stt_segments"
    __table_args__ = (UniqueConstraint("job_id", "position"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("local_stt_jobs.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    start: Mapped[float] = mapped_column(Float)
    end: Mapped[float] = mapped_column(Float)
    original_text: Mapped[str] = mapped_column(Text)
    corrected_text: Mapped[str | None] = mapped_column(Text)
    speaker_id: Mapped[str | None] = mapped_column(String(100))
    # Human identification is a separate, explicitly verified layer (not inferred from names).
    identified_person: Mapped[str | None] = mapped_column(String(100))
    overlap: Mapped[bool | None] = mapped_column(Boolean)
    verification_status: Mapped[str] = mapped_column(String(20), default="unverified")
    avg_logprob: Mapped[float | None] = mapped_column(Float)
    no_speech_prob: Mapped[float | None] = mapped_column(Float)
    compression_ratio: Mapped[float | None] = mapped_column(Float)
    flags: Mapped[list[Any]] = mapped_column(default=list)
    words: Mapped[list[Any]] = mapped_column(default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(default=dict)


class MeetingItem(Base):
    __tablename__ = "meeting_items"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16), index=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(100))
    due: Mapped[str | None] = mapped_column(String(40))
    evidence: Mapped[str | None] = mapped_column(Text)
    # 분석이 지목한 기존 기억·액션과의 충돌 [{ref, type, target_id, text, reason, change}]. 반영은 제안으로만 한다.
    conflicts: Mapped[list[Any]] = mapped_column(default=list)
    # open | done | cancelled. 액션·미해결 쟁점에만 의미가 있고 나머지는 open 으로 둔다.
    status: Mapped[str] = mapped_column(String(12), default="open")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class MeetingProposal(Base):
    """맥락 도서관 반영 제안. 사용자가 승인해야만 기억·액션이 바뀐다(pending → approved | rejected)."""

    __tablename__ = "meeting_proposals"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    item_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("meeting_items.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(24))  # add_memory | supersede_memory | update_action_item
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(default=dict)
    reason: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    resolved_at: Mapped[datetime | None]


class MeetingChunk(Base):
    """회의 한 건을 검색 단위로 자른 조각(요약·결정·액션·쟁점·논의·전사 구간)과 임베딩. 맥락 도서관 recall 이 같이 찾는다(ADR 0021).
    정본은 meetings·meeting_items 이고 이 표는 언제든 다시 만들 수 있는 색인이다."""

    __tablename__ = "meeting_chunks"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    meeting_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("meetings.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # summary | decision | action | open_issue | discussion | transcript
    ord: Mapped[int] = mapped_column(Integer, default=0)
    heading: Mapped[str | None] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    scope: Mapped[str] = mapped_column(String(20), default="personal", index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("ctx_entities.id", ondelete="SET NULL"), index=True)
    embedding: Mapped[Any] = mapped_column(Vector(EMBED_DIM), nullable=True)
    model: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class RetiredIntegrationRecord(Base):
    """Migration-only source snapshots; never used to connect to a retired service."""
    __tablename__ = "retired_integration_records"
    source: Mapped[str] = mapped_column(String(40), primary_key=True)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    payload: Mapped[dict[str, Any]] = mapped_column()
    archived_at: Mapped[datetime] = mapped_column(default=utcnow)


# ---- 프로젝트 관리 PMS · WBS (ADR 0012)
class PmsProject(Base):
    __tablename__ = "pms_projects"
    __table_args__ = (Index("ix_pms_projects_created", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    scope: Mapped[str] = mapped_column(String(20), default="personal", server_default="personal", index=True)
    # planning | active | on_hold | done
    status: Mapped[str] = mapped_column(String(12), default="planning", server_default="planning", index=True)
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    # 사람 이름 텍스트 목록. 계정이 아니라 담당자 자동완성에 쓰는 이름표다.
    members: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class PmsTask(Base):
    """WBS 의 한 줄이자 칸반의 카드. 말단(자식 없음) 작업만 자기 상태·진행률을 갖고, 상위는 읽을 때 자식에서 집계한다."""

    __tablename__ = "pms_tasks"
    __table_args__ = (Index("ix_pms_tasks_project_parent", "project_id", "parent_id"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pms_projects.id", ondelete="CASCADE"), index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pms_tasks.id", ondelete="CASCADE"))
    # 같은 부모 아래 순서. 0 부터 빈틈없이 매긴다.
    position: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="", server_default="")
    # todo | doing | review | done
    status: Mapped[str] = mapped_column(String(10), default="todo", server_default="todo")
    # low | normal | high | urgent
    priority: Mapped[str] = mapped_column(String(10), default="normal", server_default="normal")
    assignee: Mapped[str | None] = mapped_column(String(100))
    start_date: Mapped[date | None] = mapped_column(Date)
    end_date: Mapped[date | None] = mapped_column(Date)
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 선행 작업 id 목록(끝→시작). 같은 프로젝트 안에서 순환 없이만 허용한다.
    predecessors: Mapped[list[Any]] = mapped_column(default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    done_at: Mapped[datetime | None]


class PmsMilestone(Base):
    __tablename__ = "pms_milestones"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pms_projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    due_date: Mapped[date]
    done: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class PmsNote(Base):
    """이슈·리스크·메모 한 장."""

    __tablename__ = "pms_notes"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("pms_projects.id", ondelete="CASCADE"), index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("pms_tasks.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(8), default="issue")  # issue | risk | note
    title: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="", server_default="")
    severity: Mapped[str] = mapped_column(String(8), default="medium")  # low | medium | high
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | resolved
    owner: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None]


class ChecklistTemplate(Base):
    __tablename__ = "checklist_templates"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)


class ChecklistItem(Base):
    __tablename__ = "checklist_items"
    __table_args__ = (UniqueConstraint("day", "template_id"), Index("ix_checklist_items_day", "day"))
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    day: Mapped[date] = mapped_column(Date)
    template_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("checklist_templates.id"), default=None)
    title: Mapped[str] = mapped_column(String(300))
    checked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(default=None)


class ChecklistReminder(Base):
    __tablename__ = "checklist_reminders"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    slot: Mapped[str] = mapped_column(String(5), primary_key=True)
    claimed_at: Mapped[datetime] = mapped_column(default=utcnow)
