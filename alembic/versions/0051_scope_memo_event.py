"""Memo·CalendarEvent scope 도입(SEC2-AUTHZ-01).

기존 메모와 일정은 personal 로 이관(server_default="personal")한다.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051_scope_memo_event"
down_revision: str | None = "0050_login_throttle_shadow"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("memos", "calendar_events"):
        op.add_column(
            table,
            sa.Column(
                "scope", sa.String(20), nullable=False, server_default="personal"
            ),
        )
        op.create_index(f"ix_{table}_scope", table, ["scope"])


def downgrade() -> None:
    for table in ("memos", "calendar_events"):
        op.drop_index(f"ix_{table}_scope", table)
        op.drop_column(table, "scope")
