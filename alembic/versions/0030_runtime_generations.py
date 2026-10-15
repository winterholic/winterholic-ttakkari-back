"""Add generation ownership without changing existing Run status values."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

from alembic import op

revision = "0030_runtime_generations"
down_revision = "0029_antigravity_usage_credits"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("runs", sa.Column("request_key", sa.String(120), nullable=True))
    op.create_unique_constraint("uq_runs_session_request", "runs", ["session_id", "request_key"])
    op.create_table("runtime_generations",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("revision", sa.String(40), nullable=False),
        sa.Column("api_url", sa.String(150), nullable=False),
        sa.Column("pid", sa.Integer, nullable=False),
        sa.Column("process_started_at", sa.Float, nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("runtime_control",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("active_generation", pg.UUID(as_uuid=True), sa.ForeignKey("runtime_generations.id")),
        sa.Column("epoch", sa.Integer, nullable=False))
    op.create_table("runtime_leases",
        sa.Column("run_id", pg.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("generation_id", pg.UUID(as_uuid=True), sa.ForeignKey("runtime_generations.id"), nullable=False),
        sa.Column("token", pg.UUID(as_uuid=True), nullable=False),
        sa.Column("session_id", pg.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("lane", sa.String(16), nullable=False),
        sa.Column("account_id", pg.UUID(as_uuid=True), sa.ForeignKey("engine_accounts.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    for column in ("generation_id", "session_id", "lane"):
        op.create_index("ix_runtime_leases_" + column, "runtime_leases", [column])
    op.create_table("runtime_commands",
        sa.Column("id", pg.UUID(as_uuid=True), primary_key=True),
        sa.Column("key", sa.String(120), nullable=False, unique=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("generation_id", pg.UUID(as_uuid=True), sa.ForeignKey("runtime_generations.id")),
        sa.Column("run_id", pg.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="CASCADE")),
        sa.Column("session_id", pg.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="CASCADE")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)))
    op.create_index("ix_runtime_commands_generation_id", "runtime_commands", ["generation_id"])


def downgrade():
    for name in ("runtime_commands", "runtime_leases", "runtime_control", "runtime_generations"):
        op.drop_table(name)
    op.drop_constraint("uq_runs_session_request", "runs", type_="unique")
    op.drop_column("runs", "request_key")
