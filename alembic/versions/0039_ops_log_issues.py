"""Add operational log issues, events, hourly stats and ingest cursors (ADR 0033). New tables only."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0039_ops_log_issues"
down_revision = "0038_qwen_flash_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ops_issues",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("fingerprint", sa.String(length=40), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("culprit", sa.String(length=200), nullable=True),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False, server_default="unresolved"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_count", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("first_release", sa.String(length=40), nullable=True),
        sa.Column("last_release", sa.String(length=40), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_release", sa.String(length=40), nullable=True),
        sa.Column("regressed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_regressed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_ops_issues_fingerprint"),
    )
    op.create_index("ix_ops_issues_source", "ops_issues", ["source"])
    op.create_index("ix_ops_issues_last_seen", "ops_issues", ["last_seen"])
    op.create_index("ix_ops_issues_status_last_seen", "ops_issues", ["status", "last_seen"])
    op.create_table(
        "ops_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("issue_id", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source", sa.String(length=24), nullable=False),
        sa.Column("level", sa.String(length=10), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("release", sa.String(length=40), nullable=True),
        sa.Column("tags", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("breadcrumbs", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("stack", sa.Text(), nullable=True),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("ref", sa.String(length=200), nullable=True),
        sa.Column("dedupe_key", sa.String(length=120), nullable=True),
        sa.ForeignKeyConstraint(["issue_id"], ["ops_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_ops_events_dedupe_key"),
    )
    op.create_index("ix_ops_events_occurred_at", "ops_events", ["occurred_at"])
    op.create_index("ix_ops_events_release", "ops_events", ["release"])
    op.create_index("ix_ops_events_ref", "ops_events", ["ref"])
    op.create_index("ix_ops_events_issue_occurred", "ops_events", ["issue_id", "occurred_at"])
    op.create_table(
        "ops_issue_stats",
        sa.Column("issue_id", sa.BigInteger(), nullable=False),
        sa.Column("bucket", sa.DateTime(timezone=True), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["issue_id"], ["ops_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("issue_id", "bucket"),
    )
    op.create_index("ix_ops_issue_stats_bucket", "ops_issue_stats", ["bucket"])
    op.create_table(
        "ops_ingest_cursors",
        sa.Column("key", sa.String(length=300), nullable=False),
        sa.Column("inode", sa.BigInteger(), nullable=True),
        sa.Column("position", sa.BigInteger(), nullable=False, server_default=sa.text("0")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade():
    op.drop_table("ops_ingest_cursors")
    op.drop_index("ix_ops_issue_stats_bucket", table_name="ops_issue_stats")
    op.drop_table("ops_issue_stats")
    for name in ("ix_ops_events_issue_occurred", "ix_ops_events_ref", "ix_ops_events_release", "ix_ops_events_occurred_at"):
        op.drop_index(name, table_name="ops_events")
    op.drop_table("ops_events")
    for name in ("ix_ops_issues_status_last_seen", "ix_ops_issues_last_seen", "ix_ops_issues_source"):
        op.drop_index(name, table_name="ops_issues")
    op.drop_table("ops_issues")
