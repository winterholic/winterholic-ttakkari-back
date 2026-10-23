"""Delivery retry bookkeeping on the outbox. Additive: nullable or defaulted columns only (CP-S2-08)."""
from alembic import op

revision = "0067_collector_outbox_retry"
down_revision = "0066_collector_budget"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE collector_outbox ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE collector_outbox ADD COLUMN not_before TIMESTAMP WITH TIME ZONE")
    op.execute("ALTER TABLE collector_outbox ADD COLUMN last_error VARCHAR(300)")


def downgrade():
    op.execute("ALTER TABLE collector_outbox DROP COLUMN last_error")
    op.execute("ALTER TABLE collector_outbox DROP COLUMN not_before")
    op.execute("ALTER TABLE collector_outbox DROP COLUMN attempts")
