"""수집 플랫폼 1단계: 영속 수집 작업(collector_jobs) 및 전송 아웃박스(collector_outbox) 추가.

- collector_jobs: tenant/scope/source_key/request_key 복합 고유 제약조건을 통한 멱등 등록 보장
- collector_outbox: tenant/sink/scope/envelope_id 고유성 및 원자적 outbox 상태
- 신규 테이블만 생성하는 online-safe 마이그레이션
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0059_collector_intake"
down_revision = "0058_kb_immutability_truncate"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def upgrade():
    op.create_table(
        "collector_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("source_key", sa.String(256), nullable=False),
        sa.Column("request_key", sa.String(256), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("envelope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(32), nullable=False, server_default="intake"),
        sa.Column("envelope", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("owner_generation", sa.String(64), nullable=True),
        sa.Column("attempt_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("not_before", _tz(), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("updated_at", _tz(), nullable=False),
        sa.CheckConstraint(
            "scope IN ('personal', 'work', 'shared')",
            name="ck_collector_jobs_scope",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'partial', 'failed', 'cancelled', 'retry_wait', 'budget_wait', 'policy_blocked', 'quarantined')",
            name="ck_collector_jobs_status",
        ),
        sa.UniqueConstraint(
            "tenant_id", "scope", "source_key", "request_key",
            name="uq_collector_jobs_idempotency",
        ),
    )
    op.create_index("ix_collector_jobs_status", "collector_jobs", ["status"])
    op.create_index("ix_collector_jobs_tenant_scope", "collector_jobs", ["tenant_id", "scope"])
    op.create_index("ix_collector_jobs_claim", "collector_jobs", ["status", "not_before", "created_at"])

    op.create_table(
        "collector_outbox",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "job_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("collector_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(32), nullable=False),
        sa.Column("sink", sa.String(64), nullable=False, server_default="default"),
        sa.Column("envelope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("delivered_at", _tz(), nullable=True),
        sa.CheckConstraint(
            "scope IN ('personal', 'work', 'shared')",
            name="ck_collector_outbox_scope",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivered', 'failed', 'delivery_unknown')",
            name="ck_collector_outbox_status",
        ),
        sa.UniqueConstraint(
            "tenant_id", "sink", "scope", "envelope_id",
            name="uq_collector_outbox_tenant_sink_envelope",
        ),
    )
    op.create_index("ix_collector_outbox_status", "collector_outbox", ["status"])
    op.create_index("ix_collector_outbox_job_id", "collector_outbox", ["job_id"])


def downgrade():
    op.drop_index("ix_collector_outbox_job_id", table_name="collector_outbox")
    op.drop_index("ix_collector_outbox_status", table_name="collector_outbox")
    op.drop_table("collector_outbox")

    op.drop_index("ix_collector_jobs_claim", table_name="collector_jobs")
    op.drop_index("ix_collector_jobs_tenant_scope", table_name="collector_jobs")
    op.drop_index("ix_collector_jobs_status", table_name="collector_jobs")
    op.drop_table("collector_jobs")
