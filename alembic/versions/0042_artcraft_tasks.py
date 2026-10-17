"""Add ArtCraft work tasks (ADR 0039). New table only."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0042_artcraft_tasks"
down_revision = "0041_reverse_jobs"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "artcraft_tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("apps", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("template_id", sa.String(length=40), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_artcraft_tasks_session_id"),
    )
    op.create_index("ix_artcraft_tasks_category_updated", "artcraft_tasks", ["category", "updated_at"])


def downgrade():
    op.drop_index("ix_artcraft_tasks_category_updated", table_name="artcraft_tasks")
    op.drop_table("artcraft_tasks")
