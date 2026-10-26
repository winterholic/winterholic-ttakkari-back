"""Durable collection intent and links to the existing acquisition ledger. Additive."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0072_collection_missions"
down_revision = "0071_library_platform_v2_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "collection_missions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(256), nullable=False),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("gaps", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "request_key", name="uq_collection_mission_request"),
    )
    op.create_index("ix_collection_mission_created", "collection_missions", ["tenant_id", "created_at"])
    op.create_table(
        "collection_mission_jobs",
        sa.Column("mission_id", sa.Uuid(), sa.ForeignKey("collection_missions.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("collector_jobs.id", ondelete="RESTRICT"), primary_key=True),
    )


def downgrade():
    op.drop_table("collection_mission_jobs")
    op.drop_table("collection_missions")
