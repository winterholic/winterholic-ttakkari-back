"""Persist notifications independently of Web Push subscriptions."""
import sqlalchemy as sa

from alembic import op

revision = "0031_notifications"
down_revision = "0030_runtime_generations"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("notifications",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_notifications_category", "notifications", ["category"])


def downgrade():
    op.drop_index("ix_notifications_category", table_name="notifications")
    op.drop_table("notifications")
