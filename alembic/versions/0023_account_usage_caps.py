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
    # 초기값(10-14): 보호 대상 계정 두 개만 95%. 다른 계정은 DB 기본값 100%이며 이후 비서관리에서 바꾼다.
    op.execute("UPDATE engine_accounts SET max_used_percent = 95 WHERE (engine = 'claude' AND lower(name) = lower('내 클로드')) OR (engine = 'codex' AND lower(name) = lower('Daddy codex'))")


def downgrade() -> None:
    op.drop_column("engine_accounts", "max_used_percent")
