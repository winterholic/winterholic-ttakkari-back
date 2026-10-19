"""게임 활성 빌드와 복구 출처(ADR 0050): 기존 표에 NULL 허용 컬럼 두 개만 추가한다(online-safe). active_build_id 는 순환 참조라 FK 없이 둔다."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0048_game_active_build"
down_revision = "0047_game_studio"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("game_projects", sa.Column("active_build_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("game_builds", sa.Column("restored_from_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_foreign_key("fk_game_builds_restored_from", "game_builds", "game_builds", ["restored_from_id"], ["id"], ondelete="SET NULL")


def downgrade():
    op.drop_constraint("fk_game_builds_restored_from", "game_builds", type_="foreignkey")
    op.drop_column("game_builds", "restored_from_id")
    op.drop_column("game_projects", "active_build_id")
