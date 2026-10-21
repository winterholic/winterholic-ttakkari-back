"""Change set registry and provenance models for AI-native delivery pipeline (ADR 0066).

공용 models.py 수정을 최소화하기 위해 독립 파일로 관리한다.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ttakkari.models import Base, utcnow

JsonType = JSON().with_variant(JSONB, "postgresql")


class ChangeSetStatus(enum.StrEnum):
    """Change set lifecycle states (ADR 0066 D2, D3)."""

    # 워크스페이스/작업 worktree 생성 시 등록된 초기 상태
    registered = "registered"
    # worker가 수정을 마치고 검증·통합을 요청한 상태
    submitted = "submitted"
    # 최신 main 위로 rebase / fast-forward 대조 중
    rebasing = "rebasing"
    # Stage 1 (계약 묶음, lint, 영향 테스트) 검증 진행 중
    validating_stage1 = "validating_stage1"
    # Stage 2 (rebase 후 고위험 영역 및 통합 필수 묶음) 검증 진행 중
    validating_stage2 = "validating_stage2"
    # 검증 완료, main 브랜치 승격 직전
    ready_for_merge = "ready_for_merge"
    # main 브랜치에 안전하게 병합 완료
    merged = "merged"
    # rebase/merge 충돌 발생 (원 세션 피드백 반환, 큐 차단 해제)
    conflict = "conflict"
    # 검증 실패, diff guard 위반, 또는 repair 상한 도달
    rejected = "rejected"
    # dirty 변경이나 미통합 자산 보존된 채 작업 종료
    abandoned = "abandoned"


class ChangeSet(Base):
    """PostgreSQL 기반 change set registry (ADR 0066 D2)."""

    __tablename__ = "change_sets"
    __table_args__ = (
        Index("ix_change_sets_status_submitted_at", "status", "submitted_at"),
        Index("ix_change_sets_failure_sig", "failure_signature"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    repo_path: Mapped[str] = mapped_column(String(500))
    branch_name: Mapped[str] = mapped_column(String(200))  # e.g. "work/<session_id>"
    worktree_path: Mapped[str] = mapped_column(String(500))

    base_sha: Mapped[str] = mapped_column(String(40))  # 작업 시작 시점 main SHA
    head_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)  # 제출된 커밋 SHA
    candidate_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    candidate_worktree_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_branch: Mapped[str] = mapped_column(String(100), default="main")

    status: Mapped[str] = mapped_column(
        String(32), default=ChangeSetStatus.registered, index=True
    )

    stage1_result: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    stage2_result: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    diff_guard_result: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)

    # 실패 서명(test_id + exception_type 등)
    failure_signature: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    # 총 수리 시도 횟수 (상한: 3회)
    repair_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    # 동일 서명에 대한 수리 시도 횟수 (상한: 2회)
    signature_repair_attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    signature_attempts: Mapped[dict[str, int]] = mapped_column(JsonType, default=dict, server_default="{}")

    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    conflict_files: Mapped[list[str] | None] = mapped_column(JsonType, nullable=True)
    is_dirty: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
