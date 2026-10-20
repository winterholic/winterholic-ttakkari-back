"""범용 지식 도서관 KB1: 원본·문서·리비전·패시지 테이블. 근거·트리거는 0054 에 둔다(ADR 0063)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0053_kb_core"
down_revision = "0052_artifact_area_byproducts"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "kb_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source_key", sa.String(500), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("locator", sa.Text(), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=True),
        sa.Column("store_path", sa.String(500), nullable=False),
        sa.Column("collected_at", _tz(), nullable=False),
        sa.Column("origin_meta", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.UniqueConstraint("source_key", "sha256", name="uq_kb_source_bytes"),
        sa.CheckConstraint("byte_size >= 0", name="ck_kb_source_bytes"),
    )
    op.create_index("ix_kb_sources_source_key", "kb_sources", ["source_key"])
    op.create_index("ix_kb_sources_sha256", "kb_sources", ["sha256"])
    op.create_table(
        "kb_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("doc_key", sa.String(500), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("lang", sa.String(16), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
        sa.UniqueConstraint("doc_key", name="uq_kb_documents_doc_key"),
    )
    op.create_table(
        "kb_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("rev_no", sa.Integer(), nullable=False),
        sa.Column("parser_version", sa.String(60), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("text_sha256", sa.String(64), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False),
        sa.Column("valid_from", _tz(), nullable=True),
        sa.Column("valid_to", _tz(), nullable=True),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["kb_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_id"], ["kb_sources.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["kb_revisions.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("document_id", "rev_no", name="uq_kb_revision_no"),
        sa.CheckConstraint("rev_no > 0", name="ck_kb_revision_no"),
        sa.CheckConstraint("char_count >= 0", name="ck_kb_revision_chars"),
        sa.CheckConstraint(
            "valid_from IS NULL OR valid_to IS NULL OR valid_from < valid_to",
            name="ck_kb_revision_valid_time",
        ),
    )
    op.create_index("ix_kb_revisions_document_id", "kb_revisions", ["document_id"])
    op.create_index("ix_kb_revisions_source_id", "kb_revisions", ["source_id"])
    op.create_index("ix_kb_revisions_supersedes_id", "kb_revisions", ["supersedes_id"])
    op.create_table(
        "kb_passages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ord", sa.Integer(), nullable=False),
        sa.Column("heading", sa.Text(), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("char_start", sa.Integer(), nullable=False),
        sa.Column("char_end", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("created_at", _tz(), nullable=False),
        sa.ForeignKeyConstraint(["revision_id"], ["kb_revisions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("revision_id", "ord", name="uq_kb_passage_ord"),
        sa.CheckConstraint("ord >= 0", name="ck_kb_passage_ord"),
        sa.CheckConstraint(
            "char_start >= 0 AND char_end > char_start", name="ck_kb_passage_span"
        ),
    )
    op.create_index("ix_kb_passages_revision_id", "kb_passages", ["revision_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_passages_revision_id", table_name="kb_passages")
    op.drop_table("kb_passages")
    op.drop_index("ix_kb_revisions_supersedes_id", table_name="kb_revisions")
    op.drop_index("ix_kb_revisions_source_id", table_name="kb_revisions")
    op.drop_index("ix_kb_revisions_document_id", table_name="kb_revisions")
    op.drop_table("kb_revisions")
    op.drop_table("kb_documents")
    op.drop_index("ix_kb_sources_sha256", table_name="kb_sources")
    op.drop_index("ix_kb_sources_source_key", table_name="kb_sources")
    op.drop_table("kb_sources")
