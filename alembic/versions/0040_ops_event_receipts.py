"""Preserve source event IDs independently of sampled details; additive only."""
import sqlalchemy as sa

from alembic import op

revision = "0040_ops_event_receipts"
down_revision = "0039_ops_log_issues"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ops_event_receipts",
        sa.Column("dedupe_key", sa.String(120), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_ops_event_receipts_created_at", "ops_event_receipts", ["created_at"])
    op.execute("INSERT INTO ops_event_receipts (dedupe_key) SELECT dedupe_key FROM ops_events WHERE dedupe_key IS NOT NULL ON CONFLICT DO NOTHING")


def downgrade():
    op.drop_index("ix_ops_event_receipts_created_at", table_name="ops_event_receipts")
    op.drop_table("ops_event_receipts")
