# Context Gateway 독립 검증

## 판정

Gateway core 전체 테스트를 전용 PostgreSQL DB에서 실행했다. 최초 4건의 실패는 테스트 격리 또는 fixture/테스트 문법 문제였다. 작업 도중 core worker가 문서 provenance 보존 코드를 추가했고, 최신 고정 SHA snapshot의 Gateway 전체 테스트는 **29 passed, 0 failed, 0 skipped**다. REST/MCP parity와 rate-limit 감사 및 문서 provenance 회귀검사도 포함된다. Private API/MCP/실제 Gateway 56 passed 기록은 이번 소유 변경으로 영향받지 않아 기존 증거를 재사용했다. 외부 배포 및 외부 HTTPS 경로는 검증하지 않았다.

## revision과 변경 경계

- 최초 실패 재현 HEAD: `bba4990b4df1f026e3fed4687caa3b846e1340c5` (`feat(context-gateway): revoke project shares and version manifests`). 작업 중 Private worker가 `185ea8bdb708f8a4cdf11739b930ea9abba86c7e` (`fix(context-sharing): align publisher with gateway contract`, 2026-11-16 10:52:46 +09:00)를 추가했다. 해당 변경은 Private 구현·계약 경로 7개이며 이번 커밋에 넣지 않았다.
- 이후 `54d89036a5c253b9dd4adc18ede3e3eff6858e4b` (`docs(context-sharing): clarify ADR 0030 boundary`), `8e12b0d7812df185f9dd287439538f5520243dea` (`docs(context-gateway-web): record live runtime verification`), `2f5f2e4546091114c13b8530a6dff2491d22b67a` (`fix(context-gateway): retain document provenance`)가 추가됐다. 최신 core commit은 document provenance를 보존하는 코드와 회귀테스트를 포함한다. 그 앞선 공유 checkout 재실행에서 provenance 기대값 2건이 실제 구현과 어긋나는 것을 관측했으며, 이 작업에서는 구현을 수정하지 않았다. 최종 안정 검증 기준은 `2f5f2e4546091114c13b8530a6dff2491d22b67a` + 아래 소유 파일 변경분이다.
- 이번에 수정·커밋할 경로: `context_gateway/tests/conftest.py`, `context_gateway/tests/test_key_exchange.py`, `context_gateway/tests/test_rest_mcp_parity.py`, 이 보고서.
- 안정 검증 worktree: `/Users/winterholic/development/context-gateway-verify-2f5f2e4`. 해당 snapshot에는 소유 파일 4개만 추가 적용했다. 이전 shared checkout 실행 결과 `3 failed, 24 passed, 1 error`는 동시 수정 중의 중간 상태다. 두 provenance 회귀검사는 그 시점에 실제 누락을 잡았고 core worker가 `2f5f2e45`에 수정을 커밋했다. 다른 fixture 오류와 파일 혼합은 안정적으로 귀속하지 않았다. 이 작업은 core 구현을 수정하지 않았다.
- 최종 검증 HEAD full SHA: `2f5f2e4546091114c13b8530a6dff2491d22b67a`. 커밋 직전 공유 checkout의 미커밋 경로는 이번 소유 파일 `context_gateway/tests/conftest.py`, `context_gateway/tests/test_key_exchange.py`, `context_gateway/tests/test_rest_mcp_parity.py`, 본 보고서와 타 작업 문서 `docs/context-gateway-discovery.md`, `docs/work-history/2026-11-16-context-gateway-progress.md`였다. 이번 커밋은 앞의 소유 파일 4개만 포함한다.
- 소유 파일 외 shared dirty 파일을 되돌리거나 커밋하지 않았다. 이번 커밋에는 소유 파일 4개만 경로 지정한다.
- `context_gateway/tests/test_http_gateway.py`와 구현 파일은 읽기만 했다. Gateway 인증/게시 구현은 변경하지 않았다.

## 실패 재현 및 분류

초기 전용 DB 전체 실행은 `21 passed, 4 failed, 0 skipped`였다.

1. `test_publisher_rejects_hash_mismatch_without_publishing`: 앞선 HTTP 테스트가 만든 snapshot 행이 같은 DB에 남아 `select(Snapshot.id) is None` 검사가 실패했다. hash mismatch 응답 자체는 409였다.
2. `test_only_one_concurrent_exchange_can_consume_a_key`: 테스트가 APIKey만 저장해 Project/KeyACL이 없었다. 현재 인증 구현의 유효 grant 검사가 두 교환 모두 거부했다.
3. `test_session_expiry_is_capped_at_the_original_key_expiry`: 같은 ACL 누락으로 교환 전에 `AuthenticationError`가 발생했다.
4. `test_expired_and_reused_keys_cannot_exchange`: 테스트가 `pytest.raises`를 `async with`로 사용해 `TypeError: 'RaisesExc' object does not support the asynchronous context manager protocol`가 발생했다.

## 수정

- `conftest.py`에 자동 DB 격리 fixture를 두어 각 테스트 전에 Gateway schema를 재생성한다. `db_sessions`는 schema를 다시 drop/create하지 않고 격리된 schema의 session factory만 제공한다.
- key exchange fixture가 매 실행마다 synthetic Project와 유효한 `read` KeyACL을 저장하도록 했다. 동시 교환, 원래 만료시각 상한, 만료 및 재사용 거부 assertion은 그대로 유지했다.
- 비동기 함수 호출을 동기 `with pytest.raises(...)`로 감쌌다.
- parity 검사는 synthetic project/key로 REST와 MCP SDK의 context 문서·snapshot 및 search 결과를 비교하고, read-only search 차단과 동시 교환의 `[200, 401]` 결과를 확인한다.

## 실행 증거

모든 pytest 명령 전에 두 환경 변수 URL을 SQLAlchemy `make_url`로 파싱해 database가 정확히 `test_cg_verify_141e23a3`인지 assert했다. 사용한 DB URL은 아래 전용 DB뿐이다.

초기 재현 명령:

```bash
PYTHONPATH=context_gateway/src \
CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_verify_141e23a3 \
CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_verify_141e23a3 \
.venv/bin/pytest -q -o asyncio_default_fixture_loop_scope=function context_gateway/tests
```

출력: `4 failed, 21 passed in 0.90s` (앞서 열거한 네 테스트만 실패).

수정된 네 실패 테스트:

```bash
PYTHONPATH=context_gateway/src CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_verify_141e23a3 CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_verify_141e23a3 .venv/bin/pytest -q -o asyncio_default_fixture_loop_scope=function context_gateway/tests/test_http_gateway.py::test_publisher_rejects_hash_mismatch_without_publishing context_gateway/tests/test_key_exchange.py
```

출력: `4 passed in 0.50s`.

요청된 전체 Gateway 테스트 명령. 최종 재실행은 pinned worktree의 HEAD `2f5f2e4546091114c13b8530a6dff2491d22b67a`에서 실행했다:

```bash
PYTHONPATH=context_gateway/src \
CONTEXT_GATEWAY_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_verify_141e23a3 \
CONTEXT_GATEWAY_TEST_DATABASE_URL=postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_verify_141e23a3 \
.venv/bin/pytest -q -o asyncio_default_fixture_loop_scope=function context_gateway/tests
```

출력: `29 passed in 1.74s`; 실패 및 skip 없음. 앞선 pinned `8e12b0d…` snapshot에서는 provenance/rate-limit 신규 파일이 아직 commit되지 않아 `25 passed in 1.27s`였다.

공유 checkout에서 동시 수정 중 재실행한 결과는 `3 failed, 24 passed, 1 error`였다. 그중 document-level provenance 2건의 KeyError는 실제 미구현 필드로, core worker가 후속 `2f5f2e45`에서 구현·회귀검사를 커밋했다. 나머지 중간 결과는 fixture 파일이 실행 중 변경된 상태라 안정적으로 귀속하지 않았다. 최신 pinned HEAD에서 신규 회귀 포함 전체를 재실행해 위 29 passed 결과를 얻었다.

정적 검사:

```text
.venv/bin/ruff check context_gateway/tests/conftest.py context_gateway/tests/test_key_exchange.py context_gateway/tests/test_rest_mcp_parity.py
All checks passed!

git diff --check
(exit 0, 출력 없음)
```

## 기존 Private 증거와 미검증 경계

- 재사용한 기존 검증: `tests/test_context_sharing_api.py tests/test_context_sharing_mcp.py tests/test_context_sharing_real_gateway.py` → **56 passed, 0 failed, 0 skipped**. 이번 변경은 Gateway test fixture와 Gateway parity 테스트에만 한정했고 Private 경로를 수정하지 않았다. 해당 테스트는 이번 턴에서 반복 실행하지 않았다.
- 운영 `ttakkari`, 다른 시험 DB, 공유 PostgreSQL 설정은 읽거나 수정하지 않았다. `initdb`, Docker daemon, DB 삭제, push, deploy도 실행하지 않았다.
- 독립 Gateway 외부 배포, 별도 서비스 프로세스/Compose 네트워크 간 HTTP, 외부 HTTPS는 미검증이다. 테스트 결과를 운영 배포 증거로 해석하지 않는다.
- 시험 DB `test_cg_verify_141e23a3`는 테스트가 schema를 재생성하는 용도로 사용했다. DB 자체를 삭제하지 않았다.
