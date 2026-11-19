# 2026-11-19 Context Sharing v2 Private 구현 기록

작업 위치: `back-context-sharing-v2-20261119` (backend 기준 `978cb5ac`). 확정 계약은 `docs/context-sharing-v2-contract.md`; 이 파일은 Private 소유 범위의 구현·검증을 기록한다.

## 구현

- `single_use_v1` 기존 `/keys` 계약(1시간·1회 교환)을 유지하고 `reusable_api_v2` 발급을 `/control/v2/credentials`로 분기했다. TTL 1·3·7·30일, 기본 3일이며 pinned/latest 선택을 Gateway ACL로 전달한다.
- `/links` 발급·목록·폐기와 agent API 배선을 추가했다. key/list와 link/list를 분리했고 URL token은 발급 응답에만 노출한다. 링크 키 ID를 key revoke로 보내거나 반대로 보내면 404다.
- ShareKey와 ShareProject에 additive metadata/revoke state migration `0080_ctx_share_v2_keys`를 추가했다. 기존 키 기본 type은 `single_use_v1`이다.
- 원격 폐기 전에 `revoking` intent를 commit하고 Gateway 확인 뒤 `revoked`로 바꾼다. `generation_service.maintenance_jobs`에 제한 batch 재시도를 10~60초 간격으로 등록하고, 개발 legacy 경로도 같은 operation을 호출한다. 1시간 retention과 분리한다.
- publish payload에 draft UUID를 `operation_id`로 전달한다. 승인 digest와 scanner를 유지한다.
- 신규 v2 키 응답의 `connect.project_id`에 Gateway UUID를 넣어 read 없는 search/export 범위도 공개 REST 경로를 구성할 수 있게 한다.
- work 범위는 내부 초안 검토를 보존하되 신규 외부 publish와 모든 새 키 발급(v1 포함)을 `work_sharing_policy_required`로 막는다. 기존 v1 행은 재해석하지 않는다.
- 프로젝트 목록에 active key/link counts와 last published time을 보완하고 MCP tool/policy/assistant 및 route inventory를 갱신했다.

## 검증

Private API·migration suite는 카드에서 지정한 `test_cg_v2_private_20261119_b9f28799`를 사용한다. 실제 Gateway ASGI 통합 fixture는 기존 test harness대로 `gateway_<uuid>` 합성 DB를 별도로 만들었다. core worker DB나 운영 DB는 사용하지 않았다.

- 최초 test-first: Gateway v2 adapter와 v2 metadata model 테스트가 구현 전 실패하는 것을 확인했다.
- 최초 교정 RED: 신규 project_id, 신규 work v1 차단/기존 v1 보존, generation maintenance 등록, 짧은 revision ID 회귀 4건 모두 실패했다(기존 동작 확인).
- 영향 회귀 + 신규 검증: `PGPORT=5432 TTAKKARI_TEST_DB='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_v2_private_20261119_b9f28799' TTAKKARI_DATABASE_URL='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_v2_private_20261119_b9f28799' PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_context_sharing_gateway.py tests/test_context_sharing_migration.py tests/test_context_sharing_api.py tests/test_context_sharing_real_gateway.py tests/test_context_sharing_mcp.py tests/test_context_sharing_scanner.py tests/test_route_inventory.py tests/test_orchestrator.py`. 최종 결과 `197 passed in 21.64s`; pytest 출력에 skip 없음. 중간 집계의 `196`은 최종 회귀 수가 아니며 이 결과로 정정한다. 기존 공통 기준은 194건이고 최종 묶음에는 신규/parameterized 케이스가 추가됐다.
- 발급 연결 계약 증거: `tests/test_context_sharing_api.py::test_v2_connect_includes_gateway_project_id_without_read_scope`에서 실제 Private HTTP `POST /api/context-sharing/keys`의 `201` 응답 JSON `connect.project_id`를 `gateway.projects[slug]["id"]`(Gateway 발급 프로젝트 UUID)와 직접 대조한다. `scopes`를 `search` 단독 및 `export` 단독으로 parameterize해 두 응답 모두 `connect.url`과 project UUID가 존재함을 검사한다. 최종 suite의 이 두 케이스 포함 결과는 `197 passed in 21.64s`다. 이는 합성 Gateway control을 사용한 Private API 응답 검증이며 별도 실제 로컬 Gateway HTTP 연동 증거로 표현하지 않는다.
- 관리 UI 별도 결과는 기획이 **3 files / 88 passed (4.88s)**로 직접 재현했다는 보고를 반영한다. 이는 관리 UI 단위 검증이고 Private API의 실제 응답 검사와는 별도다. `connect.project_id`는 이미 이 Private 교정에 포함된 항목이므로 중복 구현/테스트 항목을 추가하지 않았다.
- TDD 교정 확인: 구현 전 신규 연결 UUID/work v1/maintenance/revision 테스트 4건이 모두 의도한 이유로 실패했다. 수정 후 같은 집중 테스트 4건은 `4 passed in 1.46s`로 통과했다.
- CLI migration은 별도 신규 소유 DB `test_cg_v2_migration_cli_20261119_b9f28799`에서만 실행했다. `PGPORT=5432 TTAKKARI_TEST_DB='<해당 DB URL>' TTAKKARI_DATABASE_URL='<해당 DB URL>' TTAKKARI_NO_ENV_FILE=1 TTAKKARI_ENV=dev PYTHONPATH=src ../back/.venv/bin/alembic upgrade 0079_context_sharing` 후 합성 v1 프로젝트/초안/게시/키 행을 넣었다. 같은 env로 `alembic upgrade head` 결과 `Running upgrade 0079_context_sharing -> 0080_ctx_share_v2_keys`; 조회 결과 `0080_ctx_share_v2_keys|single_use_v1|latest|[]`.
- 같은 별도 DB에서 legacy v1 합성 행만 둔 상태로 `alembic downgrade 0079_context_sharing` 결과 출력 `Running downgrade 0080_ctx_share_v2_keys -> 0079_context_sharing`; downgrade 직후 `0079_context_sharing|1`로 해당 키 행 보존 확인. 이어 `alembic upgrade head` 재실행 성공, 조회 `0080_ctx_share_v2_keys|single_use_v1|latest|[]`. 이 시험은 v2 credential 행이 있는 rollback을 검증하지 않았다. 실제 `downgrade()`는 v2 metadata 열을 drop하므로 v2 행이 남아 있을 때 실행하면 안 된다. revision 길이는 22자로 Alembic version column 32 이내다. 이 전용 DB는 시험 DB 삭제 금지에 따라 삭제하지 않고 그대로 둔다.
- `../back/.venv/bin/ruff check src/ttakkari/context_sharing/service.py src/ttakkari/generation_service.py src/ttakkari/main.py tests/test_context_sharing_api.py tests/test_context_sharing_migration.py alembic/versions/0080_ctx_share_v2_keys.py` 결과 `All checks passed!`; 변경 파일 `git diff --check`도 출력 없이 통과했다.
- 합성 Gateway core control/public 앱 ASGI 통합 fixture도 통과했다. v1 회귀, v2 반복 교환과 직접 Bearer, status/last-used 대조, pinned snapshot, link 발급, operation_id 저장을 확인했다. 이것은 격리 코드 연동이지 운영 Gateway 연결 증거가 아니다. Private→Gateway 별도 통합 검수는 `216f3ccb` 소유로 독립 진행 중이고, core 독립 검수는 `1ec1cc4b` 소유다.

## 제약과 다음 단계

- common contract 문서는 기획 소유이며 수정하지 않았다.
- Gateway core와 front 저장소는 이 worktree의 소유 범위가 아니다. Private 선택 회귀 최종치는 197건이며 공통 기존 기준 194건과 구분한다. 후속 운영 안내 교정에서 `rg --files deploy/context-gateway context_gateway`로 실제 compose·nginx·Dockerfile 경로를 확인했다. 최초 조사 당시 홈서버 nginx의 control host는 `/control/v1/`만 전달해 v2 경로와 불일치했다. 이후 기획이 저장소 nginx에 `/control/v2/` location을 추가하고 배포 계약 테스트로 확인했다. 현재 저장소 설정에는 두 prefix가 있으며 운영 적용·nginx syntax/runtime은 미검증이다. 이 문서 작업자는 코드/deploy 설정을 읽기 전용으로 두었다. 별도 운영 Gateway 호출·운영 게시·공개 활성화·배포·nginx 변경·재시작은 하지 않았다.
- v2 운영 안내 단계에서는 docs 소유 파일만 수정했다. Claude Code MCP 공식 문서(<https://code.claude.com/docs/en/mcp>)의 HTTP transport, Authorization header, `.mcp.json` 환경 변수 치환 절을 2026-11-19에 확인했다. 이 문서 근거는 운영 연결을 뜻하지 않는다.
- 운영 게시·외부 공개 활성화·push·배포·서비스 재시작은 실행하지 않았다.
