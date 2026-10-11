"""transfer tokens

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0005'
down_revision: str | None = '0004'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('transfer_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=60), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash'),
    )


def downgrade() -> None:
    op.drop_table('transfer_tokens')
