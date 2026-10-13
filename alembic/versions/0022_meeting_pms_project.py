"""회의록을 PMS 프로젝트에 연결한다. 기존 맥락 프로젝트 연결은 보존한다."""
import sqlalchemy as sa

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("meetings", sa.Column("pms_project_id", sa.Uuid(), nullable=True))
    op.create_foreign_key("fk_meetings_pms_project", "meetings", "pms_projects", ["pms_project_id"], ["id"], ondelete="SET NULL")
    op.create_index("ix_meetings_pms_project_id", "meetings", ["pms_project_id"])


def downgrade() -> None:
    op.drop_index("ix_meetings_pms_project_id", table_name="meetings")
    op.drop_constraint("fk_meetings_pms_project", "meetings", type_="foreignkey")
    op.drop_column("meetings", "pms_project_id")
