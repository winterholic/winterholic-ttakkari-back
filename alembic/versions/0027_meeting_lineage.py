"""Persist the direct source meeting independently of STT attempt records."""

import sqlalchemy as sa

from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("meetings", sa.Column("source_meeting_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_meetings_source_meeting_id",
        "meetings",
        "meetings",
        ["source_meeting_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_meetings_source_meeting_id", "meetings", ["source_meeting_id"])
    # Match UUIDs as text: malformed/orphan legacy params must not abort deployment.
    # Only full attempts establish lineage; earliest valid attempt wins deterministically.
    op.execute(
        sa.text("""
        UPDATE meetings AS child SET source_meeting_id = origin.source_id
        FROM (
            SELECT DISTINCT ON (job.meeting_id) job.meeting_id, source.id AS source_id
            FROM local_stt_jobs AS job
            JOIN meetings AS source ON source.id::text = job.params->>'source_meeting_id'
            WHERE job.parent_job_id IS NULL AND job.meeting_id <> source.id
            ORDER BY job.meeting_id, job.created_at, job.id
        ) AS origin
        WHERE child.id = origin.meeting_id AND child.source_meeting_id IS NULL
    """)
    )


def downgrade():
    op.drop_index("ix_meetings_source_meeting_id", table_name="meetings")
    op.drop_constraint("fk_meetings_source_meeting_id", "meetings", type_="foreignkey")
    op.drop_column("meetings", "source_meeting_id")
