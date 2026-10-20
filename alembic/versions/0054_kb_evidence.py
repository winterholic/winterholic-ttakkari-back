"""범용 지식 도서관 KB1: 근거 테이블 + 불변 트리거 + 인용 구간 해시 검증(ADR 0063).

kb_sources·kb_revisions·kb_passages·kb_evidence 는 UPDATE 를 거부한다.
새 수집·새 파서는 새 행이다. 검토에서 스키마 수정이 오면 이 단위만 되돌린다.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0054_kb_evidence"
down_revision = "0053_kb_core"
branch_labels = None
depends_on = None

_IMMUTABLE_TABLES = ("kb_sources", "kb_revisions", "kb_passages", "kb_evidence")

IMMUTABLE_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_immutable_row() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'kb rows are immutable' USING ERRCODE='23514';
END;
$$ LANGUAGE plpgsql
"""
SPAN_FUNCTION = """
CREATE OR REPLACE FUNCTION kb_validate_evidence_span() RETURNS trigger AS $$
DECLARE original text;
BEGIN
  SELECT body INTO original FROM kb_passages WHERE id=NEW.passage_id;
  IF original IS NULL OR NEW.span_start < 0 OR NEW.span_end <= NEW.span_start
      OR NEW.span_end > char_length(original)
      OR encode(sha256(convert_to(substring(original FROM NEW.span_start+1 FOR NEW.span_end-NEW.span_start), 'UTF8')), 'hex') <> NEW.sha256 THEN
    RAISE EXCEPTION 'kb evidence span or digest mismatch' USING ERRCODE='23514';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "kb_evidence",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("passage_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("span_start", sa.Integer(), nullable=False),
        sa.Column("span_end", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["passage_id"], ["kb_passages.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "passage_id", "span_start", "span_end", name="uq_kb_evidence_span"
        ),
        sa.CheckConstraint(
            "span_start >= 0 AND span_end > span_start", name="ck_kb_evidence_span"
        ),
    )
    op.create_index("ix_kb_evidence_passage_id", "kb_evidence", ["passage_id"])
    op.execute(IMMUTABLE_FUNCTION)
    for table in _IMMUTABLE_TABLES:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION kb_immutable_row()"
        )
    op.execute(SPAN_FUNCTION)
    op.execute(
        "CREATE TRIGGER kb_evidence_validate BEFORE INSERT ON kb_evidence "
        "FOR EACH ROW EXECUTE FUNCTION kb_validate_evidence_span()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS kb_evidence_validate ON kb_evidence")
    op.execute("DROP FUNCTION IF EXISTS kb_validate_evidence_span()")
    for table in _IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
    op.execute("DROP FUNCTION IF EXISTS kb_immutable_row()")
    op.drop_index("ix_kb_evidence_passage_id", table_name="kb_evidence")
    op.drop_table("kb_evidence")
