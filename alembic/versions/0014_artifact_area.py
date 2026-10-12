"""artifacts.area: 보관함 파일(files)과 에이전트 파일(agent) 분리

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = '0014'
down_revision: str | None = '0013'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 불확실하면 에이전트 쪽이 기본이다. 사용자가 올린 것만 아래에서 파일로 올린다.
    op.add_column('artifacts', sa.Column('area', sa.String(length=10), nullable=False, server_default='agent'))
    op.create_index('ix_artifacts_area', 'artifacts', ['area'])
    # 사용자 업로드·사용자가 직접 등록한 파일은 반드시 파일.
    op.execute("UPDATE artifacts SET area = 'files' WHERE source IN ('upload', 'user_registered')")
    # outbox 결과물은 사용자와 직접 대화한 세션(chat·orchestrator)의 것만 파일.
    # 작업 세션(worker)의 outbox, 세션이 사라진 것, 작업이 고친 워크스페이스 파일(agent_modified)은 에이전트 파일로 남는다.
    op.execute("""
        UPDATE artifacts SET area = 'files'
        WHERE source = 'agent_output'
          AND session_id IN (SELECT id FROM sessions WHERE kind IN ('chat', 'orchestrator'))
    """)


def downgrade() -> None:
    op.drop_index('ix_artifacts_area', 'artifacts')
    op.drop_column('artifacts', 'area')
