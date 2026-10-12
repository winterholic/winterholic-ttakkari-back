"""Model-free context library: scopes, project sources and lexical indexes."""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("ctx_entities", "ctx_memories", "ctx_sources"):
        op.add_column(table, sa.Column("scope", sa.String(20), nullable=False, server_default="personal"))
        op.create_index(f"ix_{table}_scope", table, ["scope"])
    op.add_column("ctx_sources", sa.Column("category", sa.String(30), nullable=False, server_default="knowledge"))
    op.add_column("ctx_sources", sa.Column("entity_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("ctx_sources_entity_id_fkey", "ctx_sources", "ctx_entities", ["entity_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_ctx_sources_entity_id", "ctx_sources", ["entity_id"])
    for table, column in (("ctx_memories", "content"), ("ctx_chunks", "content"), ("ctx_entities", "name")):
        op.execute(sa.text(f"CREATE INDEX ix_{table}_lexical ON {table} USING gin (to_tsvector('simple', {column}))"))


def downgrade() -> None:
    for table in ("ctx_memories", "ctx_chunks", "ctx_entities"):
        op.drop_index(f"ix_{table}_lexical", table)
    op.drop_index("ix_ctx_sources_entity_id", "ctx_sources")
    op.drop_constraint("ctx_sources_entity_id_fkey", "ctx_sources", type_="foreignkey")
    op.drop_column("ctx_sources", "entity_id")
    op.drop_column("ctx_sources", "category")
    for table in ("ctx_entities", "ctx_memories", "ctx_sources"):
        op.drop_index(f"ix_{table}_scope", table)
        op.drop_column(table, "scope")
