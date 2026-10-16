"""Persist per-call token usage returned by llama.cpp for operational desk routing."""
import sqlalchemy as sa

from alembic import op

revision = "0035_qwen_token_usage"
down_revision = "0034_knowledge_ledger"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "qwen_token_usage",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("runs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("call_index", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("request_id", "call_index", name="uq_qwen_token_usage_request_call"),
    )
    op.create_index("ix_qwen_token_usage_recorded_at", "qwen_token_usage", ["recorded_at"])


def downgrade():
    op.drop_index("ix_qwen_token_usage_recorded_at", table_name="qwen_token_usage")
    op.drop_table("qwen_token_usage")
