"""게임 스튜디오 최소 연결·빌드 기록(ADR 0046): game_projects, game_builds, game_feedbacks. 신규 표만 추가한다(online-safe)."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0047_game_studio"
down_revision = "0046_notebooklm"
branch_labels = None
depends_on = None


def _tz():
    return sa.DateTime(timezone=True)


def _uuid_fk(name, target, *, ondelete, nullable=True, **kw):
    return sa.Column(name, postgresql.UUID(as_uuid=True), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable, **kw)


def upgrade():
    op.create_table(
        "game_projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("engine_id", sa.String(40), nullable=False),
        _uuid_fk("workspace_id", "workspaces.id", ondelete="RESTRICT", nullable=False, unique=True),
        _uuid_fk("pms_project_id", "pms_projects.id", ondelete="SET NULL"),
        sa.Column("locked_decisions_sha256", sa.String(64), nullable=True),
        sa.Column("locked_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("archived_at", _tz(), nullable=True),
    )
    op.create_index("ix_game_projects_pms_project_id", "game_projects", ["pms_project_id"])
    op.create_table(
        "game_builds",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("game_id", "game_projects.id", ondelete="CASCADE", nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="building"),
        sa.Column("source_commit", sa.String(40), nullable=True),
        sa.Column("source_dirty", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("manifest_sha256", sa.String(64), nullable=True),
        sa.Column("lock_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("bundle_rel_path", sa.Text(), nullable=True),
        sa.Column("bundle_sha256", sa.String(64), nullable=True),
        sa.Column("bundle_bytes", sa.BigInteger(), nullable=True),
        sa.Column("bundle_files", sa.Integer(), nullable=True),
        _uuid_fk("run_id", "runs.id", ondelete="SET NULL"),
        _uuid_fk("artifact_id", "artifacts.id", ondelete="SET NULL"),
        sa.Column("log_tail", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", _tz(), nullable=False),
        sa.Column("finished_at", _tz(), nullable=True),
        sa.UniqueConstraint("game_id", "seq", name="uq_game_builds_game_seq"),
    )
    op.create_index("ix_game_builds_game_id", "game_builds", ["game_id"])
    op.create_index("ix_game_builds_run_id", "game_builds", ["run_id"])
    op.create_table(
        "game_feedbacks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _uuid_fk("game_id", "game_projects.id", ondelete="CASCADE", nullable=False),
        _uuid_fk("build_id", "game_builds.id", ondelete="SET NULL"),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        _uuid_fk("screenshot_artifact_id", "artifacts.id", ondelete="SET NULL"),
        _uuid_fk("run_id", "runs.id", ondelete="SET NULL"),
        sa.Column("created_at", _tz(), nullable=False),
    )
    op.create_index("ix_game_feedbacks_game_id", "game_feedbacks", ["game_id"])


def downgrade():
    op.drop_index("ix_game_feedbacks_game_id", table_name="game_feedbacks")
    op.drop_table("game_feedbacks")
    op.drop_index("ix_game_builds_run_id", table_name="game_builds")
    op.drop_index("ix_game_builds_game_id", table_name="game_builds")
    op.drop_table("game_builds")
    op.drop_index("ix_game_projects_pms_project_id", table_name="game_projects")
    op.drop_table("game_projects")
