"""work mode keeps its own on/off and order per account

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0017'
down_revision: str | None = '0016'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('engine_accounts', sa.Column('work_enabled', sa.Boolean(), nullable=False, server_default=sa.true()))
    op.add_column('engine_accounts', sa.Column('work_priority', sa.Integer(), nullable=False, server_default='0'))
    conn = op.get_bind()
    # 이미 회사 그룹이던 계정은 지금 켜기·상대 순서를 회사 모드의 시작값으로 삼는다. 개인 모드 값은 그대로 둔다.
    conn.execute(sa.text("UPDATE engine_accounts SET work_enabled = enabled WHERE pool = 'work'"))
    rows = conn.execute(sa.text("SELECT id FROM engine_accounts WHERE pool = 'work' ORDER BY priority, created_at")).fetchall()
    for i, (aid,) in enumerate(rows):
        conn.execute(sa.text("UPDATE engine_accounts SET work_priority = :p WHERE id = :id"), {"p": i, "id": aid})


def downgrade() -> None:
    op.drop_column('engine_accounts', 'work_priority')
    op.drop_column('engine_accounts', 'work_enabled')
