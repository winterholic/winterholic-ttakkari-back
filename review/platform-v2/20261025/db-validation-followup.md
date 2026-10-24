# 격리 DB 검증 경로 교정과 실제 migration 왕복

2026-10-25 · root Codex 명령 샌드박스 · 시작 후보 `d31e3a097b0739f4e9e638e3333456548574bd43`

**기존 migration 실패 9건을 현재 후보에서 통과시켰고, library·실행부 6파일 161개 검사를 실제 PostgreSQL에서 통과시켰다.** 단일 merge head와 main→V2 library 보존, 공통 조상까지 내렸다 다시 올리는 schema/합성 데이터 왕복도 실행했다. 전체 V2 완료나 운영 전환 성공을 뜻하지 않는다.

## 사용한 경로

이번 Run에 이미 주입된 `TTAKKARI_TEST_DB`가 있었다. URL 값을 출력하거나 인증 파일을 읽지 않았다. loopback 및 test DB 이름, 운영 DB `ttakkari`가 아님을 검사하고 읽기 전용 접속으로 PostgreSQL **17.11 (Homebrew)**을 확인했다. 새 `initdb`, 다른 engine을 통한 shmget 거부 우회, 운영 DB·proxy·설정 조작은 하지 않았다.

현재 checkout에는 `.venv`가 없어 기존 `/back/.venv`로 workspace 내부 symlink를 만들었다. 이전 전체 gate와 같은 interpreter 경로다. 이 symlink는 테스트 실행 환경이며 새 설치·배포가 아니다. 실제 왕복은 이 Run의 테스트 클러스터 안에 UUID로 만든 전용 DB 한 개만 사용했고 마지막에 삭제했다.

## 실패 원인 확정과 최소 교정

1. 대표 game migration은 `tests/test_game_migration.py` → `test_meeting_lineage_migration.migrate()` → Alembic 자식에서 exit1, **1 failed in 0.84s**였다.
2. direct asyncpg 연결은 기존 `patch_asyncpg_test_port()`가 주입된 test URL의 port/user/password를 유지한다. 반면 자식용 `pg_sandbox.localhost_database_url()`는 `localhost:PGPORT`만 만들어 계정 정보를 버렸다.
3. 별도 소유 DB에서 `.venv/bin/alembic current`를 실행해 stderr를 확보했다. 실제 port는 같고 managed URL에는 계정/비밀번호가 있었지만 helper URL에는 없었다. 마지막 오류는 **`password authentication failed for user "winterholic"`**이다. 과거 보고서의 인증 불일치 추론을 이 환경에서 실행 증거로 확인했다. [비밀값을 제거한 원문](db-child-auth-diagnostic.json).
4. helper는 명시적 test URL의 계정·port·query를 보존하고 DB 이름만 바꾼다. 명시 URL이 없을 때 기존 session-port 동작을 유지한다. 운영 DB와 원격 host는 거절한다. 제품 접속/인증 코드는 수정하지 않았다.
5. 회귀 5case를 먼저 추가했다. **4 failed, 1 passed in 0.10s** → 교정 후 **5 passed in 0.06s**. 인코딩된 password, query, 명시 port 우선, 기존 fallback, 운영/원격 거절을 검사한다. fake URL만 사용하며 비밀값은 없다.
6. 인증을 교정하자 기존 9건 중 **8 passed / 1 failed in 12.07s**였다. 남은 0048 검사는 `head`까지 올리면서 0048의 두 컬럼만 추가됐다고 주장했다. 현재 head에는 합법적인 0069 `queue_state/claimed_at`도 있다. 이 특정 검사의 upgrade를 `0048_game_active_build`로 고정했다. 최신 head에 대한 다른 검사와 기존 assertion은 보존했다.

과정 중 Google 검사 파일명을 잘못 지정해 한 호출은 **no tests ran**이었다. 첫 범위 고정 patch가 다른 `head` 호출에 적용된 실수도 diff에서 확인해 되돌리고 의도한 0048 검사만 교정했다. 그 중간 **8 pass / 1 fail in 11.79s** 기록은 [mistargeted](db-migration-nine-mistargeted.txt)로 구분했다. 이를 성공 증거로 쓰지 않는다.

## 실행 결과

공통: `TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src:. .venv/bin/pytest -q -p no:cacheprovider --tb=short`.

| 명령의 선택 범위 | 결과 | 원문 |
|---|---|---|
| `tests/test_game_migration.py tests/test_google_account.py tests/test_knowledge_migration.py tests/test_meeting_lineage_migration.py tests/test_retire_clovanote_migration.py -k migration` | **9 passed, 25 deselected in 12.30s** | [migration9](db-migration-nine-green.txt) |
| `tests/test_artifact_area.py tests/test_background_consumer_migration.py tests/test_game_build_handoff_migration.py tests/test_background_consumers.py tests/test_game_build_queue.py tests/test_tts_executor_isolation.py` | **161 passed in 16.19s** | [library/executor](db-library-executor-integration.txt) |
| `PYTHONPATH=src:tests ... --noconftest -o addopts='' tests/test_pg_sandbox.py -k 'not real_cluster'` | **39 passed, 2 deselected in 0.22s** | [helper regression](db-child-url-regression.txt) |

39개 중 실제 initdb/kill/reap을 수행하는 `real_cluster` 2개는 알려진 sandbox 거부를 반복하지 않기 위해 선택하지 않았다. 선택 제외는 제품 검사 skip 추가가 아니고 명령 범위다. 161개에는 합성 TTS 엔진/가짜 외부 backend가 있으므로 실제 모델·Chrome 검증이 아니다. 서로 다른 범위의 숫자를 전체 gate 통과 수로 합산하지 않는다.

## 실제 Alembic 6단계 왕복

실행: `TTAKKARI_NO_ENV_FILE=1 PYTHONPATH=src:. .venv/bin/python review/platform-v2/20261025/db_roundtrip_probe.py`.

1. 전용 DB를 공통 조상 `0067_collector_outbox_retry`로 upgrade하고 전체 public column schema를 저장했다.
2. `0068_library_intent`로 올린 뒤 synthetic 업로드와 사용자가 보관한 AI 파일 2행을 넣었다.
3. `head`로 올려 version row가 `0071_library_platform_v2_merge` **한 개**임을 확인했다. library의 source/origin/area/keep_reason/keep_evidence/category/sha256 해시는 그대로였다. main→V2 upgrade가 library migration을 재실행해 사용자의 보관을 취소하지 않았다.
4. `downgrade 0068_library_intent`의 실제 version row는 **`0068_library_intent`와 `0070_background_consumer_queue` 두 개**였다. V2 컬럼도 남았다. 따라서 이 명령을 전체 DB rollback이라고 부르면 안 된다. 기존 복수 head 거절 계약과 맞는다.
5. 공통 조상0067까지 내리자 version row 한 개 및 처음과 같은 column schema가 됐다. 기존 id/filename/source/area/sha256/size/created_at 해시도 보존됐다.
6. 다시 head로 올려 최초 head와 같은 column schema와 단일 version row를 확인했다. 전용 DB는 삭제했다.

[실행 JSON](db-roundtrip-result.json)은 단계별 명령·exit·시간·stderr 요약, 실제 version 행과 schema를 모두 담는다. **공통 조상으로 내리면 library의 새 metadata 컬럼은 설계대로 사라진다.** 이를 사용자 보관 의도까지 보존하는 rollback이나 운영 backup restore라고 주장하지 않는다. 이 검사는 column schema와 명시한 기존 데이터 필드의 왕복이다. 실행 후 probe의 Git metadata 조회를 비동기화하고 실패 상태 기록을 보강했다. 실제 migration 경로는 변경하지 않았고 다시 실행하지 않았다.

## 현재 입력과 미완료

변경은 `tests/pg_sandbox.py`, `tests/test_pg_sandbox.py`, `tests/test_game_migration.py` 3개다. 제품 `src` 변경은 없다. [P1 후속 해시/분류](p1-db-correction-manifest.json)는 이전 원장을 덮어쓰지 않고 이 3개 입력만 갱신했다. 테스트 소스 333개 중 331개 분류 재사용, 2개만 재분류, 정적 함수 정의 3,889다. runtime case 수가 아니다.

`ls -l /Users/winterholic/.ttakkari-backups/latest.json`은 `No such file or directory`였다. 이 정확한 기본 경로의 최신 영수증 부재만 확인한 것이며 다른 위치의 백업 존재 여부를 판단하지 않는다. 운영 snapshot 복원·실시간 소유권·비교 성능(P0), 현재 후보 전체 gate와 독립 residual 검증, FND1 자동 전송 장벽, 실제 모델/MCP/재개·장기 Run·보안·사용자 E2E·Core 무배포/P4·P6는 미완료다. 알려진 process-list sandbox 거부를 변경 없이 반복하지 않았다. main push와 배포 예약은 보류한다.

과거 전체gate의 공통 account-selection assertion도 현재 후보에서 해당 1개만 실행했다: `tests/test_account_selection.py::test_choose_prefers_idle_account_then_priority` → **1 passed in 0.50s** ([원문](db-account-selection-current.txt)). 단독 통과이며 전체 suite에서의 순서 의존성 해소나 과거 실패 원인 확정을 뜻하지 않는다.
