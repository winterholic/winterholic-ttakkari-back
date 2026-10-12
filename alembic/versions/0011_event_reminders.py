"""calendar event reminders

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-12
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0011'
down_revision: str | None = '0010'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('calendar_events', sa.Column('reminder', sa.String(length=20), nullable=True))
    op.add_column('calendar_events', sa.Column('remind_at', sa.DateTime(timezone=True), nullable=True))
    op.add_column('calendar_events', sa.Column('reminded_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index('ix_calendar_events_remind_at', 'calendar_events', ['remind_at'])


def downgrade() -> None:
    op.drop_index('ix_calendar_events_remind_at', 'calendar_events')
    op.drop_column('calendar_events', 'reminded_at')
    op.drop_column('calendar_events', 'remind_at')
    op.drop_column('calendar_events', 'reminder')
