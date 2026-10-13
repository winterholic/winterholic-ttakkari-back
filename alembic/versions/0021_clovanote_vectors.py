"""meeting_chunks(회의록 벡터), clovanote_notes, clovanote_jobs: 클로바노트 연동과 회의록 벡터 저장(ADR 0021)

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-13
"""
from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
DIM = 1024


def upgrade() -> None:
    op.add_column("meetings", sa.Column("vector_status", sa.String(length=10), server_default="none", nullable=False))
    op.add_column("meetings", sa.Column("vector_chunks", sa.Integer(), server_default="0", nullable=False))
    op.add_column("meetings", sa.Column("vector_error", sa.Text(), nullable=True))
    op.add_column("meetings", sa.Column("embedded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "meeting_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("meeting_id", sa.Uuid(), sa.ForeignKey("meetings.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("ctx_entities.id", ondelete="SET NULL"), nullable=True),
        sa.Column("embedding", Vector(DIM), nullable=True),
        sa.Column("model", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_meeting_chunks_meeting_id", "meeting_chunks", ["meeting_id"])
    op.create_index("ix_meeting_chunks_scope", "meeting_chunks", ["scope"])
    op.create_index("ix_meeting_chunks_entity_id", "meeting_chunks", ["entity_id"])
    op.execute("CREATE INDEX ix_meeting_chunks_embedding ON meeting_chunks USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX ix_meeting_chunks_lexical ON meeting_chunks USING gin (to_tsvector('simple', content))")

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
        sa.Column("meeting_id", sa.Uuid(), sa.ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True),
        sa.Column("transcript_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_clovanote_notes_state", "clovanote_notes", ["state"])
    op.create_index("ix_clovanote_notes_meeting_id", "clovanote_notes", ["meeting_id"])
    op.execute("CREATE INDEX ix_clovanote_notes_lexical ON clovanote_notes USING gin "
               "(to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(transcript, '')))")

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
        sa.Column("meeting_id", sa.Uuid(), sa.ForeignKey("meetings.id", ondelete="SET NULL"), nullable=True),
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


def downgrade() -> None:
    op.drop_table("clovanote_jobs")
    op.drop_table("clovanote_notes")
    op.drop_table("meeting_chunks")
    for c in ("embedded_at", "vector_error", "vector_chunks", "vector_status"):
        op.drop_column("meetings", c)
