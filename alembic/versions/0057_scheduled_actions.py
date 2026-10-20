"""영속 예약 작업 표(scheduled_actions): 재배포·세대 전환·재시작 뒤에도 살아남는 작업 큐. 신규 표만 추가한다(online-safe)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0057_scheduled_actions"
down_revision = "0056_research_lab"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "scheduled_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("due_at", _tz(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("owner_id", sa.String(64), nullable=True),
        sa.Column("leased_until", _tz(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
        sa.Column("executed_at", _tz(), nullable=True),
    )
    op.create_index("ix_scheduled_actions_action_type", "scheduled_actions", ["action_type"])
    op.create_index("ix_scheduled_actions_due_at", "scheduled_actions", ["due_at"])
    op.create_index("ix_scheduled_actions_status", "scheduled_actions", ["status"])
    op.create_index("ix_scheduled_actions_owner_id", "scheduled_actions", ["owner_id"])
    op.create_index("ix_scheduled_actions_leased_until", "scheduled_actions", ["leased_until"])
    op.create_index("ix_scheduled_actions_status_due", "scheduled_actions", ["status", "due_at"])


def downgrade():
    op.drop_index("ix_scheduled_actions_status_due", table_name="scheduled_actions")
    op.drop_index("ix_scheduled_actions_leased_until", table_name="scheduled_actions")
    op.drop_index("ix_scheduled_actions_owner_id", table_name="scheduled_actions")
    op.drop_index("ix_scheduled_actions_status", table_name="scheduled_actions")
    op.drop_index("ix_scheduled_actions_due_at", table_name="scheduled_actions")
    op.drop_index("ix_scheduled_actions_action_type", table_name="scheduled_actions")
    op.drop_table("scheduled_actions")
