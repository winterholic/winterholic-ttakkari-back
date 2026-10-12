"""account_usage_samples: 계정별 사용량 측정값(최신값 + 추세용 이력)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0015'
down_revision: str | None = '0014'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'account_usage_samples',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('account_id', sa.Uuid(), sa.ForeignKey('engine_accounts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('measured_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('plan', sa.String(length=40), nullable=True),
        sa.Column('windows', postgresql.JSONB(), nullable=False),
    )
    op.create_index('ix_account_usage_samples_measured_at', 'account_usage_samples', ['measured_at'])
    op.create_index('ix_account_usage_samples_account_measured', 'account_usage_samples', ['account_id', 'measured_at'])


def downgrade() -> None:
    op.drop_index('ix_account_usage_samples_account_measured', 'account_usage_samples')
    op.drop_index('ix_account_usage_samples_measured_at', 'account_usage_samples')
    op.drop_table('account_usage_samples')
