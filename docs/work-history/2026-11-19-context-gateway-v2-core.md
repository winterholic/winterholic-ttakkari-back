# Context Gateway v2 core 작업 기록

- 일자: 2026-11-19 (Asia/Seoul)
- 작업 위치: `back-context-sharing-v2-20261119`
- 기준 revision: `978cb5ace5115743b32f375ce065120e8d068d82` (작업 트리는 미커밋 변경 상태)
- 검증 DB: 작업 카드의 독립 코어 DB `test_cg_v2_core_20261119_b9f28799`; `CONTEXT_GATEWAY_TEST_DATABASE_URL`, `TTAKKARI_TEST_DB`, `PGPORT=5432`를 명시함
- 쓰기 범위: `context_gateway/src/`, `context_gateway/tests/`, 지정 work-history. 웹 source 및 Private source/tests/alembic은 변경하지 않음.

## 구현

- Gateway API key에 `credential_type`, `last_used_at`, 자격증명 공유 rate window를 추가. 기존 행은 `single_use_v1` 기본값, 기존 키 교환은 1회·1시간 semantics를 유지.
- `/control/v2/credentials` 발급과 `/control/v2/credentials/status` metadata 조회 구현. `reusable_api_v2` / `share_link_v2`, TTL 1·3·7·30일(기본 3일), 프로젝트 하나, pinned/latest ACL, 발급 영수증 및 마지막 사용 시각을 처리. 원문은 발급 응답만 반환하고 status에는 제외.
- `reusable_api_v2`의 반복 `/api/v1/auth/exchange`, raw Bearer REST/MCP 접근 구현. REST와 MCP에서 같은 snapshot resolver 및 scope 검사 사용. API key와 그 키로 만든 session이 자격증명 단위 rate counter를 공유.
- share link 교환·조회·Markdown export·logout 경로 구현. 반복 교환은 15분 이하 세션 쿠키로 제한하며 Secure/HttpOnly/SameSite=Strict 및 `/api/v1/share` path 설정. Origin, JSON content-type, Fetch Metadata 검증, no-store/no-referrer/CSP/noindex, parent 키·session·project·snapshot 및 site switch 재검사를 적용.
- `/s`가 기존 정적 index shell을 반환하도록 하고 static response에 noindex를 추가.
- Gateway runtime init에 additive DDL을 추가하고 기존 v1 type·ACL 기본값을 보존. 기존 snapshot immutability trigger 유지 확인.
- optional `operation_id`를 payload SHA-256 및 snapshot ID 영수증으로 저장. 같은 payload 재시도는 같은 snapshot response, payload 변경 재사용은 409; receipt와 latest pointer를 같은 DB commit에 기록.
- 신규 테스트: `test_v2_control_contract.py`, `test_share_link_v2.py`, `test_publish_idempotency.py`, `test_runtime_migration_v2.py`; 기존 MCP 툴 목록은 계약상 export 도구 추가에 맞춰 기대 목록에 반영.

## 2026-11-19 누락 보완 (코어 교정 단계)

- 검토 중 발견: pinned grant에서도 `Project.name`, `description`, `latest_snapshot_id`를 직렬화해 후속 게시 metadata가 섞였고, manifest에 project 이름/요약이 없었다. `validate_session`도 share 부모 세션을 일반 MCP HTTP gate에서 허용했다. v2 auth 만료·폐기도 `unauthorized`로 합쳐졌다.
- 새 manifest에 게시 당시 `project_name` / `project_description`을 저장한다. pinned ACL에 metadata를 저장하며, manifest에 metadata가 있는 과거 snapshot grant는 그 게시 당시 값, metadata가 없는 legacy snapshot은 발급 시점의 Project 값을 저장한다. 해당 저장값까지 없는 기존 pinned ACL은 최신 Project 값을 노출하지 않고 slug/빈 요약으로 제한한다. snapshot 행/manifest는 갱신하지 않았다.
- REST 및 MCP project/list/context와 share cookie context는 credential ACL의 pinned metadata 및 pinned snapshot ID를 직렬화한다. pinned snapshot 목록은 해당 snapshot만 반환한다. latest grant는 최신 Project metadata를 유지한다.
- `validate_session`은 `share_link_v2` 부모 세션을 거부한다. MCP HTTP auth gate는 자격 유형·만료·폐기를 먼저 구분하며 share session은 `audience_mismatch`를 낸다. 일반 REST Bearer도 share session을 거부한다.
- v2 인증 오류: REST 및 MCP transport auth 거절은 HTTP 401 JSON `{"detail":"<code>"}`이며 `<code>`는 `unauthorized`, `credential_expired`, `credential_revoked`, `audience_mismatch` 중 하나다. share cookie REST 응답도 동일한 `detail` 필드를 쓴다. 브라우저/UI 소비자는 `detail`을 읽어 만료/철회/잘못된 audience를 구분할 수 있다. v1 키/세션 실패는 기존 `unauthorized`, scope/project 부재 404 및 rate limit 429 의미를 유지한다. MCP JSON-RPC tool 실행 단계의 error content shape는 이 교정에서 별도 UI 계약으로 검증하지 않았다.
- 이번 교정 변경 파일: `context_gateway/src/context_gateway/apps.py`, `auth.py`, `models.py`, `publishing.py`, 신규 `context_gateway/tests/test_v2_metadata_auth_regressions.py`, 본 work-history. 다른 worktree에서 함께 보이는 Private/UI 및 기존 core 변경은 이번 교정에서 손대지 않았다.
- RED: 새 metadata regression은 두 번째 게시 후 REST `/api/v1/projects`가 새 이름을 반환해 실패했고, v2 expiry test는 `unauthorized`를 받아 실패했다. 추가 description-only 게시 회귀는 같은 이름으로 설명만 바꿨을 때 manifest가 이전 설명을 남겨 실패했다. 각 실패를 해당 동작 수정 후 재실행해 통과시켰다.
- 회귀검증 DB는 `test_cg_v2_core_20261119_b9f28799`만 사용했다. 최종 명령과 결과는 아래 검증 증거의 갱신 내용을 참조한다. 운영 데이터/브라우저 UI/배포/서비스 상태는 실행하지 않았다.

## 완료 기준별 결과

1. **v1 호환과 v2 발급 타입** — 완료. v1 단회 교환 회귀 및 v2 반복 교환·share audience 분리를 테스트.
2. **발급/status 영수증, CSPRNG/hash, TTL, grant shape** — 완료. 실제 DB 제약의 control route 테스트, 기본 3일·1일 TTL, status 원문 비노출 및 last_used 검증.
3. **반복 raw Bearer와 REST/MCP 정책 parity** — 완료. REST raw key 요청, 반복 exchange, MCP context/search/export 도구를 시험. pinned/latest 모두 REST에서 resolver 테스트; MCP raw-key pinned context parity 확인.
4. **사람용 cookie 세션·정적 `/s`·만료/철회/중단** — 코어 기준 완료. 반복 share 교환, cookie flags, context, export scope 거부, logout, 부모 철회 및 site pause/resume 테스트. `/s` 정적 라우트 테스트 포함.
5. **타입 audience·프로젝트/snapshot 격리·credential 누출 방지** — 코어 테스트 완료. share 원문 일반 exchange/Bearer 거부, 임의 project 및 다른 snapshot 거부, 응답/오류/log/URL의 원문 부재 검사 포함.
6. **Additive DDL·public role grant·v1/trigger 호환** — 완료. 구형 컬럼을 제거한 synthetic schema에서 runtime init 복구, v1 행/ACL 기본값과 기존 immutability trigger 확인. 공용 role의 추가 APIKey counter/last-used 업데이트 권한은 제한 컬럼 grant로 부여.
7. **operation_id publish retry** — 완료. 동일 ID/동일 payload 동일 response, 변경 payload 409 검증.
8. **독립 시험 DB·명령/결과 기록** — 완료. 아래 명령 결과 기록. 운영 DB·실제 문서·실제 공개 활성화·서비스 재시작·push·배포는 수행하지 않음.

## 검증 증거

- RED: 신규 control v2 테스트는 구현 전 `POST /control/v2/credentials`에서 `404 Not Found`로 실패. 발급 구현 후 통과.
- RED: reusable exchange test는 consumed 키의 두 번째 교환에서 `AuthenticationError: unauthorized`로 실패. 타입별 교환 허용 후 통과.
- RED: operation ID 테스트는 같은 payload retry가 다른 snapshot UUID/version을 반환해 실패. operation receipt 적용 후 같은 response, payload 변경 `409` 통과.
- RED: runtime migration 테스트는 신규 컬럼이 빠진 구형 schema에서 신규 권한 grant 시 `UndefinedColumnError`로 실패. additive DDL 복구 후 통과.
- RED: `/s` test는 route 추가 전 `404`로 실패. 정적 shell mapping 후 통과.
- 최종 회귀:
  ```text
  CONTEXT_GATEWAY_TEST_DATABASE_URL='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_v2_core_20261119_b9f28799' TTAKKARI_TEST_DB='test_cg_v2_core_20261119_b9f28799' PGPORT=5432 PYTHONPATH=context_gateway/src ../back/.venv/bin/python -m pytest -q context_gateway/tests
  ........................................ [100%]
  44 passed in 3.62s
  ```
- Python/Ruff:
  ```text
  ../back/.venv/bin/ruff check context_gateway/src/context_gateway context_gateway/tests
  All checks passed!
  ../back/.venv/bin/ruff format --check context_gateway/src/context_gateway context_gateway/tests
  28 files already formatted
  ../back/.venv/bin/python -m compileall -q context_gateway/src/context_gateway
  exit 0
  ```
- Diff hygiene: `git diff --check` → exit 0. Tested base revision: `978cb5ace5115743b32f375ce065120e8d068d82`.

## 미검증 및 경계

- 브라우저 UI에서 fragment token을 메모리로 옮기고 `history.replaceState`로 지우는 동작은 `context_gateway/web/` 소유권 때문에 수정·실행하지 않음. `/s`는 정적 shell 라우트까지만 검증.
- 실제 배포 role이 이 runtime init에서 권한을 적용하는지 운영 DB에서는 확인하지 않음. synthetic test DB에서 DDL 및 role grant 코드를 검증.
- 작업 트리에 별도 Private worker 변경이 함께 나타남. 이 코어 작업에서 Private 경로는 읽거나 수정·검증하지 않았음.
- 현재 변경은 미커밋·미푸시·미배포. 공개 활성화 및 실제 운영 동작은 미실행.
