"""맥락 공유 영속 모델. models.py 수정을 피하려고 독립 파일로 둔다. 원문 키·control 토큰은 어떤 열에도 저장하지 않는다."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
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


class ShareProject(Base):
    """Gateway 프로젝트 slug 와 라이브러리 프로젝트(CtxEntity)의 연결. 항목이 지워져도 철회할 수 있게 slug 를 남긴다."""

    __tablename__ = "ctx_share_projects"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("ctx_entities.id", ondelete="SET NULL"), unique=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    # Gateway 가 정한 프로젝트 UUID. 첫 게시 영수증에서 받고, 키 발급 ACL 에 쓴다.
    gateway_project_id: Mapped[str | None] = mapped_column(String(36))
    revoke_state: Mapped[str] = mapped_column(String(16), default="active")  # active | revoking
    revoke_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class ShareDraft(Base):
    __tablename__ = "ctx_share_drafts"
    __table_args__ = (Index("ix_ctx_share_drafts_project_created", "project_id", "created_at"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_share_projects.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)  # draft | published
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[str] = mapped_column(String(8), default="owner")  # owner | agent
    scope: Mapped[str] = mapped_column(String(20), default="personal")
    digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class ShareDraftDocument(Base):
    __tablename__ = "ctx_share_draft_documents"
    __table_args__ = (UniqueConstraint("draft_id", "logical_path"),)
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    draft_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_share_drafts.id", ondelete="CASCADE"), index=True)
    position: Mapped[int] = mapped_column(Integer)
    # 선택 당시의 ctx_documents.id. 재색인으로 사라질 수 있어 FK 를 걸지 않는다.
    source_document_id: Mapped[uuid.UUID | None]
    title: Mapped[str] = mapped_column(String(500))
    logical_path: Mapped[str] = mapped_column(String(500))
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    source_refs: Mapped[list[Any]] = mapped_column(default=list)
    source_revision: Mapped[str] = mapped_column(String(128))
    generated_at: Mapped[datetime]
    verified_at: Mapped[datetime | None]
    verified_by: Mapped[str | None] = mapped_column(String(16))
    content_origin: Mapped[str] = mapped_column(String(20), default="indexed_chunks")
    edited: Mapped[bool] = mapped_column(Boolean, default=False)


class SharePublication(Base):
    __tablename__ = "ctx_share_publications"
    __table_args__ = (
        Index("uq_ctx_share_publications_active", "project_id", unique=True, postgresql_where="status = 'active'"),
    )
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_share_projects.id", ondelete="CASCADE"), index=True)
    draft_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_share_drafts.id", ondelete="CASCADE"), unique=True)
    digest: Mapped[str] = mapped_column(String(64))
    gateway_snapshot_id: Mapped[str] = mapped_column(String(64))
    gateway_manifest_sha256: Mapped[str | None] = mapped_column(String(64))
    # active: 최신. superseded: 더 새 스냅샷이 있지만 Gateway 에는 폐기 전까지 남아 있다. revoked: Gateway 폐기 확인됨.
    status: Mapped[str] = mapped_column(String(12), default="active")
    published_at: Mapped[datetime] = mapped_column(default=utcnow)
    revoked_at: Mapped[datetime | None]


class ShareKey(Base):
    """발급한 키의 메타데이터. 원문 키는 발급 응답에만 있고 여기에 없다."""

    __tablename__ = "ctx_share_keys"
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_share_projects.id", ondelete="CASCADE"), index=True)
    publication_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ctx_share_publications.id", ondelete="CASCADE"))
    gateway_key_id: Mapped[str] = mapped_column(String(64), unique=True)
    label: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | revoked
    ttl_seconds: Mapped[int] = mapped_column(Integer)
    issued_at: Mapped[datetime] = mapped_column(default=utcnow)
    expires_at: Mapped[datetime]
    revoked_at: Mapped[datetime | None]
    credential_type: Mapped[str] = mapped_column(String(24), default="single_use_v1")
    snapshot_policy: Mapped[str] = mapped_column(String(8), default="latest")
    pinned_snapshot_id: Mapped[str | None] = mapped_column(String(36))
    scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    last_used_at: Mapped[datetime | None]
    revoke_error: Mapped[str | None] = mapped_column(String(64))
