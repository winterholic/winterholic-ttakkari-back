"""AI-native 파이프라인 change_sets 레지스트리 표 추가 (ADR 0066).

Revision ID: 0062_delivery_change_sets
Revises: 0061_local_tts
Create Date: 2026-10-21

NOTE: 수집기(0060) 및 TTS(0061) 작업과 병행 중이므로,
최종 main 직렬 통합 시 down_revision과 번호를 필요에 따라 조정할 수 있다.
신규 표만 추가하므로 무중단 배포(online-safe)에 적합하다.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0062_delivery_change_sets"
down_revision = "0061_local_tts"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "change_sets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("repo_path", sa.String(500), nullable=False),
        sa.Column("branch_name", sa.String(200), nullable=False),
        sa.Column("worktree_path", sa.String(500), nullable=False),
        sa.Column("base_sha", sa.String(40), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=True),
        sa.Column("target_branch", sa.String(100), nullable=False, server_default="main"),
        sa.Column("status", sa.String(32), nullable=False, server_default="registered"),
        sa.Column("stage1_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("stage2_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("diff_guard_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("failure_signature", sa.String(200), nullable=True),
        sa.Column("repair_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signature_repair_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("conflict_files", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("is_dirty", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _tz(), nullable=False, server_default=sa.func.now()),
        sa.Column("submitted_at", _tz(), nullable=True),
        sa.Column("merged_at", _tz(), nullable=True),
    )

    op.create_index(
        "ix_change_sets_status_submitted_at",
        "change_sets",
        ["status", "submitted_at"],
    )
    op.create_index(
        "ix_change_sets_session_id",
        "change_sets",
        ["session_id"],
    )
    op.create_index(
        "ix_change_sets_failure_sig",
        "change_sets",
        ["failure_signature"],
    )


def downgrade():
    op.drop_index("ix_change_sets_failure_sig", table_name="change_sets")
    op.drop_index("ix_change_sets_session_id", table_name="change_sets")
    op.drop_index("ix_change_sets_status_submitted_at", table_name="change_sets")
    op.drop_table("change_sets")
