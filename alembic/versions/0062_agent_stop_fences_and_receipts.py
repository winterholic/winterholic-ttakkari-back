"""Persist scoped stop fences, process identity, and stop receipts."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0062_agent_stop_fences"
down_revision = "0061_local_tts"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("pid_started_at", sa.Float(), nullable=True))
    op.add_column("runs", sa.Column("stop_fence_epochs", postgresql.JSONB(astext_type=sa.Text()),
                                     nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_table(
        "agent_stop_fences",
        sa.Column("scope_key", sa.String(180), primary_key=True),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("stopping", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_stop_receipts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("actor_session_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("pool", sa.String(16), nullable=False),
        sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_agent_stop_receipts_actor_session_id", "agent_stop_receipts", ["actor_session_id"])


def downgrade():
    op.drop_index("ix_agent_stop_receipts_actor_session_id", table_name="agent_stop_receipts")
    op.drop_table("agent_stop_receipts")
    op.drop_table("agent_stop_fences")
    op.drop_column("runs", "stop_fence_epochs")
    op.drop_column("runs", "pid_started_at")
