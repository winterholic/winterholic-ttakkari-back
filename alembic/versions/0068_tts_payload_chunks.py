"""TTS accepted work persistence chunks_json on tts_jobs (ADR 0066 / FND-01).

Expand-only: nullable JSONB column for persisted audio text chunks.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0068_tts_payload_chunks"
down_revision = "0067_collector_outbox_retry"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("tts_jobs", sa.Column("chunks_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True))


def downgrade():
    op.drop_column("tts_jobs", "chunks_json")
