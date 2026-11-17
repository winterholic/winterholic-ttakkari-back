"""Retry failed daily weather collection and deduplicate its failure alert."""
import sqlalchemy as sa

from alembic import op

revision = "0034_weather_collection_retry"
down_revision = "0033_weather_analysis_engine"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("weather_briefings", sa.Column("collection_attempts", sa.Integer(), server_default="0", nullable=False))
    op.add_column("weather_briefings", sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True))
    op.add_column("weather_briefings", sa.Column("failure_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade():
    op.drop_column("weather_briefings", "failure_notified_at")
    op.drop_column("weather_briefings", "retry_after")
    op.drop_column("weather_briefings", "collection_attempts")
