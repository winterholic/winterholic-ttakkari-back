"""Persist exact account pins on sessions."""
import sqlalchemy as sa

from alembic import op

revision = "0077_artcraft_account_pin"
down_revision = "0076_kb_v3_collection_merge"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sessions", sa.Column("account_id", sa.Uuid(), nullable=True))
    op.create_index("ix_sessions_account_id", "sessions", ["account_id"])


def downgrade():
    op.drop_index("ix_sessions_account_id", table_name="sessions")
    op.drop_column("sessions", "account_id")
