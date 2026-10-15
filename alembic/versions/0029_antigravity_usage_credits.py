"""Persist the separately measured Antigravity AI credit balance."""

import sqlalchemy as sa

from alembic import op

revision = "0029_antigravity_usage_credits"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("account_usage_samples", sa.Column("credits_balance", sa.Integer(), nullable=True))
    op.add_column("account_usage_samples", sa.Column("credits_error", sa.String(length=120), nullable=True))


def downgrade():
    op.drop_column("account_usage_samples", "credits_error")
    op.drop_column("account_usage_samples", "credits_balance")
