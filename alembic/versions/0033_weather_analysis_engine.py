"""Record which engine and model produced each weather briefing."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0033_weather_analysis_engine"
down_revision = "0032_weather_briefings"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("weather_briefings", sa.Column("analysis_engine", sa.String(20), nullable=True))
    op.add_column("weather_briefings", sa.Column("analysis_model", sa.String(100), nullable=True))
    op.add_column("weather_briefings", sa.Column("analysis_route", postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column("weather_briefings", "analysis_route")
    op.drop_column("weather_briefings", "analysis_model")
    op.drop_column("weather_briefings", "analysis_engine")
