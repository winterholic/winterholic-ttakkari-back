"""persist Run heartbeat and bounded restart-resume state

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("resume_attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("runs", sa.Column("resume_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("resume_handoff", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "resume_handoff")
    op.drop_column("runs", "resume_after")
    op.drop_column("runs", "resume_attempts")
    op.drop_column("runs", "heartbeat_at")
