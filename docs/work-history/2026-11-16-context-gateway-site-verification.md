# Context Gateway 사이트 제어 검증 보완

기준 제품 revision은 `033daa9c79d6767153fd114136e421f1c6a76f06`이다. 사이트 독립 검증자 `886f26b5-a0ea-4adf-9e2b-3293c8ccb912`의 보고를 받아 기획이 Gateway 변경 범위와 시험 이식성을 대조했다.

- `git diff e85517ea 033daa9c -- context_gateway`는 출력이 없었다. 사이트 검증 시작과 종료 revision 사이 Gateway 소스는 동일하다.
- 독립 검증자는 자기 PostgreSQL DB에서 사이트 테스트 3개 통과를 보고했다. 기존 세션·미교환 키·REST·export·MCP의 차단과 재개, 원래 TTL 및 snapshot 보존, public DB 역할의 SELECT-only 권한을 관측했다. 같은 SDK 클라이언트의 닫힘 이후 새 MCP 읽기도 503이었다. 누락 row는 초기화 함수 재실행 뒤에도 닫힘을 유지했다. 진행 중 요청 취소나 열린 스트림 강제 종료는 미검증이다.
- 고정 테스트 하나가 `current_database()`를 이전 작업자의 DB 이름 `test_cg_site_4aea5321`과 비교해 독립 시험 DB에서 실행되지 못했다. 기획이 자신의 `test_cg_portability_ee17c693`을 생성하고 동일 테스트를 실행해 `AssertionError: 'test_cg_portability_ee17c693' == 'test_cg_site_4aea5321'`, `1 failed in 0.32s`를 직접 재현했다.
- 해당 assertion은 `make_url(settings.database_url).database`와 실제 DB 이름을 비교하도록 수정했다. 실제 접속 DB 대조는 유지하며, 명시 URL의 시험 DB 이름 제한은 기존 conftest가 검사한다. 제품 런타임은 변경하지 않았다.
- GREEN: `cwd=context_gateway`, `CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_portability_ee17c693 CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_portability_ee17c693 .venv/bin/python -m pytest -q -o asyncio_default_fixture_loop_scope=function tests` → `34 passed in 2.52s`, skip 0. `.venv/bin/ruff check tests/test_rest_mcp_parity.py` → `All checks passed!`; `git diff --check -- tests/test_rest_mcp_parity.py` → 종료 0.
- 독립 보고의 보조 종합 스크립트 2회는 감사 이벤트 개수 기대값 assertion으로 실패했다. 통과 근거로 세지 않는다. 기획의 위 전체 Gateway 검사는 별도로 실제 실행한 증거다.
- 보존 대상 기존 work-history 미커밋 8줄은 수정하거나 이 커밋에 포함하지 않는다. 새 운영 배포, 운영 게시·키 발급·사이트 변경은 이 보완 단위에서 실행하지 않았다.
