"""보관함 재분류: 파일 탭에 쌓인 에이전트 부산물을 에이전트 파일로 옮긴다.

outbox 자동 수집이 세션 종류와 무관하게 에이전트 파일이 되면서, 이미 파일 탭에
들어간 부산물(로그·증거 JSON 등)도 같은 기준으로 옮긴다. 보고서·이미지·압축 같은
결과물 성격의 파일은 건드리지 않는다(앱·MCP 에서 되돌릴 수 있음).
영향 행은 artifact_area_backup_0052 에 보관하고 downgrade 에서 되돌린다.
"""

from alembic import op

revision = "0052_artifact_area_byproducts"
down_revision = "0051_scope_memo_event"
branch_labels = None
depends_on = None

_BYPRODUCTS = """
    area = 'files'
    AND deleted_at IS NULL
    AND source IN ('agent_output', 'agent_modified')
    AND (
        filename LIKE '%.log'
        OR filename LIKE '%evidence%.json'
        -- snapshot/cleanup/bindings 는 기계 부산물 확장자로 한정한다.
        -- cleanup-plan.md, snapshot-analysis.md 같은 정상 문서는 건드리지 않는다.
        OR (filename LIKE '%snapshot%' AND (filename LIKE '%.json' OR filename LIKE '%.py' OR filename LIKE '%.xml'))
        OR (filename LIKE '%cleanup%' AND (filename LIKE '%.json' OR filename LIKE '%.xml'))
        OR filename LIKE '%bindings%.json'
        OR filename LIKE 'final-%.json'
        OR filename LIKE 'check-%.py'
        OR original_path LIKE '%/outbox/%/browser/page-%'
        OR original_path LIKE '%/outbox/%/evidence/%'
    )
"""


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE artifact_area_backup_0052 AS
        SELECT id, area FROM artifacts WHERE {_BYPRODUCTS}
    """)
    op.execute(f"UPDATE artifacts SET area = 'agent' WHERE {_BYPRODUCTS}")


def downgrade() -> None:
    op.execute("""
        UPDATE artifacts SET area = b.area
        FROM artifact_area_backup_0052 b WHERE artifacts.id = b.id
    """)
    op.execute("DROP TABLE artifact_area_backup_0052")
