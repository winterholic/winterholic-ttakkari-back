"""보관함 재분류: 파일 탭에 쌓인 에이전트 부산물을 에이전트 파일로 옮긴다.

outbox 자동 수집이 세션 종류와 무관하게 에이전트 파일이 되면서, 이미 파일 탭에
들어간 부산물도 같은 기준으로 옮긴다. 원칙은 애매하면 옮기지 않는 것이다. 사용자 결과물이
에이전트 파일에 묻히는 쪽이 탭이 지저분한 쪽보다 나쁘기 때문이다. 그래서 이름만으로는
정상 산출물일 수 있는 snapshot-*.json, cleanup-*.json, final-*.json, check-*.py 같은 파일은
옮기지 않고, 기계 부산물이 확실한 것만 옮긴다.
- 로그(*.log), 증거 캡처 폴더(outbox/<run>/evidence/), 브라우저 캡처(outbox/<run>/browser/page-*),
  워커 증거 JSON(*-worker-evidence.json).
- 폴더 기준 규칙은 자동 수집 행(원본 경로가 outbox/<run>/<rel_path>)에만 적용한다. share 로 올린
  행은 rel_path 앞에 run 폴더가 붙어 여기서 제외되므로, 사용자에게 넘기려고 공유한 파일은 건드리지 않는다.
영향 행은 artifact_area_backup_0052 에 보관하고 downgrade 에서 되돌린다(앱·MCP 에서도 되돌릴 수 있음).
"""

from alembic import op

revision = "0052_artifact_area_byproducts"
down_revision = "0051_scope_memo_event"
branch_labels = None
depends_on = None

# 폴더 규칙에 걸려도 기계 산출물 확장자만 옮긴다. 운영 DB 실측(2026-10-20, files 탭 자동 수집 행):
# evidence/ 는 json·txt·html·xml, browser/page-* 는 yml(접근성 스냅샷)·png 였다.
# 문서·납품물(md, txt, pdf, docx, zip 등)이 이 폴더에 있으면 사용자 결과물일 수 있어 유지한다.
_MACHINE_EXT = " OR ".join(
    f"lower(filename) LIKE '%.{ext}'" for ext in ("json", "log", "png", "jpg", "jpeg", "webp", "html", "xml", "yml", "yaml")
)

_BYPRODUCTS = f"""
    area = 'files'
    AND deleted_at IS NULL
    AND (
        (source = 'agent_modified' AND filename LIKE '%.log')
        OR (
            source = 'agent_output'
            AND original_path LIKE ('%/outbox/%/' || rel_path)
            AND (
                filename LIKE '%.log'
                OR filename LIKE '%-worker-evidence.json'
                OR (
                    (rel_path LIKE 'evidence/%' OR rel_path LIKE 'browser/page-%')
                    AND ({_MACHINE_EXT})
                )
            )
        )
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
