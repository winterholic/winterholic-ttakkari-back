"""orchestrator: session kind/parent, per-profile engine sessions, run origin/profile

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0003'
down_revision: str | None = '0002'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column('sessions', sa.Column('engine_sessions', postgresql.JSONB(astext_type=sa.Text()), nullable=False,
                                        server_default=sa.text("'{}'::jsonb")))
    op.add_column('sessions', sa.Column('kind', sa.String(length=16), nullable=False, server_default='chat'))
    op.add_column('sessions', sa.Column('parent_id', sa.Uuid(), nullable=True))
    op.create_foreign_key('fk_sessions_parent', 'sessions', 'sessions', ['parent_id'], ['id'], ondelete='CASCADE')
    op.create_index('ix_sessions_kind', 'sessions', ['kind'])
    op.create_index('ix_sessions_parent_id', 'sessions', ['parent_id'])
    # 기존 대화를 기본 프로필 이름(= 엔진 이름) 아래로 옮겨 resume 이 이어지게 한다.
    op.execute("UPDATE sessions SET engine_sessions = jsonb_build_object(engine, engine_session_id) "
               "WHERE engine_session_id IS NOT NULL")
    op.add_column('runs', sa.Column('origin', sa.String(length=16), nullable=False, server_default='user'))
    op.add_column('runs', sa.Column('profile', sa.String(length=40), nullable=True))


def downgrade() -> None:
    op.drop_column('runs', 'profile')
    op.drop_column('runs', 'origin')
    op.drop_index('ix_sessions_parent_id', 'sessions')
    op.drop_index('ix_sessions_kind', 'sessions')
    op.drop_constraint('fk_sessions_parent', 'sessions', type_='foreignkey')
    op.drop_column('sessions', 'parent_id')
    op.drop_column('sessions', 'kind')
    op.drop_column('sessions', 'engine_sessions')
