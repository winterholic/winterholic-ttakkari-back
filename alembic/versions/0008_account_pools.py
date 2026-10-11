"""account pools (personal | work) for accounts and sessions

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0008'
down_revision: str | None = '0007'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for t in ('engine_accounts', 'sessions'):
        op.add_column(t, sa.Column('pool', sa.String(length=16), nullable=False, server_default='personal'))
        op.create_index(f'ix_{t}_pool', t, ['pool'])
    # 기존 임시 순서는 개인 계정 것이다.
    op.execute("UPDATE app_settings SET key = 'account_priority_override:personal' WHERE key = 'account_priority_override'")


def downgrade() -> None:
    op.execute("UPDATE app_settings SET key = 'account_priority_override' WHERE key = 'account_priority_override:personal'")
    for t in ('engine_accounts', 'sessions'):
        op.drop_index(f'ix_{t}_pool', t)
        op.drop_column(t, 'pool')
