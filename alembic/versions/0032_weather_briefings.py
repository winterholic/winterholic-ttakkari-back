"""Store daily weather input, analysis status and generated briefings."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0032_weather_briefings"
down_revision = "0031_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "weather_briefings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("request_source", sa.String(12), nullable=False),
        sa.Column("raw_forecasts", postgresql.JSONB(), nullable=True),
        sa.Column("forecast", postgresql.JSONB(), nullable=True),
        sa.Column("source_errors", postgresql.JSONB(), nullable=True),
        sa.Column("briefing", postgresql.JSONB(), nullable=True),
        sa.Column("analysis_status", sa.String(20), nullable=True),
        sa.Column("failure", sa.Text(), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sessions.id", ondelete="SET NULL"), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("push_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_weather_briefings_day", "weather_briefings", ["day"])
    op.add_column("notifications", sa.Column("dedupe_key", sa.String(120), nullable=True))
    op.create_index("uq_notifications_dedupe_key", "notifications", ["dedupe_key"], unique=True,
                    postgresql_where=sa.text("dedupe_key IS NOT NULL"))


def downgrade():
    op.drop_index("uq_notifications_dedupe_key", table_name="notifications")
    op.drop_column("notifications", "dedupe_key")
    op.drop_index("ix_weather_briefings_day", table_name="weather_briefings")
    op.drop_table("weather_briefings")
