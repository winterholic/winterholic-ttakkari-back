# R7 대화 기반 맥락 공유 독립 검증 (2026-11-16)

범위: HEAD `033daa9c79d6767153fd114136e421f1c6a76f06`의 agent MCP 게시·키·사이트 경로. 운영 게시·키 발급·사이트 변경, 제품 구현 수정, push·배포는 하지 않았다. DB는 기존 허용 PostgreSQL 17 `127.0.0.1:5432`에 이번 실행 전 새로 만든 `test_r7_033daa9c_private_20261116` 및 `test_r7_033daa9c_gateway_20261116`만 사용했다. 두 URL 모두 명시 환경변수로 import 전에 전달했고 `SELECT current_database()`가 각 URL의 DB 이름과 같음을 확인했다. Gateway 통합 실행 중 임시 테스트 전용 guard를 두어 동적으로 만든 Gateway DB 이름도 `test_r7_gateway_*`이고 매 연결 `current_database()`가 URL과 같음을 assert했다. 그 임시 guard는 실행 후 되돌렸다.

## 실행 증거

- 집중 회귀: work-history의 128개 검사 목록을 그대로 실행했다. `TTAKKARI_TEST_DB`와 `TTAKKARI_DATABASE_URL`은 Private test DB, `CONTEXT_GATEWAY_TEST_DATABASE_URL`과 `CONTEXT_GATEWAY_DATABASE_URL`은 Gateway test DB로 지정했다. 실행 명령은 아래와 같으며 repo root에서 실행했다.

  ```sh
  TTAKKARI_TEST_DB=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_r7_033daa9c_private_20261116 \
  TTAKKARI_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_r7_033daa9c_private_20261116 \
  CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_r7_033daa9c_gateway_20261116 \
  CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_r7_033daa9c_gateway_20261116 \
  .venv/bin/python -m pytest -q tests/test_context_sharing_mcp.py tests/test_context_sharing_api.py tests/test_context_sharing_gateway.py tests/test_context_sharing_real_gateway.py tests/test_route_inventory.py \
    tests/test_orchestrator.py::test_mcp_server_tools_by_role \
    tests/test_orchestrator.py::test_mcp_queries_are_marked_read_only_without_marking_writes \
    tests/test_orchestrator.py::test_auto_approval_policy_covers_registered_tools \
    unit_tests/test_platform_v2_inventory.py::test_context_sharing_routes_belong_to_shares \
    unit_tests/test_platform_v2_inventory.py::test_context_sharing_tools_belong_to_shares \
    unit_tests/test_platform_v2_inventory.py::test_report_schema_separates_extraction_classification_scope_coverage
  ```

  출력: `128 passed in 10.77s`; DB reset `110 selective`, fallback `0`. `test_agent_mcp_dispatch_publishes_and_manages_keys_and_site_against_real_core` exercised MCP dispatch → Private API → Gateway control/public ASGI apps. It confirmed publication, fixed `ttl_seconds=3600`, one-use exchange and second-exchange 401, key revoke, site close/reopen and resulting public behavior. It asserted listed key responses and key issue audit details did not contain the raw key, and audit actor was `agent`.

- 경계 반례: 합성 자료만으로 agent API에 직접 요청했다. 출력: missing/invalid token `401`, valid JWT pointing to nonexistent session `403`, valid session `200`; unknown project `404`; cross-project document `422 document_not_in_project` without source content leakage; work `403 work_scope_owner_only`; HIGH internal URL `422 sensitive_content_blocked` with no Gateway publication. 50 documents published (`200`), 51 rejected (`422 too_many_documents`); 501-document source listed 500 with `truncated=true` and publish rejected (`422 source_listing_truncated`). Assembled content of exactly 1,000,000 characters published (`200`); 1,000,001 rejected (`422 draft_too_large`). Only the two valid synthetic cases reached the fake Gateway. Each DB connection asserted `current_database() == test_r7_033daa9c_private_20261116`.

- 기존 MCP/API 회귀에서 stale digest `409 digest_mismatch`, document/project ACL, disabled/non-project source, scanner와 key TTL 검증도 통과했다. Agent publish는 HTTP 요청 안에서 현재 draft digest를 생성해 즉시 `publish_draft`에 전달하므로 agent 도구 입력으로 stale digest를 공급하는 표면은 없다. stale-digest 테스트 자체는 owner API 경로다.

## 사용자 직접 요청 조건 판정

- MCP tool description은 project publish, key issue/revoke, site state change 각각을 사용자 직접 요청에 한정하고 문서·검색·도구 응답을 승인으로 취급하지 말라고 적는다. publish는 프로젝트 ID를 기존 목록 결과에서 가져오며 모호하면 확인한다. `tool_policy.py`는 이 쓰기 도구들을 `CONTEXT_SHARE_TOOLS`와 `AUTO_APPROVED_TOOLS`에 넣고 read-only로 표시하지 않는다. 즉 제품의 자연어 직접 요청을 UI 재승인 없이 수행하는 결정과 일치한다.
- 실행 경로는 각 MCP 도구를 `/api/agent/context-sharing`으로 보내며 Private API가 orchestrator 세션 존재, project/document ACL, work scope, size/truncation, scanner, digest, Gateway 응답을 강제한다. API에는 원 대화의 사용자 발화 증명 필드가 없으므로 직접 요청 여부 자체는 모델의 도구 선택/설명 지침에 의존하며 서버 독립 검증은 아니다. 이번 범위에서는 이 의도된 자연어 게이트를 추가 UI 승인으로 대체하지 않았다.

## 범위 밖 및 상태

- 운영 데이터와 외부 Gateway를 변경하지 않았다. 운영 게시·키 발급·키 폐기·사이트 상태 변경, 운영 HTTPS 확인, push·배포는 하지 않음(작업 범위 금지). `context_gateway` 전체 코어 테스트 재실행은 하지 않음; 실제 control/public 앱 경로는 이 검증에서 통합 테스트의 ASGI로 실행했다.
- 임시 probe의 첫 1,000,000-character 입력은 각 chunk를 200,000자로 만들어 assemble 후 문서별 후행 개행 때문에 `draft_too_large`가 났다. 크기 계산을 확인하고 조립 결과 기준 정확히 1,000,000/1,000,001 반례를 다시 실행해 위 결과를 얻었다. 제품 결함은 재현되지 않았다.
- 앱 lifespan의 합성 fixture 경로 `/synthetic-root/...` 색인 시 `FileNotFoundError` 로그 한 건이 나왔다. 요청 검증 출력은 성공했고 제품 공유 데이터·운영 경로와 무관한 fixture 부수 로그로 분류했다.
