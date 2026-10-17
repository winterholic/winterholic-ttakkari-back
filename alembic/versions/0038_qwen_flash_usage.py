"""Persist standalone Flash Next token usage windows and samples."""
import sqlalchemy as sa

from alembic import op

revision = "0038_qwen_flash_usage"
down_revision = "0037_worker_routing"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "qwen_flash_usage",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cached_input_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("raw_prompt_tokens", sa.BigInteger(), nullable=True),
        sa.Column("raw_cached_tokens", sa.BigInteger(), nullable=True),
        sa.Column("raw_output_tokens", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_qwen_flash_usage_recorded_at", "qwen_flash_usage", ["recorded_at"])


def downgrade():
    op.drop_index("ix_qwen_flash_usage_recorded_at", table_name="qwen_flash_usage")
    op.drop_table("qwen_flash_usage")
