"""Retire the external integration without discarding source data (ADR 0027).

Revision ID: 0026; Revises: 0025. Only the deployment controller runs this on production.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retired_integration_records",
        sa.Column("source", sa.String(40), primary_key=True),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False),
    )
    for table in ("clovanote_notes", "clovanote_jobs"):
        op.execute(
            sa.text(
                f"INSERT INTO retired_integration_records (source,id,payload,archived_at) "
                f"SELECT '{table}', id, to_jsonb(t), now() FROM {table} t"
            )
        )
    # A copied meeting is the public record. Never overwrite an existing/user-edited transcript.
    op.execute("""UPDATE meetings m SET transcript = n.transcript,
                  transcript_source = COALESCE(m.transcript_source, 'provider:clovanote')
        FROM (SELECT DISTINCT ON (meeting_id) meeting_id, transcript
              FROM clovanote_notes WHERE meeting_id IS NOT NULL AND btrim(COALESCE(transcript,'')) <> ''
              ORDER BY meeting_id, transcript_fetched_at DESC NULLS LAST, synced_at DESC, id) n
        WHERE m.id=n.meeting_id AND btrim(COALESCE(m.transcript,'')) = ''""")
    op.drop_table("clovanote_jobs")
    op.drop_table("clovanote_notes")


def downgrade() -> None:
    op.create_table(
        "clovanote_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("note_id", sa.String(length=100), nullable=False, unique=True),
        sa.Column("workspace_id", sa.String(length=100), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("remote_status", sa.String(length=40), nullable=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("remote_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("speakers", postgresql.JSONB(), nullable=False),
        sa.Column("segments", postgresql.JSONB(), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column("remote_summary", sa.Text(), nullable=True),
        sa.Column("keywords", postgresql.JSONB(), nullable=False),
        sa.Column("meta", postgresql.JSONB(), nullable=False),
        sa.Column(
            "meeting_id",
            sa.Uuid(),
            sa.ForeignKey("meetings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("transcript_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_clovanote_notes_state", "clovanote_notes", ["state"])
    op.create_index("ix_clovanote_notes_meeting_id", "clovanote_notes", ["meeting_id"])
    op.execute(
        "CREATE INDEX ix_clovanote_notes_lexical ON clovanote_notes USING gin (to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(transcript, '')))"
    )
    op.create_table(
        "clovanote_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("stage", sa.String(length=30), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("note_id", sa.String(length=100), nullable=True),
        sa.Column(
            "meeting_id",
            sa.Uuid(),
            sa.ForeignKey("meetings.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_clovanote_jobs_created", "clovanote_jobs", ["created_at"])
    op.create_index("ix_clovanote_jobs_status", "clovanote_jobs", ["status"])
    op.create_index("ix_clovanote_jobs_note_id", "clovanote_jobs", ["note_id"])
    op.create_index("ix_clovanote_jobs_meeting_id", "clovanote_jobs", ["meeting_id"])
    for table in ("clovanote_notes", "clovanote_jobs"):
        # Meetings may have been deleted after retirement. Keep the source, detach orphan links.
        op.execute(
            sa.text(f"""INSERT INTO {table}
            SELECT (jsonb_populate_record(NULL::{table}, CASE
                WHEN payload->>'meeting_id' IS NOT NULL AND NOT EXISTS
                    (SELECT 1 FROM meetings WHERE id=(payload->>'meeting_id')::uuid)
                THEN payload || jsonb_build_object('meeting_id', NULL) ELSE payload END)).*
            FROM retired_integration_records WHERE source='{table}'""")
        )
    # Rescued text stays in meetings: downgrading must not remove newly available user data.
    op.drop_table("retired_integration_records")
