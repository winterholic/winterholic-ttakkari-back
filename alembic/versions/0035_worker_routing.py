"""Persist auto-worker task policy without changing existing session engine locks."""
import sqlalchemy as sa

from alembic import op

revision = "0035_worker_routing"
down_revision = "0034_knowledge_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("worker_task_type", sa.String(20), nullable=True))


def downgrade():
    op.drop_column("sessions", "worker_task_type")
