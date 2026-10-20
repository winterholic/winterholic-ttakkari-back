"""범용 지식 도서관 KB1 보완: DELETE 차단 불변 트리거 및 RESTRICT 외래키(검토 지적 1).

원장 행(kb_sources, kb_revisions, kb_passages, kb_evidence)의 DELETE 를 거부하고,
부모 문서·리비전·패시지 삭제로 인한 연쇄 삭제(CASCADE)를 방지하기 위해 외래키를 RESTRICT 로 변경한다.
"""

from alembic import op

revision = "0055_kb_immutability_hardening"
down_revision = "0054_kb_evidence"
branch_labels = None
depends_on = None

_IMMUTABLE_TABLES = ("kb_sources", "kb_revisions", "kb_passages", "kb_evidence")


def upgrade() -> None:
    # 1. 트리거 강화: BEFORE UPDATE -> BEFORE UPDATE OR DELETE
    for table in _IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION kb_immutable_row()"
        )

    # 2. 외래키 제약조건 강화: CASCADE -> RESTRICT
    op.drop_constraint("kb_revisions_document_id_fkey", "kb_revisions", type_="foreignkey")
    op.create_foreign_key(
        "kb_revisions_document_id_fkey",
        "kb_revisions",
        "kb_documents",
        ["document_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("kb_passages_revision_id_fkey", "kb_passages", type_="foreignkey")
    op.create_foreign_key(
        "kb_passages_revision_id_fkey",
        "kb_passages",
        "kb_revisions",
        ["revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.drop_constraint("kb_evidence_passage_id_fkey", "kb_evidence", type_="foreignkey")
    op.create_foreign_key(
        "kb_evidence_passage_id_fkey",
        "kb_evidence",
        "kb_passages",
        ["passage_id"],
        ["id"],
        ondelete="RESTRICT",
    )


def downgrade() -> None:
    # 1. 외래키 제약조건 복원: RESTRICT -> CASCADE
    op.drop_constraint("kb_evidence_passage_id_fkey", "kb_evidence", type_="foreignkey")
    op.create_foreign_key(
        "kb_evidence_passage_id_fkey",
        "kb_evidence",
        "kb_passages",
        ["passage_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("kb_passages_revision_id_fkey", "kb_passages", type_="foreignkey")
    op.create_foreign_key(
        "kb_passages_revision_id_fkey",
        "kb_passages",
        "kb_revisions",
        ["revision_id"],
        ["id"],
        ondelete="CASCADE",
    )

    op.drop_constraint("kb_revisions_document_id_fkey", "kb_revisions", type_="foreignkey")
    op.create_foreign_key(
        "kb_revisions_document_id_fkey",
        "kb_revisions",
        "kb_documents",
        ["document_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 2. 트리거 복원: BEFORE UPDATE OR DELETE -> BEFORE UPDATE
    for table in _IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_immutable ON {table}")
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION kb_immutable_row()"
        )
