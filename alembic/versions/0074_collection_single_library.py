"""Single knowledge library (C2): access pool columns, document aliases and a merge ledger. Additive, online.

기존 scope 값·CHECK 제약·행은 건드리지 않는다. 0072 세대 코드는 새 컬럼의 server default 로 그대로 동작한다.
pool 은 새 컬럼에만 채우므로 이전 세대가 읽는 값은 바뀌지 않는다. 실제 scope 정규화와 중복 병합은 이전 세대가 빠진 뒤
멱등 명령(`ttakkari.cli collection-scope-merge`)이 ledger 와 함께 수행한다.
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0074_collection_single_library"
down_revision = "0073_collection_mission_runs"
branch_labels = None
depends_on = None


def upgrade():
    for table in ("collector_jobs", "collector_sources", "kb_document_access"):
        op.add_column(table, sa.Column("pool", sa.String(16), nullable=False, server_default="personal"))
        op.execute(f"UPDATE {table} SET pool = 'work' WHERE scope = 'work'")
    op.create_table(
        "kb_document_aliases",
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("kb_documents.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("canonical_document_id", sa.Uuid(), sa.ForeignKey("kb_documents.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("relation", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("relation IN ('covered', 'variant')", name="ck_kb_document_aliases_relation"),
        sa.CheckConstraint("document_id <> canonical_document_id", name="ck_kb_document_aliases_distinct"),
    )
    op.create_index("ix_kb_document_aliases_canonical_document_id", "kb_document_aliases", ["canonical_document_id"])
    op.create_table(
        "collection_scope_ledger",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("row_key", sa.String(128), nullable=False),
        sa.Column("action", sa.String(48), nullable=False),
        sa.Column("before", postgresql.JSONB(), nullable=False),
        sa.Column("after", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_collection_scope_ledger_run_id", "collection_scope_ledger", ["run_id"])


def downgrade():
    # 병합이 scope 를 단일 값으로 바꿨다면 pool 이 옛 의미(work 세션 = work scope)를 그대로 담고 있으므로 먼저 복원한다.
    # 병합 전에는 이미 같은 값이라 아무 행도 바뀌지 않는다. 지운 중복 cache/source 행은 ledger 에만 있다.
    op.execute("UPDATE collector_jobs SET scope = 'work' WHERE pool = 'work' AND scope <> 'work'")
    op.execute("UPDATE collector_outbox o SET scope = 'work' FROM collector_jobs j "
               "WHERE o.job_id = j.id AND j.pool = 'work' AND o.scope <> 'work'")
    op.execute("UPDATE collector_items i SET scope = 'work' FROM collector_jobs j "
               "WHERE i.job_id = j.id AND j.pool = 'work' AND i.scope <> 'work'")
    op.execute("UPDATE collector_dedupe_bands b SET scope = 'work' FROM collector_items i "
               "WHERE b.item_id = i.id AND i.scope = 'work' AND b.scope <> 'work'")
    op.execute("UPDATE kb_document_access SET scope = 'work' WHERE pool = 'work' AND scope <> 'work'")
    op.execute("UPDATE collector_sources SET scope = 'work' WHERE pool = 'work' AND scope <> 'work'")
    op.drop_index("ix_collection_scope_ledger_run_id", table_name="collection_scope_ledger")
    op.drop_table("collection_scope_ledger")
    op.drop_index("ix_kb_document_aliases_canonical_document_id", table_name="kb_document_aliases")
    op.drop_table("kb_document_aliases")
    for table in ("kb_document_access", "collector_sources", "collector_jobs"):
        op.drop_column(table, "pool")
