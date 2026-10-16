"""Persist auto-worker task policy and strict-engine lock option on sessions."""
import sqlalchemy as sa

from alembic import op

revision = "0037_worker_routing"
down_revision = "0036_system_resource_samples"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("worker_task_type", sa.String(20), nullable=True))
    op.add_column("sessions", sa.Column("strict_engine", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade():
    op.drop_column("sessions", "strict_engine")
    op.drop_column("sessions", "worker_task_type")
