"""Run identity for periodic mission refresh so page budgets apply per run. Additive."""
import sqlalchemy as sa

from alembic import op

revision = "0073_collection_mission_runs"
down_revision = "0072_collection_missions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collection_missions", sa.Column("current_run_seq", sa.Integer(), nullable=False, server_default="1"))
    op.add_column("collection_mission_jobs", sa.Column("run_seq", sa.Integer(), nullable=False, server_default="1"))
    op.create_index("ix_collection_mission_job_run", "collection_mission_jobs", ["mission_id", "run_seq"])
    op.create_table(
        "collection_mission_runs",
        sa.Column("mission_id", sa.Uuid(), sa.ForeignKey("collection_missions.id", ondelete="RESTRICT"), primary_key=True),
        sa.Column("run_seq", sa.Integer(), primary_key=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("page_budget", sa.Integer(), nullable=False),
        sa.Column("extra_pages", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Existing missions have exactly one run (1) whose budget is the budget they were created or extended with.
    op.execute(
        "INSERT INTO collection_mission_runs (mission_id, run_seq, trigger, page_budget, extra_pages, started_at) "
        "SELECT id, 1, 'initial', GREATEST(1, COALESCE((plan->'budget'->>'pages')::int, 1)), 0, created_at "
        "FROM collection_missions"
    )


def downgrade():
    op.drop_table("collection_mission_runs")
    op.drop_index("ix_collection_mission_job_run", table_name="collection_mission_jobs")
    op.drop_column("collection_mission_jobs", "run_seq")
    op.drop_column("collection_missions", "current_run_seq")
