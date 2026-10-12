"""memo categories

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0012'
down_revision: str | None = '0011'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'memo_categories',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('name', sa.String(length=30), nullable=False, unique=True),
        sa.Column('color', sa.String(length=20), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.add_column('memos', sa.Column('category_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('memos_category_id_fkey', 'memos', 'memo_categories', ['category_id'], ['id'], ondelete='SET NULL')
    op.create_index('ix_memos_category_id', 'memos', ['category_id'])


def downgrade() -> None:
    op.drop_index('ix_memos_category_id', 'memos')
    op.drop_constraint('memos_category_id_fkey', 'memos', type_='foreignkey')
    op.drop_column('memos', 'category_id')
    op.drop_table('memo_categories')
