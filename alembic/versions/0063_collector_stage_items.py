"""수집 플랫폼 S2-03/04/06: 출처 정책, 호스트 상태(robots·cooldown), fetch 캐시, 수집 항목, 중복 band 표.

신규 표 5개만 추가한다(기존 표 변경·재작성 없음, online-safe). 원본 bytes 는 DB 가 아니라 content-addressed 파일 저장소에 두고
collector_items 는 해시와 handle 만 가진다.
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0063_collector_stage_items"
down_revision = "0062_delivery_change_sets"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())
SCOPES = "scope IN ('personal', 'work', 'shared')"


def upgrade():
    op.create_table(
        "collector_source_policies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(256), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("spec", JSONB, nullable=False),
        sa.Column("spec_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(SCOPES, name="ck_collector_source_policies_scope"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "scope", "source_key", "version", name="uq_collector_source_policies_version"),
    )
    op.create_table(
        "collector_host_state",
        sa.Column("origin", sa.String(300), nullable=False),
        sa.Column("robots_status", sa.String(16), nullable=True),
        sa.Column("robots_body", sa.Text(), nullable=True),
        sa.Column("robots_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("robots_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cooldown_reason", sa.String(64), nullable=True),
        sa.Column("next_allowed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("origin"),
    )
    op.create_table(
        "collector_fetch_cache",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(256), nullable=False),
        sa.Column("url_key", sa.String(64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("etag", sa.Text(), nullable=True),
        sa.Column("last_modified", sa.Text(), nullable=True),
        sa.Column("last_raw_sha256", sa.String(64), nullable=True),
        sa.Column("last_item_id", sa.Uuid(), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "scope", "source_key", "url_key", name="uq_collector_fetch_cache_url"),
    )
    op.create_table(
        "collector_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(24), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_key", sa.String(64), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("discovered_from", sa.Uuid(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("raw_sha256", sa.String(64), nullable=True),
        sa.Column("raw_size", sa.BigInteger(), nullable=True),
        sa.Column("raw_handle", sa.String(100), nullable=True),
        sa.Column("media_type", sa.String(128), nullable=True),
        sa.Column("text_sha256", sa.String(64), nullable=True),
        sa.Column("text_chars", sa.Integer(), nullable=True),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column("language_confidence", sa.Float(), nullable=True),
        sa.Column("minhash", sa.LargeBinary(), nullable=True),
        sa.Column("exact_of", sa.Uuid(), nullable=True),
        sa.Column("cluster_id", sa.String(64), nullable=True),
        sa.Column("profile", sa.String(32), nullable=True),
        sa.Column("decision_state", sa.String(16), nullable=True),
        sa.Column("reason_codes", JSONB, nullable=False),
        sa.Column("errors", JSONB, nullable=False),
        sa.Column("signals", JSONB, nullable=False),
        sa.Column("envelope_id", sa.Uuid(), nullable=True),
        sa.Column("envelope", JSONB, nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("parser_version", sa.String(64), nullable=True),
        sa.Column("normalizer_version", sa.String(32), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(SCOPES, name="ck_collector_items_scope"),
        sa.CheckConstraint("kind IN ('document', 'discovery')", name="ck_collector_items_kind"),
        sa.CheckConstraint(
            "status IN ('accepted', 'held', 'blocked', 'failed', 'not_modified', 'discovered')",
            name="ck_collector_items_status",
        ),
        sa.ForeignKeyConstraint(["job_id"], ["collector_jobs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("job_id", "url_key", name="uq_collector_items_job_url"),
    )
    op.create_index("ix_collector_items_text", "collector_items", ["tenant_id", "scope", "text_sha256"])
    op.create_index("ix_collector_items_url", "collector_items", ["tenant_id", "scope", "source_key", "url_key"])
    op.create_index("ix_collector_items_job", "collector_items", ["job_id"])
    op.create_table(
        "collector_dedupe_bands",
        sa.Column("item_id", sa.Uuid(), nullable=False),
        sa.Column("band", sa.Integer(), nullable=False),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("band_hash", sa.String(16), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["collector_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "band"),
    )
    op.create_index(
        "ix_collector_dedupe_bands_lookup", "collector_dedupe_bands", ["tenant_id", "scope", "band", "band_hash"]
    )


def downgrade():
    op.drop_table("collector_dedupe_bands")
    op.drop_table("collector_items")
    op.drop_table("collector_fetch_cache")
    op.drop_table("collector_host_state")
    op.drop_table("collector_source_policies")
