"""meetings, meeting_items, meeting_proposals: 회의록(ADR 0010)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0016'
down_revision: str | None = '0015'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'meetings',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('scope', sa.String(length=20), server_default='personal', nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('entity_id', sa.Uuid(), sa.ForeignKey('ctx_entities.id', ondelete='SET NULL'), nullable=True),
        sa.Column('held_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('participants', postgresql.JSONB(), nullable=False),
        sa.Column('source', sa.String(length=10), nullable=False),
        sa.Column('audio_artifact_id', sa.Uuid(), sa.ForeignKey('artifacts.id', ondelete='SET NULL'), nullable=True),
        sa.Column('provider', sa.String(length=30), nullable=True),
        sa.Column('provider_job_id', sa.String(length=200), nullable=True),
        sa.Column('transcript', sa.Text(), nullable=True),
        sa.Column('transcript_source', sa.String(length=40), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('analysis_run_id', sa.Uuid(), sa.ForeignKey('runs.id', ondelete='SET NULL'), nullable=True),
        sa.Column('analysis_session_id', sa.Uuid(), sa.ForeignKey('sessions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('analysis_context', postgresql.JSONB(), nullable=False),
        sa.Column('failed_stage', sa.String(length=20), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('analyzed_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_meetings_scope', 'meetings', ['scope'])
    op.create_index('ix_meetings_status', 'meetings', ['status'])
    op.create_index('ix_meetings_entity_id', 'meetings', ['entity_id'])
    op.create_index('ix_meetings_created', 'meetings', ['created_at'])
    op.create_table(
        'meeting_items',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('meeting_id', sa.Uuid(), sa.ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.Column('owner', sa.String(length=100), nullable=True),
        sa.Column('due', sa.String(length=40), nullable=True),
        sa.Column('evidence', sa.Text(), nullable=True),
        sa.Column('conflicts', postgresql.JSONB(), nullable=False),
        sa.Column('status', sa.String(length=12), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_meeting_items_meeting_id', 'meeting_items', ['meeting_id'])
    op.create_index('ix_meeting_items_kind', 'meeting_items', ['kind'])
    op.create_table(
        'meeting_proposals',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('meeting_id', sa.Uuid(), sa.ForeignKey('meetings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('item_id', sa.Uuid(), sa.ForeignKey('meeting_items.id', ondelete='SET NULL'), nullable=True),
        sa.Column('kind', sa.String(length=24), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('payload', postgresql.JSONB(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        sa.Column('result', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_meeting_proposals_meeting_id', 'meeting_proposals', ['meeting_id'])
    op.create_index('ix_meeting_proposals_status', 'meeting_proposals', ['status'])


def downgrade() -> None:
    op.drop_table('meeting_proposals')
    op.drop_table('meeting_items')
    op.drop_table('meetings')
