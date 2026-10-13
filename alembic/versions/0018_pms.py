"""pms_projects, pms_tasks, pms_milestones, pms_notes: 프로젝트 관리 PMS·WBS(ADR 0012)

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0018'
down_revision: str | None = '0017'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'pms_projects',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('name', sa.String(length=200), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('scope', sa.String(length=20), server_default='personal', nullable=False),
        sa.Column('status', sa.String(length=12), server_default='planning', nullable=False),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('members', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_pms_projects_scope', 'pms_projects', ['scope'])
    op.create_index('ix_pms_projects_status', 'pms_projects', ['status'])
    op.create_index('ix_pms_projects_created', 'pms_projects', ['created_at'])
    op.create_table(
        'pms_tasks',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('pms_projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('parent_id', sa.Uuid(), sa.ForeignKey('pms_tasks.id', ondelete='CASCADE'), nullable=True),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('description', sa.Text(), server_default='', nullable=False),
        sa.Column('status', sa.String(length=10), server_default='todo', nullable=False),
        sa.Column('priority', sa.String(length=10), server_default='normal', nullable=False),
        sa.Column('assignee', sa.String(length=100), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('end_date', sa.Date(), nullable=True),
        sa.Column('progress', sa.Integer(), server_default='0', nullable=False),
        sa.Column('predecessors', postgresql.JSONB(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('done_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_pms_tasks_project_id', 'pms_tasks', ['project_id'])
    op.create_index('ix_pms_tasks_project_parent', 'pms_tasks', ['project_id', 'parent_id'])
    op.create_table(
        'pms_milestones',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('pms_projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('title', sa.String(length=200), nullable=False),
        sa.Column('due_date', sa.Date(), nullable=False),
        sa.Column('done', sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_pms_milestones_project_id', 'pms_milestones', ['project_id'])
    op.create_table(
        'pms_notes',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('project_id', sa.Uuid(), sa.ForeignKey('pms_projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('task_id', sa.Uuid(), sa.ForeignKey('pms_tasks.id', ondelete='SET NULL'), nullable=True),
        sa.Column('kind', sa.String(length=8), nullable=False),
        sa.Column('title', sa.String(length=300), nullable=False),
        sa.Column('body', sa.Text(), server_default='', nullable=False),
        sa.Column('severity', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=10), nullable=False),
        sa.Column('owner', sa.String(length=100), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('resolved_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index('ix_pms_notes_project_id', 'pms_notes', ['project_id'])


def downgrade() -> None:
    op.drop_table('pms_notes')
    op.drop_table('pms_milestones')
    op.drop_table('pms_tasks')
    op.drop_table('pms_projects')
