"""내 보관함 분류 체계 개편: 보관 의도(area)와 출처(origin) 분리, 카테고리(category) 추가.

Revision ID: 0068_library_intent
Revises: 0067_collector_outbox_retry
Create Date: 2026-10-25
"""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0068_library_intent"
down_revision = "0067_collector_outbox_retry"
branch_labels = None
depends_on = None

_DOCUMENT_COND = """(
    lower(filename) LIKE '%.md'
    OR lower(filename) LIKE '%.markdown'
    OR lower(filename) LIKE '%.txt'
    OR lower(filename) LIKE '%.pdf'
    OR lower(filename) LIKE '%.docx'
    OR lower(filename) LIKE '%.doc'
    OR lower(filename) LIKE '%.rtf'
    OR lower(filename) LIKE '%.odt'
    OR lower(filename) LIKE '%.pages'
    OR lower(filename) LIKE '%.hwp'
)"""

_SPREADSHEET_COND = """(
    lower(filename) LIKE '%.xlsx'
    OR lower(filename) LIKE '%.xls'
    OR lower(filename) LIKE '%.csv'
    OR lower(filename) LIKE '%.tsv'
    OR lower(filename) LIKE '%.ods'
    OR lower(filename) LIKE '%.parquet'
    OR lower(filename) LIKE '%.numbers'
    OR kind = 'sheet'
)"""

_PRESENTATION_COND = """(
    lower(filename) LIKE '%.pptx'
    OR lower(filename) LIKE '%.ppt'
    OR lower(filename) LIKE '%.key'
    OR lower(filename) LIKE '%.odp'
    OR kind = 'pptx'
)"""

_IMAGE_COND = """(
    mime_type LIKE 'image/%'
    OR kind = 'image'
)"""

_VIDEO_COND = """(
    mime_type LIKE 'video/%'
    OR kind = 'video'
    OR lower(filename) LIKE '%.mp4'
    OR lower(filename) LIKE '%.mov'
    OR lower(filename) LIKE '%.mkv'
    OR lower(filename) LIKE '%.webm'
    OR lower(filename) LIKE '%.avi'
    OR lower(filename) LIKE '%.wmv'
)"""

_AUDIO_COND = """(
    mime_type LIKE 'audio/%'
    OR kind = 'audio'
    OR lower(filename) LIKE '%.mp3'
    OR lower(filename) LIKE '%.wav'
    OR lower(filename) LIKE '%.m4a'
    OR lower(filename) LIKE '%.flac'
    OR lower(filename) LIKE '%.ogg'
    OR lower(filename) LIKE '%.aif'
    OR lower(filename) LIKE '%.aiff'
    OR lower(filename) LIKE '%.aac'
)"""

_ARCHIVE_COND = """(
    lower(filename) LIKE '%.zip'
    OR lower(filename) LIKE '%.tar'
    OR lower(filename) LIKE '%.gz'
    OR lower(filename) LIKE '%.tgz'
    OR lower(filename) LIKE '%.bz2'
    OR lower(filename) LIKE '%.tbz2'
    OR lower(filename) LIKE '%.xz'
    OR lower(filename) LIKE '%.txz'
    OR lower(filename) LIKE '%.7z'
    OR lower(filename) LIKE '%.rar'
)"""

_CODE_COND = """(
    kind IN ('code', 'html')
    OR lower(filename) LIKE '%.py'
    OR lower(filename) LIKE '%.ts'
    OR lower(filename) LIKE '%.tsx'
    OR lower(filename) LIKE '%.js'
    OR lower(filename) LIKE '%.jsx'
    OR lower(filename) LIKE '%.json'
    OR lower(filename) LIKE '%.yaml'
    OR lower(filename) LIKE '%.yml'
    OR lower(filename) LIKE '%.toml'
    OR lower(filename) LIKE '%.sql'
    OR lower(filename) LIKE '%.sh'
    OR lower(filename) LIKE '%.zsh'
    OR lower(filename) LIKE '%.bash'
    OR lower(filename) LIKE '%.html'
    OR lower(filename) LIKE '%.htm'
    OR lower(filename) LIKE '%.css'
    OR lower(filename) LIKE '%.scss'
    OR lower(filename) LIKE '%.log'
    OR lower(filename) IN ('dockerfile', 'makefile')
)"""


def upgrade() -> None:
    # 1. 컬럼 추가
    op.add_column("artifacts", sa.Column("category", sa.String(length=30), nullable=False, server_default="other"))
    op.add_column("artifacts", sa.Column("origin", sa.String(length=20), nullable=False, server_default="agent"))
    op.add_column("artifacts", sa.Column("keep_reason", sa.String(length=30), nullable=True))
    op.add_column("artifacts", sa.Column("keep_evidence", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True))

    op.create_index("ix_artifacts_category", "artifacts", ["category"])
    op.create_index("ix_artifacts_origin", "artifacts", ["origin"])

    # 2. 백업 테이블 생성 (변경 전 원본 상태 보존)
    op.execute("""
        CREATE TABLE artifact_area_backup_0068 AS
        SELECT id, area, origin, keep_reason
        FROM artifacts
    """)

    # 3. category 백필
    op.execute(f"""
        UPDATE artifacts
        SET category = CASE
            WHEN {_DOCUMENT_COND} THEN 'document'
            WHEN {_SPREADSHEET_COND} THEN 'spreadsheet'
            WHEN {_PRESENTATION_COND} THEN 'presentation'
            WHEN {_IMAGE_COND} THEN 'image'
            WHEN {_VIDEO_COND} THEN 'video'
            WHEN {_AUDIO_COND} THEN 'audio'
            WHEN {_ARCHIVE_COND} THEN 'archive'
            WHEN {_CODE_COND} THEN 'code'
            ELSE 'other'
        END
    """)

    # 4. origin 백필
    op.execute("""
        UPDATE artifacts
        SET origin = CASE
            WHEN source = 'upload' THEN 'user'
            WHEN source = 'user_registered' THEN 'user'
            WHEN source IN ('agent_output', 'agent_modified') THEN 'agent'
            ELSE 'unknown'
        END
    """)

    # 5. keep_reason 백필: 직접 업로드된 파일만 보관 사유 direct_upload 부여
    op.execute("""
        UPDATE artifacts
        SET keep_reason = 'direct_upload'
        WHERE source = 'upload'
    """)

    # 6. area 정리: 직접 업로드된 파일만 files(내 보관함) 유지.
    # 그 외(AI 생성·공유, 불명확 등) files 에 있던 모든 행은 agent(AI 작업 파일)로 이동.
    op.execute("""
        UPDATE artifacts
        SET area = 'agent'
        WHERE area = 'files' AND source != 'upload'
    """)


def downgrade() -> None:
    op.execute("""
        UPDATE artifacts SET
            area = b.area,
            origin = b.origin,
            keep_reason = b.keep_reason
        FROM artifact_area_backup_0068 b
        WHERE artifacts.id = b.id
    """)
    op.execute("DROP TABLE IF EXISTS artifact_area_backup_0068")
    op.drop_index("ix_artifacts_origin", "artifacts")
    op.drop_index("ix_artifacts_category", "artifacts")
    op.drop_column("artifacts", "keep_evidence")
    op.drop_column("artifacts", "keep_reason")
    op.drop_column("artifacts", "origin")
    op.drop_column("artifacts", "category")
