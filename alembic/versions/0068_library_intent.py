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


_NEW_COLUMNS = ("category", "origin", "keep_reason", "keep_evidence")


def _existing_columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("artifacts")}


def _has_backup_table() -> bool:
    return sa.inspect(op.get_bind()).has_table("artifact_area_backup_0068")


def upgrade() -> None:
    # 같은 SQL 을 alembic_version 밖에서 다시 실행해도 안전해야 한다. 데이터 정리 단계는 files 에 있는 사용자 보관 파일을
    # 다시 agent 로 내릴 수 있으므로, 새 컬럼이 이미 하나라도 있으면(= 이미 적용됨) 컬럼 보강만 하고 데이터는 건드리지 않는다.
    existing = _existing_columns()
    first_run = not (set(_NEW_COLUMNS) & existing)

    # 1. 컬럼 추가
    if "category" not in existing:
        op.add_column("artifacts", sa.Column("category", sa.String(length=30), nullable=False, server_default="other"))
    if "origin" not in existing:
        op.add_column("artifacts", sa.Column("origin", sa.String(length=20), nullable=False, server_default="agent"))
    if "keep_reason" not in existing:
        op.add_column("artifacts", sa.Column("keep_reason", sa.String(length=30), nullable=True))
    if "keep_evidence" not in existing:
        op.add_column("artifacts", sa.Column("keep_evidence", postgresql.JSONB().with_variant(sa.JSON(), "sqlite"), nullable=True))

    op.execute("CREATE INDEX IF NOT EXISTS ix_artifacts_category ON artifacts (category)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_artifacts_origin ON artifacts (origin)")

    if not first_run:
        print("[0068_library_intent] 이미 적용된 스키마라 데이터 정리는 건너뜀")
        return

    # 2. 백업 테이블 생성 (변경 전 원본 상태 보존: source, area)
    op.execute("DROP TABLE IF EXISTS artifact_area_backup_0068")
    op.execute("""
        CREATE TABLE artifact_area_backup_0068 AS
        SELECT id, source, area
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

    # 4. origin 백필: 직접 업로드만 user, AI 산출물은 agent, user_registered 및 불명은 unknown
    op.execute("""
        UPDATE artifacts
        SET origin = CASE
            WHEN source = 'upload' THEN 'user'
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
    # 그 외(AI 생성·공유, 링크 등록 등) files 에 있던 모든 행은 agent(AI 작업 파일)로 이동.
    op.execute("""
        UPDATE artifacts
        SET area = 'agent'
        WHERE area = 'files' AND source != 'upload'
    """)

    # 7. 업그레이드 결과(유지/이동 건수) 기록
    conn = op.get_bind()
    if conn is not None and hasattr(conn, "execute"):
        maintained = conn.execute(sa.text("SELECT count(*) FROM artifacts WHERE area = 'files' AND source = 'upload'")).scalar() or 0
        moved = conn.execute(sa.text("SELECT count(*) FROM artifact_area_backup_0068 WHERE area = 'files' AND source != 'upload'")).scalar() or 0
        print(f"[0068_library_intent] 보관함 분류 개편 완료: 내 보관함 유지 {maintained}건, AI 작업 파일로 이동 {moved}건")


def downgrade() -> None:
    if _has_backup_table():
        op.execute("""
            UPDATE artifacts SET
                area = b.area
            FROM artifact_area_backup_0068 b
            WHERE artifacts.id = b.id
        """)
    op.execute("DROP TABLE IF EXISTS artifact_area_backup_0068")
    op.execute("DROP INDEX IF EXISTS ix_artifacts_origin")
    op.execute("DROP INDEX IF EXISTS ix_artifacts_category")
    existing = _existing_columns()
    for name in ("keep_evidence", "keep_reason", "origin", "category"):
        if name in existing:
            op.drop_column("artifacts", name)
