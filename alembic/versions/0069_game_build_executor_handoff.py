"""Game build executor handoff marker on game_builds (Platform V2 Step 5).

Expand-only: two nullable columns. Existing rows keep NULL, which the executor never claims.
"""
import sqlalchemy as sa

from alembic import op

revision = "0069_game_build_executor_handoff"
down_revision = "0068_tts_payload_chunks"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("game_builds", sa.Column("queue_state", sa.String(10), nullable=True))
    op.add_column("game_builds", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("game_builds", "claimed_at")
    op.drop_column("game_builds", "queue_state")
