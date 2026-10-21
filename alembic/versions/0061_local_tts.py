"""로컬 TTS 표(tts_jobs, tts_cache). 신규 표만 추가한다(online-safe). 원문 컬럼은 두지 않는다(ADR 0062)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0061_local_tts"
down_revision = "0060_collector_job_runtime"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "tts_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("started_at", _tz(), nullable=True),
        sa.Column("finished_at", _tz(), nullable=True),
    )
    op.create_index("ix_tts_jobs_status_owner_created", "tts_jobs", ["status", "owner_id", "created_at"])
    op.create_table(
        "tts_cache",
        sa.Column("chunk_key", sa.String(64), primary_key=True),
        sa.Column("engine", sa.String(40), nullable=False),
        sa.Column("model_revision", sa.String(80), nullable=False),
        sa.Column("voice", sa.String(40), nullable=False),
        sa.Column("speed", sa.Float(), nullable=False),
        sa.Column("chunk_hash", sa.String(64), nullable=False),
        sa.Column("audio_relpath", sa.String(120), nullable=False),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("last_hit_at", _tz(), nullable=False),
    )
    op.create_index("ix_tts_cache_last_hit_at", "tts_cache", ["last_hit_at"])


def downgrade():
    op.drop_index("ix_tts_cache_last_hit_at", table_name="tts_cache")
    op.drop_table("tts_cache")
    op.drop_index("ix_tts_jobs_status_owner_created", table_name="tts_jobs")
    op.drop_table("tts_jobs")
