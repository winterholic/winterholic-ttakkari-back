"""로그인 제한 S1 shadow 관측 표(SEC2-LGN-01): login_throttle, login_bucket, login_attempts. 신규 표만 추가한다(online-safe)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0050_login_throttle_shadow"
down_revision = "0049_google_accounts"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "login_throttle",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("kind", sa.String(8), nullable=False),
        sa.Column("budget", sa.Integer(), nullable=False),
        sa.Column("level", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", _tz(), nullable=True),
        sa.Column("stage_started_at", _tz(), nullable=False),
        sa.Column("last_failure_at", _tz(), nullable=True),
        sa.Column("lock_trigger", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summarized_at", _tz(), nullable=True),
        sa.Column("updated_at", _tz(), nullable=False),
    )
    op.create_index("ix_login_throttle_updated_at", "login_throttle", ["updated_at"])
    op.create_table(
        "login_bucket",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("refilled_at", _tz(), nullable=False),
        sa.Column("blocked_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "login_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("lane", sa.String(4), nullable=False),
        sa.Column("src_key", sa.String(100), nullable=True),
        sa.Column("reserved_at", _tz(), nullable=False),
        sa.Column("would_block", sa.String(24), nullable=True),
        sa.Column("charged_src", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("charged_acct", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("outcome", sa.String(10), nullable=False, server_default="pending"),
    )
    op.create_index("ix_login_attempts_reserved_at", "login_attempts", ["reserved_at"])


def downgrade():
    op.drop_index("ix_login_attempts_reserved_at", table_name="login_attempts")
    op.drop_table("login_attempts")
    op.drop_table("login_bucket")
    op.drop_index("ix_login_throttle_updated_at", table_name="login_throttle")
    op.drop_table("login_throttle")
