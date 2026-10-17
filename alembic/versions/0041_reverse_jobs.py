"""Reverse-engineering jobs (ADR 0038); additive only."""

import sqlalchemy as sa

from alembic import op

revision = "0041_reverse_jobs"
down_revision = "0040_ops_event_receipts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "reverse_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("revision", sa.String(64)),
        sa.Column("stages", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_reverse_jobs_status", "reverse_jobs", ["status"])
    op.create_index("ix_reverse_jobs_updated_at", "reverse_jobs", ["updated_at"])


def downgrade():
    op.drop_index("ix_reverse_jobs_updated_at", table_name="reverse_jobs")
    op.drop_index("ix_reverse_jobs_status", table_name="reverse_jobs")
    op.drop_table("reverse_jobs")
