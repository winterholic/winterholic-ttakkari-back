# 2026-11-19 Context Sharing v2 Private 독립 검수

## 검수 식별

- 작업 위치: `back-context-sharing-v2-20261119`
- 기준 revision: `978cb5ace5115743b32f375ce065120e8d068d82` (`origin/main`)
- 검수 시작 시 구현·기존 테스트·타 작업 문서의 dirty 변경이 있었다. 해당 파일은 읽기 전용으로 두었다. 이 검수에서 추가한 파일은 `tests/test_context_sharing_v2_private_acceptance.py`와 이 기록뿐이다.
- DB: `test_cg_v2_private_verify_20261119_private`, `test_cg_v2_private_verify_20261119_gateway`, `test_cg_v2_private_verify_20261119_migration` (모두 `PGPORT=5432`; 삭제하지 않았다). 기존 작업자의 core/private 시험 DB는 열거나 초기화하지 않았다.

## 완료 기준별 결과

1. **Private → 실제 Gateway A/B/C 합성 여정 — 통과.** 새 수락 테스트가 Private owner API와 실제 Gateway control/public 앱을 연결해 기본 3일 만료, pinned 링크 재교환·쿠키 반복 열람·폐기, v2 direct Bearer 반복 REST/MCP, 프로젝트 격리, pinned/latest 재게시를 확인한다. 검색 전용 키의 `connect.project_id`도 확인한다.

   ```text
   PGPORT=5432 TTAKKARI_TEST_DB=postgresql+asyncpg://127.0.0.1:5432/test_cg_v2_private_verify_20261119_private PYTHONPATH=src:context_gateway/src ../back/.venv/bin/python -m pytest -q tests/test_context_sharing_v2_private_acceptance.py
   3 passed in 2.00s
   db reset: 3 selective (avg 9.3/138 tables), 0 forced full, 0 fell back to full
   ```

2. **폐기 intent와 재시도 — 통과.** key·link·project 통신 실패 후 Private pending 및 신규 발급 차단을 확인했다. 미전달 상태에서 Gateway의 기존 부모 세션은 계속 동작하고, maintenance 재시도 완료 후 차단된다. Gateway 폐기는 성공하고 Private 완료 commit만 실패하는 경우에는 로컬 `revoking`이 보존되며 재시도 후 대사된다.

   ```text
   pytest ...::test_private_revoke_intents_retry_after_transport_and_local_commit_failures
   passed (최종 파일 전체 실행에 포함)
   Gateway revoked_at=true, Private status=revoking → retry callback → status=revoked; 부모 세션 REST 401
   ```

3. **runtime maintenance·work 차단·v1 호환·project ID — 통과.** `maintenance_jobs()` 등록 항목의 callable/10–60초 interval을 확인하고 실제 callback을 호출해 pending을 정리했다. 새 work v1/v2 발급 및 work publish는 `work_sharing_policy_required`; 기존 발급 v1 행은 active/`single_use_v1` 그대로이며 실제 Gateway에서 3600초·단회 exchange 의미를 확인했다. v2 read/search 키 발급 응답의 `connect.project_id`는 실제 Gateway UUID다.

   ```text
   pytest ...::test_work_v1_v2_issue_and_publish_block_and_existing_v1_gateway_contract
   passed (최종 파일 전체 실행에 포함)
   maintenance_jobs: context-sharing-revocations 등록·호출; existing v1: 3600s, exchange 200 → 401
   ```

4. **실제 Alembic CLI 이전 및 행 호환 — 통과.** 별도 migration DB에서 CLI로 base부터 `0079_context_sharing`까지 upgrade했다. 그 revision에서 합성 기존 v1 행(활성, 3600초)을 넣고 CLI로 `head`까지 upgrade한 뒤 `alembic_version` 및 행을 조회했다.

   ```text
   alembic upgrade 0079_context_sharing
   alembic upgrade head
   SELECT version_num → 0080_ctx_share_v2_keys
   SELECT credential_type, snapshot_policy, scopes, status, ttl_seconds → single_use_v1 | latest | [] | active | 3600
   SELECT revoke_state → active
   ```

5. **검수 기록·정적 확인 — 통과.** 이 문서에 revision/DB/실행 명령/출력과 skip·미검증 표면을 기록했다. 새 테스트 Ruff와 `git diff --check`가 통과했다.

   ```text
   ../back/.venv/bin/ruff check tests/test_context_sharing_v2_private_acceptance.py
   All checks passed!
   git diff --check → exit 0 (출력 없음)
   ```

## 음성 대조

마지막 pinned snapshot assertion을 임시 잘못된 UUID로 바꾸고 해당 테스트만 실행해 예상 위치에서 `F` 및 assertion failure를 확인했다. assertion을 원래대로 복원하고 전체 새 수락 파일을 다시 실행해 `3 passed`를 확인했다. 수정은 새 테스트 파일에서만 했다.

## Skip 및 미검증 범위

- 기존 전체 Private regression 및 Gateway core 전체 suite는 다시 실행하지 않았다. 카드에서 제외한 기존 유효 코어 검증 반복을 피했고, 신규 수락 파일만 실행했다.
- 운영 Gateway/DB, 실제 문서, 공개 서비스 활성화, 브라우저 UI/실기기, push, 배포 및 서비스 재시작은 실행하지 않았다. 이 검수는 별도 합성 DB와 ASGI 앱에 한정된다.
- `gateway_apps` fixture가 사용하는 Gateway 전용 DB는 유지하며 매 테스트마다 초기화하지 않는다. 반복 실행에도 slug 충돌이 나지 않도록 폐기 재시도 프로젝트 이름은 실행별 합성 ID를 쓴다.

## 후속: 표준 pytest DB 이식성 교정 (2026-11-19)

- **기준 revision:** `4e9a199889fbf9883b788b6d643c5fbdac18717e` (작업 시작 시 `HEAD`).
- **수정 범위:** `tests/test_context_sharing_v2_private_acceptance.py`의 Gateway fixture만 수정했다. Private은 `conftest.TEST_DB`와 `assert_test_db_isolated`를 재사용하고, Gateway는 `test_context_sharing_real_gateway.create_core_database`에 `gateway_<8 hex>` 이름을 전달한다. 두 URL의 database가 다름을 확인한다. 기존 보안 assertion 본문은 변경하지 않았다.
- **기준 실패(RED):** 새 합성 DB `test_cg_v2_portability_20261119`를 만들어 원래 파일을 실행했을 때 세 테스트가 모두 setup 중 고정 `PRIVATE_DB` assertion에서 실패했다.

  ```text
  PGPORT=5432 TTAKKARI_TEST_DB=postgresql+asyncpg://127.0.0.1:5432/test_cg_v2_portability_20261119 PYTHONPATH=src:context_gateway/src ../back/.venv/bin/python -m pytest -q tests/test_context_sharing_v2_private_acceptance.py
  3 errors in 0.83s — expected test DB name, got test_cg_v2_portability_20261119
  ```

- **명시적 합성 Private DB (GREEN):** 같은 새 DB로 수정 파일을 실행해 세 테스트를 통과했다. Gateway DB는 기존 helper가 매 fixture마다 독립 `gateway_` DB로 할당한다. DB 이름은 난수라 pytest 출력에는 찍히지 않는다. `test_cg_v2_private_verify_20261119_private` 및 기존 검수 DB들은 사용하거나 reset하지 않았다.

  ```text
  PGPORT=5432 TTAKKARI_TEST_DB=postgresql+asyncpg://127.0.0.1:5432/test_cg_v2_portability_20261119 PYTHONPATH=src:context_gateway/src ../back/.venv/bin/python -m pytest -q tests/test_context_sharing_v2_private_acceptance.py
  3 passed in 2.21s
  db reset: 3 selective (avg 9.3/138 tables), 0 forced full, 0 fell back to full
  ```

- **정적 검사:** 수정된 수락 파일 Ruff 및 `git diff --check` 통과. Diff상 보안 assertion은 그대로이고, import·fixture setup만 바뀌었다.

  ```text
  ../back/.venv/bin/ruff check tests/test_context_sharing_v2_private_acceptance.py
  All checks passed!; git diff --check → exit 0
  ```

- **기본 `pg_sandbox` 경로:** 실제 격리 클러스터 실행은 이 환경에서 미검증이다. `env -u TTAKKARI_TEST_DB ... pytest`로 시도했으나 `tests/conftest.py`의 `pg_sandbox.start_cluster()`가 `initdb`를 실행하는 단계에서 종료 코드 1을 받았다. 출력에는 stderr가 없어 원인은 확인되지 않았다. 코드 경로는 확인했다: conftest가 `PG_CLUSTER`를 만들고, 기존 `create_core_database`는 `CLUSTER is not None`일 때 `CLUSTER.create_database(name)`을 호출한다. 이는 로컬 코드 경로 확인이며 실제 CI 실행 증거는 아니다.

  ```text
  env -u TTAKKARI_TEST_DB PGPORT=5432 PYTHONPATH=src:context_gateway/src ../back/.venv/bin/python -m pytest -q tests/test_context_sharing_v2_private_acceptance.py
  ImportError while loading tests/conftest.py → pg_sandbox.start_cluster → initdb -D /tmp/ttakkari-test-.../data → exit 1
  ```

- **미실행:** 표준 deploy gate, 실제 CI, 운영 DB/서버, 브라우저, push 및 배포. 별도 `private_b9` gate DB는 접속·초기화하지 않았다. 삭제도 하지 않았다.
- **작업 후 revision:** 커밋하지 않았으므로 `HEAD`는 기준 SHA 그대로다. 변경 파일은 worktree diff로 남겼다.
