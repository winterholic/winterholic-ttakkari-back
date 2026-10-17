# 디자인시스템 조회 화면·도구 (2026-10-18)

사용자 요청: 로깅 조회처럼 winterholic-design-system을 따까리에 연결해 자세히 확인한다. 설계는 [ADR 0034](../adr/0034-design-system-browser.md).

구현: 커밋 고정 Git blob 색인, 소유자/개인 세션 API, 읽기 전용 MCP 3종(design_system_catalog/search/item). 7개 시스템의 토큰·테마별 참조·설명·원본 포인터, 문서·컴포넌트 CSS·HTML 예제 소스 검색/구간 읽기. 임의 경로·symlink·회사 세션을 차단한다. 미커밋 디자인시스템 편집은 읽지 않으며 DB/프로토콜 변경은 없다.

격리 작업 디렉터리 back-design-system에서 구현했다. 원래 back와 디자인시스템 저장소의 다른 작업 변경은 보존한다. 프론트는 front-design-system, 원래 front의 분기 및 미커밋 변경은 보존한다.

실행 검증:
- `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_ds_20261018_0025 PYTHONPATH=src ../back/.venv/bin/python -m pytest tests/test_design_system_integration.py tests/test_orchestrator.py tests/test_oplog.py unit_tests/test_design_system.py unit_tests/test_design_system_api.py unit_tests/test_knowledge_mcp.py -q --timeout=90`: **83 passed in 23.14s**. 실제 JWT·테스트 DB 개인/회사 세션 경계, 원본 읽기, 기존 로그·오케스트레이터·stdio MCP 도구 등록 회귀 포함. 백엔드 전체 실행은 아님.
- `pytest --collect-only -q`: 당시 **1458 tests collected**. 긴 제목 회귀 1건을 이후 추가했다. 수집 검사는 통과 수가 아니다.
- 변경 Python 파일 `ruff check`, `git diff --check`: 통과.
- 실제 DS 6d4b27cce40c5d73749ec276ef92017d14b94df9: 7개 시스템·4,957개 토큰, 라이트/다크 참조 해석 오류 0. 원본 자체의 디자인 품질 판정은 아니다.
- 프론트 전체: 564 passed, 1 skipped; tsc·eslint·ui-lint·프로덕션 빌드 통과. UI는 합성 인증을 둔 로컬 검증 서버에서 실제 커밋 원본을 읽어 확인했다.
- configured ttakkari_browser: 320·390·1440 폭, 토큰·문서·예제 및 구간 이동, 라이트·다크, 긴 표 내부 스크롤 확인. 최종 preview console error 0. 실제 iOS·운영 사용자 인증은 미검증.

증거와 설계 사본은 Run outbox `98a6b230-f896-4ce2-b9a3-fef4c684451e`에 둔다. 운영 배포 상태는 최종 예약 응답이 기준이며 예약 수락을 성공으로 쓰지 않는다. 기존 Run의 MCP 프로세스에 도구가 추가된다고 보장하지 않으며 새 Run에서 확인한다.

원격 main의 `d9d1f23`(운영 로그 강화)을 rebase한 최종 코드에서 같은 검증 명령을 다시 실행했다: **93 passed in 25.60s**, ruff 통과. 증가한 10건은 원격 로그 회귀 검사다. 위 83건은 rebase 전 실행이며 최종 근거는 이 93건이다.
