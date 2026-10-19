"""NotebookLM 연동: 계정 상태·소스 중복 원장·작업. 쿠키와 토큰은 DB 에 두지 않는다(ADR 0045)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0046_notebooklm"
down_revision = "0045_tossinvest_runs"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "notebooklm_accounts",
        sa.Column("account_id", sa.String(32), primary_key=True),
        sa.Column("state", sa.String(20), nullable=False, server_default="login_required"),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("cooldown_until", _tz(), nullable=True),
        sa.Column("last_error_code", sa.String(40), nullable=True),
        sa.Column("last_ok_at", _tz(), nullable=True),
        sa.Column("last_checked_at", _tz(), nullable=True),
        sa.Column("reauth_notified_at", _tz(), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
    )
    op.create_table(
        "notebooklm_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("notebook_id", sa.String(100), nullable=False),
        sa.Column("dedupe_key", sa.String(80), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("remote_source_id", sa.String(100), nullable=True),
        sa.Column("title", sa.String(300), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="reserved"),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
        sa.UniqueConstraint("account_id", "notebook_id", "dedupe_key", name="uq_notebooklm_sources_key"),
    )
    op.create_index("ix_notebooklm_sources_account_id", "notebooklm_sources", ["account_id"])
    op.create_table(
        "notebooklm_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("notebook_id", sa.String(100), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="queued"),
        sa.Column("params", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("heartbeat_at", _tz(), nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("started_at", _tz(), nullable=True),
        sa.Column("finished_at", _tz(), nullable=True),
        sa.Column("updated_at", _tz(), nullable=False),
    )
    op.create_index("ix_notebooklm_jobs_account_status", "notebooklm_jobs", ["account_id", "status"])
    op.create_index("ix_notebooklm_jobs_updated_at", "notebooklm_jobs", ["updated_at"])


def downgrade():
    op.drop_index("ix_notebooklm_jobs_updated_at", table_name="notebooklm_jobs")
    op.drop_index("ix_notebooklm_jobs_account_status", table_name="notebooklm_jobs")
    op.drop_table("notebooklm_jobs")
    op.drop_index("ix_notebooklm_sources_account_id", table_name="notebooklm_sources")
    op.drop_table("notebooklm_sources")
    op.drop_table("notebooklm_accounts")
