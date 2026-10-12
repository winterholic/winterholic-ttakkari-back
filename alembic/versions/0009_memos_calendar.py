"""memos and calendar events

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-12
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0009'
down_revision: str | None = '0008'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'memos',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('body', sa.Text(), nullable=False),
        sa.Column('tags', postgresql.JSONB(), nullable=False),
        sa.Column('pinned', sa.Boolean(), nullable=False),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('rev', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_memos_updated_at', 'memos', ['updated_at'])
    op.create_table(
        'calendar_events',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), nullable=False),
        sa.Column('location', sa.String(length=200), nullable=False),
        sa.Column('all_day', sa.Boolean(), nullable=False),
        sa.Column('start_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('end_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('rrule', sa.String(length=500), nullable=True),
        sa.Column('source', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_calendar_events_start_at', 'calendar_events', ['start_at'])


def downgrade() -> None:
    op.drop_index('ix_calendar_events_start_at', 'calendar_events')
    op.drop_table('calendar_events')
    op.drop_index('ix_memos_updated_at', 'memos')
    op.drop_table('memos')
