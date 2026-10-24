"""Durable inputs for background consumers moved off the API process (platform v2 Step 6).

Expand-only: nullable columns. Old API generations ignore them during an online deploy.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0070_background_consumer_queue"
down_revision = "0069_game_build_executor_handoff"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("notebooklm_jobs", sa.Column("spec", postgresql.JSONB(astext_type=sa.Text()), nullable=True))
    op.add_column("ctx_sources", sa.Column("index_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ctx_sources", sa.Column("index_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("ctx_sources", "index_started_at")
    op.drop_column("ctx_sources", "index_requested_at")
    op.drop_column("notebooklm_jobs", "spec")
