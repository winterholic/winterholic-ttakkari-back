"""account pools -> one global order with a work group tag

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0010'
down_revision: str | None = '0009'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    # 회사 계정은 자기 묶음 안 순서(0..)였다. 개인 목록 맨 뒤로 이어 붙여 전체 순서 하나로 만든다.
    top = conn.execute(sa.text("SELECT COALESCE(MAX(priority), -1) FROM engine_accounts WHERE pool = 'personal'")).scalar()
    conn.execute(sa.text("UPDATE engine_accounts SET priority = priority + :off WHERE pool = 'work'"), {"off": top + 1})
    # 임시 순서는 하나만 남는다. 개인 것을 되살리고, 회사 것(전체 순서와 어긋남)은 버린다.
    conn.execute(sa.text("DELETE FROM app_settings WHERE key = 'account_priority_override:work'"))
    conn.execute(sa.text("UPDATE app_settings SET key = 'account_priority_override' WHERE key = 'account_priority_override:personal'"))


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(sa.text("UPDATE app_settings SET key = 'account_priority_override:personal' WHERE key = 'account_priority_override'"))
    # 회사 계정 순서를 묶음 안 0.. 으로 되돌린다.
    rows = conn.execute(sa.text("SELECT id FROM engine_accounts WHERE pool = 'work' ORDER BY priority, created_at")).fetchall()
    for i, (aid,) in enumerate(rows):
        conn.execute(sa.text("UPDATE engine_accounts SET priority = :p WHERE id = :id"), {"p": i, "id": aid})
