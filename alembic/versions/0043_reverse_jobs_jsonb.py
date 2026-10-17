"""Reverse jobs stages/result를 JSONB로 맞춘다(모델은 JSONB, ADR 0040). 0041은 손대지 않는다."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0043_reverse_jobs_jsonb"
down_revision = "0042_artcraft_tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("reverse_jobs", "stages",
                    type_=postgresql.JSONB(astext_type=sa.Text()),
                    postgresql_using="stages::jsonb")
    op.alter_column("reverse_jobs", "result",
                    type_=postgresql.JSONB(astext_type=sa.Text()),
                    postgresql_using="result::jsonb")


def downgrade():
    op.alter_column("reverse_jobs", "result", type_=sa.JSON(),
                    postgresql_using="result::json")
    op.alter_column("reverse_jobs", "stages", type_=sa.JSON(),
                    postgresql_using="stages::json")
