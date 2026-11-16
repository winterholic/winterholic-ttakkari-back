"""context_sharing: 프로젝트 맥락 공유 초안·게시·키 메타데이터(가산 전용)."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0079_context_sharing"
down_revision = "0078_repo_appraisals"
branch_labels = None
depends_on = None

_TS = sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "ctx_share_projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("entity_id", sa.Uuid(), sa.ForeignKey("ctx_entities.id", ondelete="SET NULL"), nullable=True, unique=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("gateway_project_id", sa.String(36), nullable=True),
        sa.Column("created_at", _TS, nullable=False),
    )
    op.create_table(
        "ctx_share_drafts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("ctx_share_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(8), nullable=False, server_default="owner"),
        sa.Column("scope", sa.String(20), nullable=False, server_default="personal"),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("created_at", _TS, nullable=False),
        sa.Column("updated_at", _TS, nullable=False),
    )
    op.create_index("ix_ctx_share_drafts_status", "ctx_share_drafts", ["status"])
    op.create_index("ix_ctx_share_drafts_project_created", "ctx_share_drafts", ["project_id", "created_at"])
    op.create_table(
        "ctx_share_draft_documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("draft_id", sa.Uuid(), sa.ForeignKey("ctx_share_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("logical_path", sa.String(500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("source_refs", JSONB, nullable=False),
        sa.Column("source_revision", sa.String(128), nullable=False),
        sa.Column("generated_at", _TS, nullable=False),
        sa.Column("verified_at", _TS, nullable=True),
        sa.Column("verified_by", sa.String(16), nullable=True),
        sa.Column("content_origin", sa.String(20), nullable=False, server_default="indexed_chunks"),
        sa.Column("edited", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.UniqueConstraint("draft_id", "logical_path"),
    )
    op.create_index("ix_ctx_share_draft_documents_draft_id", "ctx_share_draft_documents", ["draft_id"])
    op.create_table(
        "ctx_share_publications",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("ctx_share_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("draft_id", sa.Uuid(), sa.ForeignKey("ctx_share_drafts.id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("digest", sa.String(64), nullable=False),
        sa.Column("gateway_snapshot_id", sa.String(64), nullable=False),
        sa.Column("gateway_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("published_at", _TS, nullable=False),
        sa.Column("revoked_at", _TS, nullable=True),
    )
    op.create_index("ix_ctx_share_publications_project_id", "ctx_share_publications", ["project_id"])
    op.create_index("uq_ctx_share_publications_active", "ctx_share_publications", ["project_id"], unique=True,
                    postgresql_where=sa.text("status = 'active'"))
    op.create_table(
        "ctx_share_keys",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("ctx_share_projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("publication_id", sa.Uuid(), sa.ForeignKey("ctx_share_publications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("gateway_key_id", sa.String(64), nullable=False, unique=True),
        sa.Column("label", sa.String(160), nullable=False, server_default=""),
        sa.Column("status", sa.String(12), nullable=False, server_default="active"),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False),
        sa.Column("issued_at", _TS, nullable=False),
        sa.Column("expires_at", _TS, nullable=False),
        sa.Column("revoked_at", _TS, nullable=True),
    )
    op.create_index("ix_ctx_share_keys_project_id", "ctx_share_keys", ["project_id"])


def downgrade():
    op.drop_table("ctx_share_keys")
    op.drop_table("ctx_share_publications")
    op.drop_table("ctx_share_draft_documents")
    op.drop_table("ctx_share_drafts")
    op.drop_table("ctx_share_projects")
