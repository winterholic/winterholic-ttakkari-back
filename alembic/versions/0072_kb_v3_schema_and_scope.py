"""KB v3 schema extension and scope unification.

Revision ID: 0072_kb_v3_schema_and_scope
Revises: 0071_library_platform_v2_merge
Create Date: 2026-10-25 14:00:00.000000

Adds:
- kb_sources: blob_relpath, original_filename, parser_name, parser_version, source_meta
- kb_passages: section_path, chunk_kind, page_no, slide_no, time_start_ms, time_end_ms, token_estimate, search_text
- kb_document_access: origin_scope, visibility (and dual-write / backfill safe baseline)
- kb_pipeline_jobs and kb_pipeline_stages
- kb_entities, kb_entity_aliases, kb_document_entities, kb_assertions,
  kb_assertion_evidence, kb_relations, kb_entity_canonicals, kb_conflicts
"""

from __future__ import annotations

import os

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0072_kb_v3_schema_and_scope"
down_revision = "0071_library_platform_v2_merge"
branch_labels = None
depends_on = None

_V3_TABLES = (
    "kb_conflicts",
    "kb_entity_canonicals",
    "kb_relations",
    "kb_assertion_evidence",
    "kb_assertions",
    "kb_document_entities",
    "kb_entity_aliases",
    "kb_entities",
    "kb_pipeline_stages",
    "kb_pipeline_jobs",
)


def upgrade() -> None:
    # 1. kb_sources additive columns
    op.add_column("kb_sources", sa.Column("blob_relpath", sa.Text(), nullable=True))
    op.add_column(
        "kb_sources", sa.Column("original_filename", sa.String(500), nullable=True)
    )
    op.add_column(
        "kb_sources",
        sa.Column(
            "parser_name", sa.String(60), server_default="plain_v1", nullable=False
        ),
    )
    op.add_column(
        "kb_sources",
        sa.Column(
            "parser_version", sa.String(60), server_default="1.0", nullable=False
        ),
    )
    op.add_column(
        "kb_sources",
        sa.Column(
            "source_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )

    # 2. kb_passages additive columns & indexes
    op.add_column(
        "kb_passages",
        sa.Column(
            "section_path",
            postgresql.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
    )
    op.add_column(
        "kb_passages",
        sa.Column("chunk_kind", sa.String(24), server_default="prose", nullable=False),
    )
    op.add_column("kb_passages", sa.Column("page_no", sa.Integer(), nullable=True))
    op.add_column("kb_passages", sa.Column("slide_no", sa.Integer(), nullable=True))
    op.add_column(
        "kb_passages", sa.Column("time_start_ms", sa.Integer(), nullable=True)
    )
    op.add_column("kb_passages", sa.Column("time_end_ms", sa.Integer(), nullable=True))
    op.add_column(
        "kb_passages",
        sa.Column("token_estimate", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column(
        "kb_passages",
        sa.Column("search_text", sa.Text(), server_default="", nullable=False),
    )
    op.create_check_constraint(
        "ck_kb_passage_chunk_kind",
        "kb_passages",
        "chunk_kind IN ('prose', 'code', 'table', 'list', 'transcript', 'heading')",
    )
    op.create_index("ix_kb_passages_chunk_kind", "kb_passages", ["chunk_kind"])

    # 3. kb_document_access scope unification columns & constraints
    op.add_column(
        "kb_document_access",
        sa.Column(
            "origin_scope", sa.String(16), server_default="personal", nullable=False
        ),
    )
    op.add_column(
        "kb_document_access",
        sa.Column("visibility", sa.String(16), server_default="global", nullable=False),
    )
    op.create_check_constraint(
        "ck_kb_document_access_origin_scope",
        "kb_document_access",
        "origin_scope IN ('personal', 'work')",
    )
    op.create_check_constraint(
        "ck_kb_document_access_visibility",
        "kb_document_access",
        "visibility IN ('global', 'work_only')",
    )
    op.create_index(
        "ix_kb_document_access_visibility", "kb_document_access", ["visibility"]
    )
    op.create_index(
        "ix_kb_document_access_origin_scope", "kb_document_access", ["origin_scope"]
    )

    # Safe backfill for existing rows: work documents keep work_only visibility
    op.execute(
        "UPDATE kb_document_access "
        "SET origin_scope = scope, "
        "    visibility = CASE WHEN scope = 'work' THEN 'work_only' ELSE 'global' END "
        "WHERE origin_scope IS NOT NULL"
    )

    # 4. Pipeline Tables: kb_pipeline_jobs & kb_pipeline_stages
    op.create_table(
        "kb_pipeline_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), server_default="registered", nullable=False),
        sa.Column(
            "current_stage", sa.String(24), server_default="parsing", nullable=False
        ),
        sa.Column(
            "pipeline_version", sa.String(32), server_default="kb-v3.0", nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="3", nullable=False),
        sa.Column("attempt_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("claimed_by", sa.String(64), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("hold_reason", sa.String(64), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column(
            "metrics",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("revision_id", name="uq_kb_pipeline_job_revision"),
        sa.CheckConstraint(
            "status IN ('registered', 'parsing', 'chunking', 'extracting', 'linking', "
            "'indexing', 'canonicalizing', 'ready', 'degraded', 'held', 'failed')",
            name="ck_kb_pipeline_job_status",
        ),
        sa.CheckConstraint(
            "current_stage IN ('parsing', 'chunking', 'extracting', 'linking', 'indexing', 'canonicalizing')",
            name="ck_kb_pipeline_job_stage",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_kb_pipeline_job_attempts"),
        sa.CheckConstraint("max_attempts > 0", name="ck_kb_pipeline_job_max_attempts"),
    )
    op.create_index(
        "ix_kb_pipeline_jobs_claim", "kb_pipeline_jobs", ["status", "next_run_at"]
    )
    op.create_index(
        "ix_kb_pipeline_jobs_document_id", "kb_pipeline_jobs", ["document_id"]
    )

    op.create_table(
        "kb_pipeline_stages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_pipeline_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("status", sa.String(16), server_default="pending", nullable=False),
        sa.Column(
            "processor_version", sa.String(32), server_default="v1", nullable=False
        ),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column(
            "detail",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.UniqueConstraint("job_id", "stage", name="uq_kb_pipeline_stage"),
        sa.CheckConstraint(
            "stage IN ('parsing', 'chunking', 'extracting', 'linking', 'indexing', 'canonicalizing')",
            name="ck_kb_pipeline_stage_name",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'degraded', 'held', 'failed', 'skipped')",
            name="ck_kb_pipeline_stage_status",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_kb_pipeline_stage_attempts"),
    )
    op.create_index("ix_kb_pipeline_stages_job_id", "kb_pipeline_stages", ["job_id"])

    # 5. Ontology Tables
    # 5.1 kb_entities
    op.create_table(
        "kb_entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id", sa.String(64), server_default="personal", nullable=False
        ),
        sa.Column(
            "origin_scope", sa.String(16), server_default="personal", nullable=False
        ),
        sa.Column("visibility", sa.String(16), server_default="global", nullable=False),
        sa.Column("canonical_name", sa.String(200), nullable=False),
        sa.Column("norm_name", sa.String(200), nullable=False),
        sa.Column("norm_form", sa.String(16), server_default="NFKC", nullable=False),
        sa.Column(
            "entity_kind", sa.String(32), server_default="concept", nullable=False
        ),
        sa.Column("domain", sa.String(32), server_default="general", nullable=False),
        sa.Column("summary", sa.Text(), server_default="", nullable=False),
        sa.Column("mention_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("document_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0.8", nullable=False),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column(
            "merged_into_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "visibility",
            "entity_kind",
            "norm_name",
            name="uq_kb_entities_norm",
        ),
        sa.CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_entities_visibility"
        ),
        sa.CheckConstraint(
            "origin_scope IN ('personal', 'work')", name="ck_kb_entities_origin_scope"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'merged', 'archived')", name="ck_kb_entities_status"
        ),
        sa.CheckConstraint(
            "entity_kind IN ('concept', 'technology', 'system', 'api', 'symbol', 'standard', 'organization', 'person', 'document_topic')",
            name="ck_kb_entities_kind",
        ),
        sa.CheckConstraint(
            "domain IN ('dev', 'infra', 'ai', 'security', 'business', 'science', 'general')",
            name="ck_kb_entities_domain",
        ),
        sa.CheckConstraint(
            "confidence >= 0.0 AND confidence <= 1.0", name="ck_kb_entities_confidence"
        ),
        sa.CheckConstraint("mention_count >= 0", name="ck_kb_entities_mention_count"),
        sa.CheckConstraint("document_count >= 0", name="ck_kb_entities_doc_count"),
    )
    op.create_index("ix_kb_entities_norm_name", "kb_entities", ["norm_name"])
    op.create_index(
        "ix_kb_entities_kind_domain", "kb_entities", ["entity_kind", "domain"]
    )
    op.create_index("ix_kb_entities_visibility", "kb_entities", ["visibility"])
    op.create_index("ix_kb_entities_origin_scope", "kb_entities", ["origin_scope"])

    # 5.2 kb_entity_aliases
    op.create_table(
        "kb_entity_aliases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(200), nullable=False),
        sa.Column("norm_alias", sa.String(200), nullable=False),
        sa.Column("norm_form", sa.String(16), server_default="NFKC", nullable=False),
        sa.Column("lang", sa.String(8), nullable=True),
        sa.Column(
            "alias_kind", sa.String(24), server_default="synonym", nullable=False
        ),
        sa.Column("source", sa.String(24), server_default="extracted", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("entity_id", "norm_alias", name="uq_kb_entity_alias"),
        sa.CheckConstraint(
            "alias_kind IN ('canonical', 'synonym', 'abbrev', 'translation')",
            name="ck_kb_entity_alias_kind",
        ),
        sa.CheckConstraint(
            "source IN ('seed', 'extracted', 'user_verified')",
            name="ck_kb_entity_alias_source",
        ),
    )
    op.create_index("ix_kb_entity_aliases_norm", "kb_entity_aliases", ["norm_alias"])
    op.create_index(
        "ix_kb_entity_aliases_entity_id", "kb_entity_aliases", ["entity_id"]
    )

    # 5.3 kb_document_entities
    op.create_table(
        "kb_document_entities",
        sa.Column(
            "passage_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_passages.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_evidence.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("role", sa.String(24), server_default="mentioned", nullable=False),
        sa.Column("salience", sa.Float(), server_default="0.5", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('primary_topic', 'defined', 'mentioned')",
            name="ck_kb_doc_entities_role",
        ),
        sa.CheckConstraint(
            "salience >= 0.0 AND salience <= 1.0", name="ck_kb_doc_entities_salience"
        ),
    )
    op.create_index(
        "ix_kb_doc_entities_entity",
        "kb_document_entities",
        ["entity_id", "document_id"],
    )
    op.create_index(
        "ix_kb_doc_entities_doc", "kb_document_entities", ["document_id", "salience"]
    )

    # 5.4 kb_assertions
    op.create_table(
        "kb_assertions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_documents.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "origin_scope", sa.String(16), server_default="personal", nullable=False
        ),
        sa.Column("visibility", sa.String(16), server_default="global", nullable=False),
        sa.Column(
            "subject_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("predicate", sa.String(64), nullable=False),
        sa.Column(
            "object_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("object_value", sa.Text(), nullable=False),
        sa.Column("norm_object_value", sa.String(240), nullable=False),
        sa.Column("modality", sa.String(16), server_default="fact", nullable=False),
        sa.Column("polarity", sa.String(16), server_default="positive", nullable=False),
        sa.Column(
            "applies_condition",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("trust_score", sa.Float(), server_default="0.7", nullable=False),
        sa.Column(
            "freshness_ts",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column(
            "is_canonical",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("extractor_tier", sa.String(8), server_default="L0", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_assertions_visibility"
        ),
        sa.CheckConstraint(
            "origin_scope IN ('personal', 'work')", name="ck_kb_assertions_origin_scope"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'disputed', 'rejected')",
            name="ck_kb_assertions_status",
        ),
        sa.CheckConstraint(
            "modality IN ('fact', 'recommendation', 'requirement', 'deprecated')",
            name="ck_kb_assertions_modality",
        ),
        sa.CheckConstraint(
            "polarity IN ('positive', 'negative')", name="ck_kb_assertions_polarity"
        ),
        sa.CheckConstraint(
            "trust_score >= 0.0 AND trust_score <= 1.0", name="ck_kb_assertions_trust"
        ),
        sa.CheckConstraint(
            "extractor_tier IN ('L0', 'L1', 'L2', 'human')",
            name="ck_kb_assertions_extractor",
        ),
    )
    op.create_index(
        "ix_kb_assertions_subject_pred",
        "kb_assertions",
        ["subject_entity_id", "predicate", "status"],
    )
    op.create_index(
        "ix_kb_assertions_object_entity", "kb_assertions", ["object_entity_id"]
    )
    op.create_index("ix_kb_assertions_revision", "kb_assertions", ["revision_id"])
    op.create_index("ix_kb_assertions_visibility", "kb_assertions", ["visibility"])

    # 5.5 kb_assertion_evidence
    op.create_table(
        "kb_assertion_evidence",
        sa.Column(
            "assertion_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_assertions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_evidence.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("relation", sa.String(16), server_default="supports", nullable=False),
        sa.CheckConstraint(
            "relation IN ('supports', 'refutes', 'qualifies')",
            name="ck_kb_assertion_evidence_rel",
        ),
    )

    # 5.6 kb_relations
    op.create_table(
        "kb_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "origin_scope", sa.String(16), server_default="personal", nullable=False
        ),
        sa.Column("visibility", sa.String(16), server_default="global", nullable=False),
        sa.Column(
            "source_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("relation_type", sa.String(48), nullable=False),
        sa.Column("weight", sa.Float(), server_default="1.0", nullable=False),
        sa.Column("evidence_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "representative_evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_evidence.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.String(16), server_default="active", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "source_entity_id",
            "target_entity_id",
            "relation_type",
            name="uq_kb_relation_edge",
        ),
        sa.CheckConstraint("evidence_count >= 0", name="ck_kb_relation_evidence_count"),
        sa.CheckConstraint(
            "(status != 'active') OR (evidence_count >= 1)",
            name="ck_kb_relation_active_needs_evidence",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'active', 'disputed')",
            name="ck_kb_relation_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_relation_visibility"
        ),
        sa.CheckConstraint(
            "origin_scope IN ('personal', 'work')", name="ck_kb_relation_origin_scope"
        ),
        sa.CheckConstraint(
            "relation_type IN ('part_of', 'manages', 'depends_on', 'uses', 'implements', 'alternative_to', 'related_to')",
            name="ck_kb_relation_type",
        ),
    )
    op.create_index(
        "ix_kb_relations_source", "kb_relations", ["source_entity_id", "weight"]
    )
    op.create_index(
        "ix_kb_relations_target", "kb_relations", ["target_entity_id", "weight"]
    )
    op.create_index("ix_kb_relations_visibility", "kb_relations", ["visibility"])

    # 5.7 kb_entity_canonicals
    op.create_table(
        "kb_entity_canonicals",
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("visibility", sa.String(16), server_default="global", nullable=False),
        sa.Column("canonical_definition", sa.Text(), nullable=False),
        sa.Column(
            "key_facts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "source_document_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "supporting_evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "has_open_conflict",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "visibility IN ('global', 'work_only')",
            name="ck_kb_entity_canonicals_visibility",
        ),
        sa.CheckConstraint("version_no > 0", name="ck_kb_entity_canonicals_version"),
    )
    op.create_index(
        "ix_kb_entity_canonicals_visibility", "kb_entity_canonicals", ["visibility"]
    )

    # 5.8 kb_conflicts
    op.create_table(
        "kb_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("visibility", sa.String(16), server_default="global", nullable=False),
        sa.Column("conflict_kind", sa.String(24), nullable=False),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "candidate_entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_entities.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("predicate", sa.String(64), nullable=True),
        sa.Column(
            "assertion_a_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_assertions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "assertion_b_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("kb_assertions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.String(32), server_default="open", nullable=False),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("resolved_by", sa.String(32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "conflict_kind IN ('value_contradiction', 'polarity_contradiction', 'version_divergence', 'ambiguous_entity')",
            name="ck_kb_conflicts_kind",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved_a', 'resolved_b', 'resolved_both_valid', 'merged', 'kept_separate')",
            name="ck_kb_conflicts_status",
        ),
        sa.CheckConstraint(
            "visibility IN ('global', 'work_only')", name="ck_kb_conflicts_visibility"
        ),
    )
    op.create_index(
        "ix_kb_conflicts_status",
        "kb_conflicts",
        ["status", "conflict_kind", "created_at"],
    )
    op.create_index("ix_kb_conflicts_entity_id", "kb_conflicts", ["entity_id"])


def downgrade() -> None:
    # Guard against accidental downgrade if KB v3 data exists
    if (
        not op.get_context().as_sql
        and os.environ.get("ALLOW_KB_DESTRUCTIVE_DOWNGRADE") != "1"
    ):
        conn = op.get_bind()
        # Check if any v3 table has rows
        for tbl in ("kb_entities", "kb_pipeline_jobs"):
            has_table = conn.execute(
                sa.text(
                    "SELECT 1 FROM information_schema.tables WHERE table_name = :tbl"
                ),
                {"tbl": tbl},
            ).scalar()
            if has_table:
                count = conn.execute(sa.text(f"SELECT count(*) FROM {tbl}")).scalar()
                if count and count > 0:
                    raise RuntimeError(
                        f"Refusing to downgrade populated KB v3 schema ({tbl} has {count} rows). "
                        "Set ALLOW_KB_DESTRUCTIVE_DOWNGRADE=1 to override."
                    )

    # 1. Drop v3 tables in reverse dependency order
    for table in _V3_TABLES:
        op.drop_table(table)

    # 2. Revert kb_document_access columns & constraints
    op.drop_index("ix_kb_document_access_origin_scope", table_name="kb_document_access")
    op.drop_index("ix_kb_document_access_visibility", table_name="kb_document_access")
    op.drop_constraint(
        "ck_kb_document_access_visibility", "kb_document_access", type_="check"
    )
    op.drop_constraint(
        "ck_kb_document_access_origin_scope", "kb_document_access", type_="check"
    )
    op.drop_column("kb_document_access", "visibility")
    op.drop_column("kb_document_access", "origin_scope")

    # 3. Revert kb_passages columns & constraints
    op.drop_index("ix_kb_passages_chunk_kind", table_name="kb_passages")
    op.drop_constraint("ck_kb_passage_chunk_kind", "kb_passages", type_="check")
    op.drop_column("kb_passages", "search_text")
    op.drop_column("kb_passages", "token_estimate")
    op.drop_column("kb_passages", "time_end_ms")
    op.drop_column("kb_passages", "time_start_ms")
    op.drop_column("kb_passages", "slide_no")
    op.drop_column("kb_passages", "page_no")
    op.drop_column("kb_passages", "chunk_kind")
    op.drop_column("kb_passages", "section_path")

    # 4. Revert kb_sources columns
    op.drop_column("kb_sources", "source_meta")
    op.drop_column("kb_sources", "parser_version")
    op.drop_column("kb_sources", "parser_name")
    op.drop_column("kb_sources", "original_filename")
    op.drop_column("kb_sources", "blob_relpath")
