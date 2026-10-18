"""자연어 역분석 작업의 작업공간과 구현·검증 세션 연결. 이전 기록은 유지한다."""
import sqlalchemy as sa

from alembic import op

revision = "0044_reverse_reconstruction"
down_revision = "0043_reverse_jobs_jsonb"
branch_labels = None
depends_on = None


def upgrade():
    for column, table in (("workspace_id", "workspaces"), ("session_id", "sessions"), ("verifier_session_id", "sessions")):
        op.add_column("reverse_jobs", sa.Column(column, sa.Uuid(), nullable=True))
        op.create_foreign_key(f"fk_reverse_{column}", "reverse_jobs", table, [column], ["id"], ondelete="SET NULL")
    op.add_column("reverse_jobs", sa.Column("output_path", sa.Text(), nullable=True))
    op.create_index("ix_reverse_jobs_session_id", "reverse_jobs", ["session_id"])


def downgrade():
    op.drop_index("ix_reverse_jobs_session_id", table_name="reverse_jobs")
    op.drop_column("reverse_jobs", "output_path")
    for column in ("verifier_session_id", "session_id", "workspace_id"):
        op.drop_constraint(f"fk_reverse_{column}", "reverse_jobs", type_="foreignkey")
        op.drop_column("reverse_jobs", column)
