"""per-account usage ceilings

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-14
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("engine_accounts", sa.Column("max_used_percent", sa.Integer(), server_default="100", nullable=False))
    # 현재 개인 계정 식별(10-14): Claude 계정 1개와 이름이 Daddy codex 인 계정만 95%로 보호한다.
    op.execute("UPDATE engine_accounts SET max_used_percent = 95 WHERE engine = 'claude' OR (engine = 'codex' AND name ILIKE '%daddy%')")


def downgrade() -> None:
    op.drop_column("engine_accounts", "max_used_percent")
