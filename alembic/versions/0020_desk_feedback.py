"""desk_feedback: 접수 분류에 대한 사용자 판정(ADR 0013)

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "desk_feedback",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("decided", sa.String(length=16), nullable=False),
        sa.Column("expected", sa.String(length=16), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_desk_feedback_session_id", "desk_feedback", ["session_id"])
    op.create_index("ix_desk_feedback_updated_at", "desk_feedback", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_desk_feedback_updated_at", table_name="desk_feedback")
    op.drop_index("ix_desk_feedback_session_id", table_name="desk_feedback")
    op.drop_table("desk_feedback")
