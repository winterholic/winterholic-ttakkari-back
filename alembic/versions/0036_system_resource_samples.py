"""Persist bounded GPU and CPU resource history."""
import sqlalchemy as sa

from alembic import op

revision = "0036_system_resource_samples"
down_revision = "0035_qwen_token_usage"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "system_resource_samples",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sampled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("gpu_percent", sa.Float(), nullable=True),
        sa.Column("gpu_memory_used_bytes", sa.BigInteger(), nullable=True),
        sa.Column("gpu_status", sa.String(length=24), nullable=False),
        sa.Column("collection_status", sa.String(length=16), nullable=False),
        sa.Column("cpu_percent", sa.Float(), nullable=True),
        sa.Column("memory_used_bytes", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_system_resource_samples_sampled_at", "system_resource_samples", ["sampled_at"])


def downgrade():
    op.drop_index("ix_system_resource_samples_sampled_at", table_name="system_resource_samples")
    op.drop_table("system_resource_samples")
