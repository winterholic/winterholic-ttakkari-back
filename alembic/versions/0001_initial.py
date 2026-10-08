"""initial

Revision ID: 0001
Revises: (없음, 빈 DB 기준)
Create Date: 2026-10-09
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('audit_logs',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('actor', sa.String(length=16), nullable=False),
    sa.Column('action', sa.String(length=60), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('target_type', sa.String(length=30), nullable=True),
    sa.Column('target_id', sa.String(length=100), nullable=True),
    sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_audit_logs_action'), 'audit_logs', ['action'], unique=False)
    op.create_index(op.f('ix_audit_logs_at'), 'audit_logs', ['at'], unique=False)
    op.create_table('credentials',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('refresh_tokens',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('family_id', sa.Uuid(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('replaced_by', sa.Uuid(), nullable=True),
    sa.Column('user_agent', sa.String(length=512), nullable=True),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_index(op.f('ix_refresh_tokens_family_id'), 'refresh_tokens', ['family_id'], unique=False)
    op.create_table('workspaces',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('root_path', sa.Text(), nullable=False),
    sa.Column('is_git', sa.Boolean(), nullable=False),
    sa.Column('default_engine', sa.String(length=16), nullable=False),
    sa.Column('codex_sandbox', sa.String(length=32), nullable=False),
    sa.Column('extra_writable_roots', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('export_allowed', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('root_path')
    )
    op.create_table('sessions',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('engine', sa.String(length=16), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('effort', sa.String(length=16), nullable=True),
    sa.Column('engine_session_id', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('archived_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_sessions_workspace_id'), 'sessions', ['workspace_id'], unique=False)
    op.create_table('runs',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('session_id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=False),
    sa.Column('prompt', sa.Text(), nullable=False),
    sa.Column('context_artifact_ids', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('engine', sa.String(length=16), nullable=False),
    sa.Column('model', sa.String(length=100), nullable=True),
    sa.Column('effort', sa.String(length=16), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('result_text', sa.Text(), nullable=True),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('usage', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('checkpoint_ref', sa.String(length=100), nullable=True),
    sa.Column('outbox_path', sa.Text(), nullable=True),
    sa.Column('last_seq', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_runs_session_id'), 'runs', ['session_id'], unique=False)
    op.create_index(op.f('ix_runs_status'), 'runs', ['status'], unique=False)
    op.create_index(op.f('ix_runs_workspace_id'), 'runs', ['workspace_id'], unique=False)
    op.create_table('artifacts',
    sa.Column('id', sa.Uuid(), nullable=False),
    sa.Column('workspace_id', sa.Uuid(), nullable=True),
    sa.Column('session_id', sa.Uuid(), nullable=True),
    sa.Column('run_id', sa.Uuid(), nullable=True),
    sa.Column('filename', sa.String(length=500), nullable=False),
    sa.Column('mime_type', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('source', sa.String(length=30), nullable=False),
    sa.Column('original_path', sa.Text(), nullable=False),
    sa.Column('rel_path', sa.Text(), nullable=True),
    sa.Column('storage_mode', sa.String(length=10), nullable=False),
    sa.Column('stored_path', sa.Text(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('preview_status', sa.String(length=16), nullable=False),
    sa.Column('preview_path', sa.Text(), nullable=True),
    sa.Column('preview_mime', sa.String(length=100), nullable=True),
    sa.Column('preview_error', sa.Text(), nullable=True),
    sa.Column('export_policy', sa.String(length=16), nullable=False),
    sa.Column('retention_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('deleted_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['session_id'], ['sessions.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workspace_id'], ['workspaces.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_artifacts_created', 'artifacts', ['created_at'], unique=False)
    op.create_index(op.f('ix_artifacts_run_id'), 'artifacts', ['run_id'], unique=False)
    op.create_index(op.f('ix_artifacts_session_id'), 'artifacts', ['session_id'], unique=False)
    op.create_index(op.f('ix_artifacts_workspace_id'), 'artifacts', ['workspace_id'], unique=False)
    op.create_table('run_events',
    sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.Uuid(), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('run_id', 'seq')
    )


def downgrade() -> None:
    op.drop_table('run_events')
    op.drop_index(op.f('ix_artifacts_workspace_id'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_session_id'), table_name='artifacts')
    op.drop_index(op.f('ix_artifacts_run_id'), table_name='artifacts')
    op.drop_index('ix_artifacts_created', table_name='artifacts')
    op.drop_table('artifacts')
    op.drop_index(op.f('ix_runs_workspace_id'), table_name='runs')
    op.drop_index(op.f('ix_runs_status'), table_name='runs')
    op.drop_index(op.f('ix_runs_session_id'), table_name='runs')
    op.drop_table('runs')
    op.drop_index(op.f('ix_sessions_workspace_id'), table_name='sessions')
    op.drop_table('sessions')
    op.drop_table('workspaces')
    op.drop_index(op.f('ix_refresh_tokens_family_id'), table_name='refresh_tokens')
    op.drop_table('refresh_tokens')
    op.drop_table('credentials')
    op.drop_index(op.f('ix_audit_logs_at'), table_name='audit_logs')
    op.drop_index(op.f('ix_audit_logs_action'), table_name='audit_logs')
    op.drop_table('audit_logs')
