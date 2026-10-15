"""On-demand local transcription attempts and immutable source segments."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "local_stt_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "meeting_id",
            sa.Uuid(),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "audio_artifact_id",
            sa.Uuid(),
            sa.ForeignKey("artifacts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "parent_job_id",
            sa.Uuid(),
            sa.ForeignKey("local_stt_jobs.id", ondelete="CASCADE"),
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("params", JSONB(), nullable=False),
        sa.Column("result_metadata", JSONB(), nullable=False),
        sa.Column("transcript", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_local_stt_jobs_meeting_id", "local_stt_jobs", ["meeting_id"])
    op.create_index("ix_local_stt_jobs_status", "local_stt_jobs", ["status"])
    op.create_table(
        "local_stt_segments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("local_stt_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start", sa.Float(), nullable=False),
        sa.Column("end", sa.Float(), nullable=False),
        sa.Column("original_text", sa.Text(), nullable=False),
        sa.Column("corrected_text", sa.Text()),
        sa.Column("speaker_id", sa.String(100)),
        sa.Column("identified_person", sa.String(100)),
        sa.Column("overlap", sa.Boolean()),
        sa.Column("verification_status", sa.String(20), nullable=False),
        sa.Column("avg_logprob", sa.Float()),
        sa.Column("no_speech_prob", sa.Float()),
        sa.Column("compression_ratio", sa.Float()),
        sa.Column("flags", JSONB(), nullable=False),
        sa.Column("words", JSONB(), nullable=False),
        sa.Column("raw", JSONB(), nullable=False),
        sa.UniqueConstraint("job_id", "position"),
    )
    op.create_index("ix_local_stt_segments_job_id", "local_stt_segments", ["job_id"])


def downgrade():
    op.drop_table("local_stt_segments")
    op.drop_table("local_stt_jobs")
