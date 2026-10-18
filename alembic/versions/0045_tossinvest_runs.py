"""토스증권 연결 확인·동기화 기록. 금액·수량·쿠키는 저장하지 않는다(ADR 0044)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0045_tossinvest_runs"
down_revision = "0044_reverse_reconstruction"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "tossinvest_sync_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("logged_in", sa.Boolean(), nullable=True),
        sa.Column("page_title", sa.String(200), nullable=True),
        sa.Column("api_paths", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "request_source", sa.String(12), nullable=False, server_default="manual"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_tossinvest_sync_runs_status", "tossinvest_sync_runs", ["status"]
    )
    op.create_index(
        "ix_tossinvest_sync_runs_created", "tossinvest_sync_runs", ["created_at"]
    )


def downgrade():
    op.drop_index("ix_tossinvest_sync_runs_created", table_name="tossinvest_sync_runs")
    op.drop_index("ix_tossinvest_sync_runs_status", table_name="tossinvest_sync_runs")
    op.drop_table("tossinvest_sync_runs")
