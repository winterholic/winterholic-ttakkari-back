"""assistant context: ontology + RAG (pgvector, pg_trgm)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11
"""
from collections.abc import Sequence

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = '0006'
down_revision: str | None = '0005'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DIM = 1024
J = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    op.create_table('ctx_entities',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('type', sa.String(30), nullable=False),
        sa.Column('name', sa.String(200), nullable=False),
        sa.Column('aliases', J, nullable=False),
        sa.Column('summary', sa.Text()),
        sa.Column('attributes', J, nullable=False),
        sa.Column('pinned', sa.Boolean(), nullable=False),
        sa.Column('embedding', Vector(DIM)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('type', 'name'))
    op.create_index('ix_ctx_entities_type', 'ctx_entities', ['type'])
    op.create_table('ctx_relations',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('subject_id', sa.Uuid(), sa.ForeignKey('ctx_entities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('predicate', sa.String(60), nullable=False),
        sa.Column('object_id', sa.Uuid(), sa.ForeignKey('ctx_entities.id', ondelete='CASCADE'), nullable=False),
        sa.Column('note', sa.Text()),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('subject_id', 'predicate', 'object_id'))
    op.create_index('ix_ctx_relations_subject_id', 'ctx_relations', ['subject_id'])
    op.create_index('ix_ctx_relations_object_id', 'ctx_relations', ['object_id'])
    op.create_table('ctx_memories',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('entity_ids', J, nullable=False),
        sa.Column('pinned', sa.Boolean(), nullable=False),
        sa.Column('source', sa.String(20), nullable=False),
        sa.Column('source_ref', sa.Text()),
        sa.Column('embedding', Vector(DIM)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('archived_at', sa.DateTime(timezone=True)))
    op.create_index('ix_ctx_memories_kind', 'ctx_memories', ['kind'])
    op.create_index('ix_ctx_memories_pinned', 'ctx_memories', ['pinned'])
    op.create_table('ctx_sources',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('path', sa.Text(), nullable=False, unique=True),
        sa.Column('pattern', sa.String(100), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('last_indexed_at', sa.DateTime(timezone=True)),
        sa.Column('last_error', sa.Text()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('ctx_documents',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('source_id', sa.Uuid(), sa.ForeignKey('ctx_sources.id', ondelete='CASCADE'), nullable=False),
        sa.Column('path', sa.Text(), nullable=False, unique=True),
        sa.Column('title', sa.String(500), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('chunk_count', sa.Integer(), nullable=False),
        sa.Column('indexed_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_ctx_documents_source_id', 'ctx_documents', ['source_id'])
    op.create_table('ctx_chunks',
        sa.Column('id', sa.Uuid(), primary_key=True),
        sa.Column('document_id', sa.Uuid(), sa.ForeignKey('ctx_documents.id', ondelete='CASCADE'), nullable=False),
        sa.Column('ord', sa.Integer(), nullable=False),
        sa.Column('heading', sa.Text()),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('embedding', Vector(DIM)))
    op.create_index('ix_ctx_chunks_document_id', 'ctx_chunks', ['document_id'])
    # 벡터: HNSW(코사인). 글자: trigram GIN(한국어는 형태소 분석기가 없어 FTS 대신 trigram).
    for t in ('ctx_chunks', 'ctx_memories', 'ctx_entities'):
        op.execute(f"CREATE INDEX ix_{t}_embedding ON {t} USING hnsw (embedding vector_cosine_ops)")
    op.execute("CREATE INDEX ix_ctx_chunks_trgm ON ctx_chunks USING gin (content gin_trgm_ops)")
    op.execute("CREATE INDEX ix_ctx_memories_trgm ON ctx_memories USING gin (content gin_trgm_ops)")
    op.execute("CREATE INDEX ix_ctx_entities_trgm ON ctx_entities USING gin (name gin_trgm_ops)")


def downgrade() -> None:
    for t in ('ctx_chunks', 'ctx_documents', 'ctx_sources', 'ctx_memories', 'ctx_relations', 'ctx_entities'):
        op.drop_table(t)
