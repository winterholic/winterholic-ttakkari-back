"""repo_appraisals: 저장소 가치 평가 결과 저장(ADR 0073)."""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0078_repo_appraisals"
down_revision = "0077_artcraft_account_pin"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "repo_appraisals",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("owner", sa.String(39), nullable=False),
        sa.Column("repo", sa.String(100), nullable=False),
        sa.Column("head_sha", sa.String(40), nullable=True),
        sa.Column("mode", sa.String(8), nullable=False, server_default="quick"),
        sa.Column("rubric_version", sa.String(8), nullable=False, server_default="1"),
        sa.Column("repo_type", sa.String(16), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("weighted_score", sa.Float(), nullable=True),
        sa.Column("grade", sa.String(4), nullable=True),
        sa.Column("llm_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("llm_model", sa.String(64), nullable=True),
        sa.Column("evidence_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_repo_appraisals_owner_repo_created", "repo_appraisals",
                    ["owner", "repo", "created_at"])
    op.create_index("ix_repo_appraisals_status", "repo_appraisals", ["status"])


def downgrade():
    op.drop_index("ix_repo_appraisals_status", table_name="repo_appraisals")
    op.drop_index("ix_repo_appraisals_owner_repo_created", table_name="repo_appraisals")
    op.drop_table("repo_appraisals")
