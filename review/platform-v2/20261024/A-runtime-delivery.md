# 따까리 플랫폼 V2 감사 및 설계 보고서 (Worker A: Runtime / Delivery / Architecture — 독립 검증 교정판)

- 작성 및 교정 시각: `2026-10-24T22:16:00+09:00`
- 담당: Worker A (Runtime, Delivery, Architecture 판단 / 세션 ID `e65430e3-0775-4e2c-910a-5050cdb7d216`)
- 반영한 독립 검증 보고서:
  - 검증 워커 `1d994e50-c900-4f90-9975-1ff082522a35` 커밋 `93af9ef6e202b95883d73c74d710e1775cb402b9`
  - `/private/tmp/back-platform-v2-a-runtime-delivery-verification-20261024/review/platform-v2/20261024/A-runtime-delivery-independent-verification.md`
  - `/private/tmp/back-platform-v2-a-runtime-delivery-verification-20261024/review/platform-v2/20261024/A-runtime-delivery-independent-verification-evidence.json`
- 감사 대상 정본 SHA (`origin/main`): `c8f10156845d007ec6496a65d6d8417be19260bd` (`src/ttakkari` 트리 SHA `f2e5572cbc38e4234157be9c4c83090a1358be83`)
- 공유 작업 트리(`/Users/winterholic/development/projects/winterholic-ttakkari/back`) 로컬 HEAD: `607f8bda0df7dbdd458e4f0e54d703ce9edd3def`
  - `merge-base(607f8bda, c8f10156)` = `2522d78e29b14ffb29d8536bcf833d832b4f87e1`
  - 양방향 분기 토폴로지: **`2 ahead / 118 behind`** (`607f8bda` 고유 커밋 2개: `607f8bd`, `66c8402`; `c8f10156` 고유 커밋 118개)
  - 2-점 비교(`git diff --shortstat 607f8bda c8f10156`): **`366 files changed, 86,213 insertions(+), 339 deletions(-)`**
- 격리 작업 트리: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a` (`audit/platform-v2-a-runtime-delivery-20261024`)
- 기계 판독 증거 및 재현/검증 스크립트:
  - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery-evidence.json`
  - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-extract-runtime-delivery-evidence.py`
  - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/test_a_runtime_delivery_evidence.py`

---

## 0. 독립 검증(`93af9ef6`) 반영 8대 필수 교정 요약

초회 감사(`fddad5e152754f5f1e7ac5eac07ec26b453d6987`)에 대한 독립 검증(`93af9ef6`)에서 지적된 수치·해석·범위 오류를 아래와 같이 전수 교정하였다.

1. **교정 1 (Git 토폴로지·테이블 수·스냅샷 바인딩)**:
   - `607f8bda`와 `c8f10156`의 관계를 기존 `111 behind / 253 files changed`에서 실제 `merge-base`(`2522d78e`) 기준 **`2 ahead / 118 behind` 양방향 분기** 및 2-점 diff **`366 files changed, 86,213 insertions(+), 339 deletions(-)`**로 교정하였다.
   - `0063`~`0067` 마이그레이션이 신설한 테이블 수를 기존 `21개`에서 실제 **`18개`(`5 + 1 + 8 + 4 + 0`)**로 교정하였다. `0064_run_executor.py`는 4개 테이블이 아니라 **`runtime_process_groups` 1개 테이블**만 신설하고, 기존 `runtime_generations`(`role`, `protocol`, `execution_mode`) 및 `runtime_control`(`executor_generation`)에 컬럼을 추가하며 4개 PL/pgSQL 트리거(`guard_executor_control`, `guard_executor_api`, `guard_executor_lease`, `route_executor_command`)를 설치한다.
   - `A-extract-runtime-delivery-evidence.py`에서 모든 상수 기입을 제거하고 실제 Git 명령과 파일 파싱으로 동적 계산하며, `verify_import_origin()` 및 `verify_snapshot_binding()`으로 잘못된 repo root, 타 체크아웃 import, dirty source tree, 트리 해시 불일치, 파싱 실패를 즉시 거부(fail-closed)하도록 구현·테스트하였다.
2. **교정 2 (과거 영수증 vs 현재 직접 관측, Capabilities 범위 분리, 실패 통계 34/56 철회)**:
   - `9f82c9c1` 성공 배포(`4a6e09a8`, Slot 1 `8792`, 세대 `5ef462e5`)와 `c8f10156` 실패 배포(`e15536bc`)는 **과거 배포 영수증(`historical_receipt`)**이고, `e817fc19` 역시 과거 이슈 스택트레이스(`LOG-68`) 기록일 뿐 **현재 실시간 실행 중인 API 리비전/슬롯/세대나 supervisor 프로세스의 직접 관측값이 아님(`unknown / 확인 필요`)**을 명확히 분리하였다.
   - `execution_capabilities`에서 `host`(`blocked=[]`)와 `codex` 샌드박스(`blocked=[gpu_metal, llama_list_devices, process_list, sysctl_read]`)를 구분하고, `antigravity`/`muse`의 `blocked=[]`는 8개 항목 전체가 `stale_items`인 과거 캐시(`확인 필요`), `claude`/`qwen-local`은 미측정(`확인 필요`)으로 기록하였다.
   - 초회 보고의 배포 실패 집계(`34그룹 / 56회`)를 **전면 철회**하였다. 전체 조회(`34 groups / count 합계 65회, 10/14~10/24`)와 해당 구간 raw paged 조회(`29 groups / 58 raw events: pytest 56, archive 2`) 간 집계 기준이 다르므로, 전체 raw ID 덤프 없이는 기간 집계를 `수치 검증 불가 / 확인 필요`로 낮추고 `ops_list_deploys` 50건 상한 너머 전체 배포 모수도 `unknown`으로 기록하였다.
   - `9f82c9c1`의 `1,045.97s`(전체 4,473 passed 완주)와 `c8f10156`의 `172.23s`/`063002a1`의 `985.34s`(`-x` 첫 실패 조기 중단)가 동등한 벤치마크가 아님을 명시하였다.
3. **교정 3 (좁은 DB-Free 표본 `35 passed in 3.34s` 및 5초 보장 주장 철회)**:
   - `PYTHONPATH`를 worktree `src`로 고정한 5개 파일 DB-free(`--noconftest`) 결과를 **`35 passed in 3.34s`**로 정정하고, 기존 `39 passed in 5.00s`와 "전체 계약 게이트 5초 보장" 주장을 **철회**하였다.
4. **교정 4 (`EvidenceCacheKey v2` 및 `ValidationReceipt v2` 엄격화)**:
   - 경로만 넣던 v1을 폐기하고, 후보 소스 트리 전체 해시, 전이 의존성 클로저 해시(불완전/동적/unknown 시 즉시 `broad_fail_closed`), 선택된 테스트 파일 해시, `tests/conftest.py` 및 헬퍼 해시, selector 인자, `pyproject.toml` + `uv.lock`, Python 패치 버전/플러그인/환경 변수, `models.py` + `alembic/env.py` + 67개 마이그레이션 파일 각각의 내용 해시, `generation-contract.json` + `run-executor-manifest.json`, 그리고 provenance/trust(`import_origin_file` 검증, TTL, HMAC 서명)를 포함하는 `v2` 명세와 1바이트 변조 거부 테스트를 구현하였다.
5. **교정 5 (ADR 0036 활성화 경계, Reconcile vs Legacy Startup Resume 구분, 상태 전이 표)**:
   - `executor_reconcile.py`(`NoSuchProcess`만 `ABSENT`, `AccessDenied`/`OSError`는 `UNKNOWN` 보존, lock 아래 신원·리스 재확인 후 신호, `interrupted` 마감 및 무-replay)와 **레거시 기동 경로(`main.py:201` → `runs/manager.py:1941` `resume_interrupted()`: 세대 런타임 비활성 시 `error == "서버 종료로 중단"`인 `interrupted` Run을 최대 `MAX_RESTART_RESUME_ATTEMPTS = 3`회까지 백오프 재큐잉)**를 엄격히 구분하고, 소유권·펜스·리스·드레인·취소·재시작·은퇴 상태 전이를 표로 정의하였다.
6. **교정 6 (TTS / Creative / Qwen / Side-Effects 과장 철회 및 최소 공용 기반 + 인수 시나리오)**:
   - "현재 `llama-server`가 2~3개 중복 기동되어 Swap 포화를 일으켰다"는 인과 단정을 철회(현재 스냅샷엔 `pid=44129` 1개만 관측됨)하고, "모든 파일/외부 효과가 이미 `tmp+fsync+sha256+os.replace`와 DB 펜스로 완전 격리되어 있다"는 과장도 철회(`_write_checkpoint`는 `.tmp`+`replace`뿐이고 `fsync`/해시 없으며 바깥 Creative 태스크에는 DB 펜스 없음)하였다.
7. **교정 7 (독립 모듈 릴리스 미구현 설계/목표 구분, 스키마 확장 호환성, Creative 후보 `provisional`)**:
   - `module-releases`, `active.json`, 버전드 디스패처, `<1s` 롤백, `Core 재배포 0회`는 현재 미구현된 **설계 목표(Target Architecture) + 인수 시나리오**로 구분하고, `Creative Studio` 1순위 후보는 이름 기반 `fan_in=4`만으로 FK/리스/부수효과 분리가 입증되지 않았으므로 **`provisional` (잠정 1순위)**로 격하하였다.
8. **교정 8 (상태 원장 시간 고정 이력 분리 및 비순환 선행조건)**:
   - Worker B(`86373ce`/`2cc` 검증 `failed`, `7cf` 분모 확장, `d492` 교정 중)와 Worker C(`fed2c630` 검증 `failed` 후 교정 중)의 최신 검증 상태를 반영하고, `P0` 미검증 항목을 **(Gate B) 운영 전환 및 P2 최종 운영 서명의 선행조건**으로 두어 **(Gate A) P3 격리 코드 구현 승인**과 분리함으로써 순환 선행조건을 제거하였다.

---

## 1. P0 운영·복구·성능 기준선 실측 결과 (증거 수준 엄격 분리)

보호 경로(`~/.ttakkari`) 직접 스캔 없이 공식 MCP 도구(`backend_deploy_status`, `ops_list_deploys`, `ops_deploy_log`, `ops_list_issues`, `ops_get_issue`, `execution_capabilities`, `server_resources`, `read_session`)로 수집한 데이터를 **`direct_current_observation`(현재 직접 관측)**, **`historical_receipt`(과거 영수증/로그)**, **`unknown / 확인 필요`**로 엄격히 분리한다.

### 1.1 운영·릴리스·프로세스·실행 능력 기준선

| 항목 | 관측값 및 내용 | 근거 도구 및 시각 | 증거 분류 |
| :--- | :--- | :--- | :--- |
| **가장 최근 성공 배포 영수증** | 배포 `4a6e09a8-c49f-4c2f-9a6c-c0d83fe5824a`: 리비전 `9f82c9c16cd604e81ddf43180b0be2e895ec85cb`, 세대 `5ef462e5-d7f7-40e7-bf27-1d4bf19e1d91` (Slot `1`, Port `8792`), 직전 세대 `e1eb4ad0` (`4f341c54`, Slot `0`, Port `8791`, `2026-10-23T10:43:06Z` 은퇴) | `backend_deploy_status(4a6e09a8)`, `ops_deploy_log(4a6e09a8)` (`2026-10-23T10:43:06Z`) | **`historical_receipt`** |
| **가장 최근 실패 배포 영수증** | 배포 `e15536bc-c7f9-4e1a-89cb-95e2f91192e5`: 리비전 `c8f10156845d007ec6496a65d6d8417be19260bd`, `pytest` `172.23s` 시점 조기 중단(`-x`)으로 전환 전 실패 | `backend_deploy_status(e15536bc)`, `ops_deploy_log(e15536bc)` (`2026-10-23T11:10:22Z`) | **`historical_receipt`** |
| **현재 실시간 서빙 중인 API 리비전·슬롯·세대 ID** | `4a6e09a8` 성공 이후 후속 성공 배포 영수증은 없으나, 보호 경로 및 실시간 `/api/version` 무인증 프로브 제한으로 현재 순간 서빙 중인 정확한 리비전/슬롯은 직접 관측하지 않음 | 실시간 프로브 미수행 | **`unknown / 확인 필요`** |
| **배포 감독(Supervisor) 리비전** | 과거 이슈 `LOG-68` 스택트레이스에 `/Users/winterholic/.ttakkari/deploy/releases/e817fc19471bdc48054158cada4a99466f7eaa6f/src/ttakkari/deployment.py`가 기록되어 있으나, `~/.ttakkari/deploy/supervisor.json` 원문 및 현재 감독 프로세스의 실시간 실행 리비전은 보호 경로 밖임 | `ops_get_issue(LOG-68)` | 과거 스택트레이스는 **`historical_receipt`**, 현재 실시간 감독 리비전은 **`unknown / 확인 필요`** |
| **호스트 프로세스 및 자원 스냅샷** | `python3.13` (`pid=44104`, `kind=backend`, RSS `374 MiB`), `llama-server` (`pid=44129`, RSS `7.20 GiB`, **단 1개 인스턴스만 관측됨**); CPU 18코어 `17.6%`, GPU 40코어 `0.0%`, RAM 64GiB 중 `42.1%~43.1%`, Swap `31.5~31.7 / 33.3 GiB` (`94.8%~95.3%`), `memory_pressure=normal` | `server_resources` (`2026-10-24T12:20:12Z` 및 `12:51Z`) | **`direct_current_observation`** (단일 시점 스냅샷) |
| **실행 능력 (`host` vs `codex` vs 기타)** | - `host` (`2026-10-24T12:20Z` probe): `blocked=[]`, 8개 전체 `available`<br>- `codex` (`2026-10-24T12:51Z` 샌드박스 probe): `blocked=[gpu_metal, llama_list_devices, process_list, sysctl_read]`, `available=[spawn_process, network_loopback, network_outbound, writable_paths]`<br>- `antigravity` (`10-21 18:29`), `muse` (`10-21 17:55`): `blocked=[]`이나 8개 전체 `stale_items`<br>- `claude`, `qwen-local`: `not measured` | `execution_capabilities` | `host`·`codex`는 **`direct_current_observation`**, `antigravity`·`muse`는 **stale(`확인 필요`)**, `claude`·`qwen-local`은 **`unknown / 확인 필요`** |
| **UI 워커 부작용 대조** | 세션 `6e33860e-3773-48bf-8df5-a3dc2c330a3e`는 `front` 저장소만 수정해 `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf` 푸시 완료. `back` 수정 및 배포 예약 0건 | `read_session(6e33860e-3773-48bf-8df5-a3dc2c330a3e)` | **`historical_receipt`** |

### 1.2 Baseline Drift (`607f8bda` ↔ `c8f10156`) 정밀 대조

공유 작업 트리(`/Users/winterholic/development/projects/winterholic-ttakkari/back`, 로컬 `HEAD` = `607f8bda0df7dbdd458e4f0e54d703ce9edd3def`)와 정본 `origin/main`(`c8f10156845d007ec6496a65d6d8417be19260bd`)의 실제 Git 토폴로지 및 스키마 차이는 다음과 같다 (`A-runtime-delivery-evidence.json`의 `baseline_drift_607f8bda_to_c8f10156` 참조):

1. **양방향 분기(Divergent Graph) 토폴로지**:
   - 공통 조상(`git merge-base 607f8bda c8f10156`): `2522d78e29b14ffb29d8536bcf833d832b4f87e1`
   - `git rev-list --left-right --count 607f8bda...c8f10156`: **`2 ahead / 118 behind`**
     - `607f8bda`에만 있는 로컬 커밋 2개: `607f8bda0df7dbdd458e4f0e54d703ce9edd3def`, `66c840200b8c529a1a4cf9e06704b30f92b4e5a2`
     - `c8f10156`에만 있는 `origin/main` 커밋: **118개**
   - 2-점 비교(`git diff --shortstat 607f8bda c8f10156`): **`366 files changed, 86,213 insertions(+), 339 deletions(-)`**
   - 3-점 비교(`git diff --shortstat 607f8bda...c8f10156`, `merge-base` → `c8f10156`): **`366 files changed, 86,257 insertions(+), 319 deletions(-)`**
2. **`src/ttakkari` 파이썬 파일 및 라인 수 변화**:
   - `607f8bda`: **`321`개 파일 / `71,985`줄**
   - `c8f10156`: **`385`개 파일 / `94,439`줄** (순증 **`+64`개 `.py` 파일, `+22,454`줄**)
3. **Alembic 마이그레이션(`62`개 → `67`개, `+5`개) 및 실제 신설 테이블 수(`+18`개)**:
   - `0063_collector_stage_items.py`: **5개 테이블 신설** (`collector_source_policies`, `collector_host_state`, `collector_fetch_cache`, `collector_items`, `collector_dedupe_bands`)
   - `0064_run_executor.py`: **1개 테이블 신설** (`runtime_process_groups`), 기존 테이블 컬럼 추가 (`runtime_generations.role`, `runtime_generations.protocol`, `runtime_generations.execution_mode`, `runtime_control.executor_generation`), **4개 PL/pgSQL 트리거 설치** (`guard_executor_control`, `guard_executor_api`, `guard_executor_lease`, `route_executor_command`)
   - `0065_collector_delivery.py`: **8개 테이블 신설** (`collector_sources`, `kb_document_access`, `kb_observations`, `collector_deliveries`, `collector_daily_budgets`, `collector_reviews`, `collector_quality_models`, `kb_passage_vectors`)
   - `0066_collector_budget.py`: **4개 테이블 신설** (`collector_budget_buckets`, `collector_budget_reservations`, `collector_cache`, `collector_runtime_flags`)
   - `0067_collector_outbox_retry.py`: **0개 테이블 신설** (기존 `collector_outbox` 테이블에 `attempts`, `not_before`, `last_error` 컬럼 및 인덱스 추가)
   - **합계**: `5 + 1 + 8 + 4 + 0 = 18`개 신규 테이블 (초회 보고의 `21개` 및 `0064` 4테이블 주장은 철회).

### 1.3 배포 실패 이력 및 단계별 소요 시간 (`historical_receipt` vs `확인 필요`)

1. **배포 실패 이슈 집계 (`수치 검증 불가 / 확인 필요`로 하향 및 초회 `34/56` 철회)**:
   - 초회 보고에서 제시했던 `34개 이슈 그룹 / 56회 실패 (pytest 33/54, archive 1/2)` 수치는 집계 기준 불일치로 **전면 철회**한다.
   - 독립 검증 대조 결과:
     - `ops_list_issues(source="deploy", status="all")` 전체 요약 조회(`2026-10-14`~`2026-10-24`)는 `34 issue groups / count 합계 65회`이다.
     - 반면 `2026-10-16`~`2026-10-23` 구간의 raw paged 이벤트 조회(`50 + 8`건)는 `29 issue groups / 58 raw events` (`deploy:pytest` 56건, `deploy:archive` 2건; 시각 범위 `2026-10-16T11:08:43Z`..`2026-10-23T11:10:22Z`)이다.
     - 또한 `ops_list_deploys`는 상태별 최근 `50건` 상한이 있어 전체 배포 시도 모수(denominator)는 `unknown / 확인 필요`이다.
   - 따라서 raw 이벤트 ID 전체 덤프 없이는 기간별 정확 집계를 `수치 검증 불가 / 확인 필요`로 유지하되, **관측된 배포 실패의 대다수가 `deploy:pytest` 단계에서 발생한다**는 구조적 사실만 채택한다.
2. **완주 배포 영수증과 조기 중단(`-x`) 실패 영수증의 구분 (동등 벤치마크 아님)**:
   - **전체 스위트 완주 영수증 (`4a6e09a8`, SHA `9f82c9c1`)**:
     - 예약 생성(`2026-10-23T10:16:49.556741+00:00`) ~ 은퇴 완료(`10:43:06.032849+00:00`): 총 벽시계 **`1,576.48초` (26분 16초)**.
     - `git archive` + `uv sync --frozen`: 약 **`48초`** (`Installed 215 packages in 25.63s`).
     - `pytest -q --timeout=120 -x` 전체 완주: **`1,045.97초` (17분 25.97초)** (`4473 passed, 8 skipped, 2 xfailed, 5020 subtests passed`, DB selective reset `2,822`회 / `106`개 테이블).
     - 직전 완주 배포 `34ee75c6`(`b2faeb5d`) 역시 `pytest` 완주에 **`1,020.57초`** (`4455 passed`)가 소요되었다.
   - **첫 실패 조기 중단(`-x`) 영수증 (완주 시간과 직접 비교 금지)**:
     - 배포 `794447f3` (SHA `063002a1`, `LOG-194`): Creative Studio 자막 줄바꿈 및 장면 프레이밍 수정 후 `pytest -x`가 **`985.34초`** 지점(`3,327 passed`)에서 `unit_tests/test_creative_v2_lease_handoff.py::test_scene_generation_requests_bounded_lease_wait_from_controller` 1건(`assert 349 == 324`) 실패로 조기 중단됨.
     - 배포 `e15536bc` (SHA `c8f10156`, `LOG-195`): Desk 세션 연속성 검사(`PROMPT_VERSION = "harness-v3-topic-boundary"`) 수정 후 `pytest -x`가 **`172.23초`** 지점(`1,112 passed`)에서 `tests/test_desk_qwen_service.py::test_desk_request_goes_to_the_managed_server_of_this_process` 1건(`assert 'harness-v3-topic-boundary' == 'harness-v2'`) 실패로 조기 중단됨.

### 1.4 백업 및 복구 리허설 기준선 (`implemented` / 운영 실행 이력은 `unknown`)

- **구현된 코드 (`implemented`)**:
  - `scripts/backup_db.sh`: `asyncpg` `repeatable_read` 읽기 전용 트랜잭션에서 `pg_export_snapshot()`과 테이블별 행 수를 구하고 동일 스냅샷으로 `pg_dump -Fc --snapshot` 실행 및 `~/.ttakkari-backups/latest.json` 갱신.
  - `scripts/restore_rehearsal.sh`: 대상 DB명에 `test`·`restore`·`rehearsal` 강제, loopback PostgreSQL만 허용, `sha256` 검증 후 임시 DB 복원 및 테이블 행 수 전수 대조 후 삭제.
  - `scripts/snapshot_data.sh` 및 `src/ttakkari/backup_status.py` (`GET /api/system/backup-status`).
- **운영 실제 실행 여부 (`unknown / 확인 필요`)**:
  - `~/.ttakkari-backups/latest.json`은 에이전트 보호 영역 밖이며 MCP 도구로 직접 조회할 수 없다. 따라서 최근 백업·복원 리허설 성공 시각은 `unknown / 확인 필요`로 유지한다.

---

## 2. 런타임·배달 하위 시스템 정밀 대조 및 상태 전이 정의

### 2.1 하위 시스템 상태 요약표

| 하위 시스템 | 핵심 코드 및 마이그레이션 경로 | 코드/테스트 상태 | 설정·계약 기본값 | 운영(Live) 상태 분류 |
| :--- | :--- | :--- | :--- | :--- |
| **세대 배포 컨트롤러 및 3슬롯 (`8791/8792/8793`)** | `src/ttakkari/generation.py`, `generation_service.py`, `generation_deployment.py`, `deployment.py`, `deploy_gate.py` | `implemented` / `tested` (`tests/test_generation_deployment.py`) | 운영 `deploy_strategy="generations"`, `runtime_generation=True` | 과거 영수증(`4a6e09a8`)에서 활성 확인됨 (`historical_receipt`), 현재 실시간 슬롯은 `unknown / 확인 필요` |
| **ADR 0036 독립 RunExecutor** | `src/ttakkari/executor.py`, `alembic/versions/0064_run_executor.py` | `implemented` / `tested` (`unit_tests/test_executor_migration_contract.py`) | `feature-off`: `deploy/generation-contract.json` `run_executor.activation="disabled"`, `Settings.run_executor_mode="local"` | **`feature-off`** (독립 `RunExecutor`만 비활성일 뿐 세대 배포 자체는 활성) |
| **ADR 0036 PG 프로세스 그룹 원장 및 펜싱 Reconcile** | `src/ttakkari/executor_reconcile.py`, `src/ttakkari/models.py` (`RunProcessGroup` → `runtime_process_groups` 테이블) | `implemented` / `tested` (`unit_tests/test_executor_reconcile.py`) | `feature-off` (`run_executor` 비활성으로 미호출) | **`feature-off`** |
| **레거시 기동 시 중단 Run 재개 (`resume_interrupted`)** | `src/ttakkari/main.py:201` → `src/ttakkari/runs/manager.py:1941-2023` | `implemented` / `tested` | `not runtime_generation_enabled()`일 때만 기동 시 호출 | 세대 런타임 활성 시 미호출되나, 비활성/단독 기동 경로에서는 **최대 3회 자동 재큐잉 코드가 잔존함 (주의 필요)** |
| **ADR 0066 ChangeSet / Worktree / MergeQueue** | `src/ttakkari/delivery/{worktree,provenance,diff_guard,repair_policy,validation,merge_queue,cli}.py`, `0062_delivery_change_sets.py` | `implemented` (부분) / `tested` (`tests/delivery/test_*.py`) | `feature-off` (`Settings.delivery_pipeline_enabled=False`) | **`feature-off`** (수동 CLI만 존재, 배포 게이트 미연동) |
| **로컬 TTS 큐 (`tts_jobs`)** | `src/ttakkari/tts/jobs.py`, `src/ttakkari/tts/service.py` | `implemented` (드레인 결함 내포) / `tested` | `local_tts_enabled=True` | 코드 콜패스상 `_pending_texts` 메모리 귀속 및 `draining` 강제 실패 결함 존재 |
| **Desk Qwen 관리형 서버** | `src/ttakkari/runs/desk_qwen_service.py`, `src/ttakkari/generation_service.py:232` | `implemented` / `tested` (`tests/test_desk_qwen_service.py`) | `desk_qwen_enabled=True`, `desk_qwen_manage=True`, `desk_qwen_base_url=""` | 현재 호스트 스냅샷에 `llama-server`(`pid=44129`, RSS `7.20GiB`) **1개** 관측됨 (`direct_current_observation`) |
| **호스트 GPU 배타 리스 (`ResourceLeaseManager`)** | `src/ttakkari/host_resource.py`, `host_gpu.py`, `managed_comfy.py` | `implemented` / `tested` (`unit_tests/test_creative_v2_lease_handoff.py`) | `data_dir/host-gpu/lock` + `data_dir/host-resource/lease.json` | 코드 구현 및 단위 테스트 완료 |

### 2.2 ADR 0036 활성화 경계 및 `executor_reconcile.py` vs 레거시 `resume_interrupted()` 정밀 구분

1. **활성화 경계**:
   - `deploy/generation-contract.json`의 `"run_executor": {"activation": "disabled"}` 및 `config.py:109`의 `run_executor_mode = "local"`은 **독립 `RunExecutor` 데몬의 활성화 경계**일 뿐, 세대 배포(`runtime_generation=True`) 자체의 비활성이 아니다.
   - `0064_run_executor.py`는 `runtime_process_groups` 1개 테이블을 신설하고, `runtime_generations`(`role`, `protocol`, `execution_mode`) 및 `runtime_control`(`executor_generation`)에 컬럼을 추가하며 4개 PL/pgSQL 트리거(`guard_executor_control`, `guard_executor_api`, `guard_executor_lease`, `route_executor_command`)로 DB 계층에서 잘못된 역할/펜스의 쓰기를 차단한다.
2. **ADR 0036 `executor_reconcile.py`의 동작 (`src/ttakkari/executor_reconcile.py:86-226`)**:
   - 프로세스 생존 판정(`inspect_process_identity`)에서 **`psutil.NoSuchProcess`만 `ABSENT`로 판정**하고, `psutil.AccessDenied`나 `OSError`는 절대 죽은 것으로 단정하지 않고 **`UNKNOWN`으로 보존(`skipped_unknown`)**한다.
   - 행 잠금(`FOR UPDATE NOWAIT`, `lock_timeout='200ms'`) 획득 후 커널 프로세스 신원 `(pid, process_started_at, pgid)` 및 활성 실행기 리스 `(generation_id, lease_token)`을 재확인한 뒤에만 고아 그룹에 `SIGTERM` → `SIGKILL`을 보낸다.
   - 소유자가 부재(`ABSENT`)한 Run은 `status = "interrupted"` (오류 메시지: `"실행기 재시작으로 중단 — 자동 재개하지 않음"`)로 마감하며 **자동 재실행(replay/resume)을 절대 수행하지 않는다**.
3. **레거시 기동 경로 `RunManager.resume_interrupted()`와의 엄격한 구분 (`src/ttakkari/main.py:201` → `src/ttakkari/runs/manager.py:1941-2023`)**:
   - 반면 `main.py:199-201`은 `if not runtime_generation_enabled():` 분기에서 기동 시 `await run_manager.reconcile_runs()` 직후 **`await run_manager.resume_interrupted()`를 호출**한다.
   - `resume_interrupted()` (`manager.py:1941-2023`)는 `Run.status == "interrupted"`이고 `Run.error == "서버 종료로 중단"`인 Run들을 조회하여, `restart_resume_count < MAX_RESTART_RESUME_ATTEMPTS` (`3`회)이면 새 `Run`(메시지: `"[시스템 자동 재개] 이전 실행이 서버 재시작으로 중단되어 이어서 진행합니다..."`)을 생성하고 `_enqueue()`로 **자동 재큐잉(replay/resume)**한다!
   - 따라서 "코드베이스 전체에서 자동 재실행이 이미 제거되었다"고 주장하면 안 되며, P3 공용 기반 변경에서 `resume_interrupted()`의 무조건 자동 재큐잉을 비활성화(또는 멱등성·소유권 검증 게이트 뒤로 격리)해야 불변 조건("소유권 미증명 작업 자동 재실행 금지")이 모든 기동 모드에서 관철된다.

### 2.3 TTS / Creative / Qwen / Side-Effects 정적 콜패스 사실과 과장 철회

1. **로컬 TTS (`src/ttakkari/tts/jobs.py`, `service.py`, `generation.py`)**:
   - **정적 콜패스 사실**:
     - `jobs.py:42`의 `_pending_texts: dict[uuid.UUID, list[str]] = {}`가 분할된 원문 청크를 API 프로세스 로컬 메모리에만 보관한다 (`tts_jobs` DB 행에는 `text_sha256`, `text_chars`, `chunk_count`, `owner_generation_id`만 저장).
     - `jobs.py:153-154`의 `_guard_loop()`는 `runtime.state == "draining"`일 때 `_fail_queued_draining(runtime.id)`(`jobs.py:178-193`)을 호출하여 해당 세대의 모든 `queued` 작업을 `status="failed"`, `error_code="generation_draining"`으로 강제 실패시킨다.
     - `service.py:130`의 `budget()`은 `generation.work("tts")`를 호출하고, `generation.py:130`의 `enter_work()`는 `self.state != "active"`일 때 `False`를 반환하므로 `draining` 상태에서는 실행 예산을 얻을 수 없다.
2. **Creative Studio V2 자동 자산 생성 (`src/ttakkari/api/creative_v2.py:418`, `creative/v2/asset_generation_managed.py:345-490`)**:
   - **정적 콜패스 사실**:
     - `api/creative_v2.py:418`의 `_ASSET_GENERATION_TASKS[key] = asyncio.create_task(_run_asset_generation(...))`는 `generation.work()`나 `generation_service.py:235-245` `ready()`의 `finalizers`에 등록되지 않는다.
     - 또한 `asset_generation_managed.py:478`에서 ComfyUI 생성 직후 `tts.service.budget(600)`을 호출하므로, 상위 태스크가 도는 도중 API 슬롯이 `draining`으로 전환되면 하위 TTS 단계에서 `draining` 거부로 실패할 수 있다.
   - **과장 철회 (Side-Effects & Checkpoints)**:
     - 초회 보고에서 "모든 파일/외부 부수효과가 이미 `tmp + fsync + sha256 + os.replace`와 DB 펜스로 완전 격리되어 있다"고 서술한 부분은 **철회**한다.
     - 실제 `asset_generation_managed.py:345-363`의 `_write_checkpoint()`는 `.tmp` 파일에 `write_text()` 후 `os.replace()`만 수행하며 **`os.fsync()`나 내용 `sha256` 검증을 수행하지 않고**, 바깥 `_ASSET_GENERATION_TASKS` 비동기 태스크 자체에는 DB `fencing_token` 검사가 없다. 이를 P3/P4에서 보강해야 한다.
3. **Desk Qwen 관리형 서버 (`src/ttakkari/generation_service.py:232`, `runs/desk_qwen_service.py:124-213`)**:
   - **정적 콜패스 사실**:
     - `generation_service.py:232`는 각 API 세대 기동 시 `await qwen_service.start()`를 호출하며, `desk_qwen_enabled=True`, `desk_qwen_manage=True`, `desk_qwen_base_url=""`(기본값)이고 가용 메모리가 `desk_qwen_min_free_memory_gb`(`6.0 GiB`) 이상이면 프로세스별 `llama-server` 자식 프로세스를 스폰할 수 있는 구조다 (`desk_qwen_base_url`이 설정되면 외부 서버 `/health`만 확인).
   - **과장 철회 (Swap 포화 인과 단정 철회)**:
     - 현재 `server_resources` 실측 스냅샷에는 `llama-server`(`pid=44129`, RSS `7.20 GiB`)가 **단 1개만 실행 중**이다. 따라서 "현재 `llama-server`가 2~3개 중복 기동되어 Swap 95.3% 포화를 일으켰다"는 인과 단정은 **철회**하며, 세대 전환 시 중복 스폰 가능성은 코드 구조상 잠재 위험(`potential risk during slot transition`)으로만 기록한다.

### 2.4 런타임 소유권·펜스·리스·드레인 상태 전이 계약표 (State Transition Matrix)

API 세대, 영속 잡 큐(TTS / Creative), 독립 `RunExecutor` 간 작업 수락부터 은퇴까지의 검증 가능한 상태 전이 규칙을 아래 표로 정의한다.

| 전이 단계 | 주체 (Actor) | 선행 조건 (Precondition / Guard) | 원자적 상태 전이 및 펜스/리스 동작 | 실패 / 경합 시 동작 (Fail-Closed) |
| :--- | :--- | :--- | :--- | :--- |
| **1. 신규 수락 (`Admission`)** | Core API (`active` 슬롯) | `runtime.state == "active"` (`enter_admission(kind) == True`) | - DB에 잡/명령 행 생성 (`status='queued'`, `accepted_generation_id=gen.id`, `pinned_module_revision=mod_rev`, `idempotency_key` unique)<br>- TTS는 분할 청크를 `tts_jobs.chunks_json`에 영속 저장 | `runtime.state != "active"`(`standby`/`draining`/`retired`)이면 신규 HTTP 변이 수락 거부 (`503` 또는 활성 슬롯 라우팅) |
| **2. 실행 리스 획득 (`Claim & Fence`)** | `RunExecutor` 또는 소유 세대 워커 | `status IN ('queued', 'pending')` 또는 만료된 리스 | - `fencing_token = coalesce(max_fencing, 0) + 1` 단조 증가<br>- `lease_token`, `owner_generation_id`, `owner_pid`, `owner_process_started_at`, `lease_expires_at = now + ttl` 기록 후 `status='running'` | 동일 `idempotency_key` 중복 시 기존 행 반환; `guard_executor_lease` 트리거가 비활성/불일치 세대의 리스 획득 차단 |
| **3. 기수락 작업 드레인 (`Accepted Work Drain`)** | `draining` 세대 워커 또는 `RunExecutor` | `runtime.state IN ('active', 'draining')` **AND** `job.accepted_generation_id == runtime.id` (또는 독립 Executor 소유) | - `enter_accepted_work(kind, accepted_generation_id)`는 `draining` 상태에서도 **`True` 반환**<br>- `_fail_queued_draining` 금지: 이미 `202 Accepted`된 `queued`/`running` 작업은 완료까지 실행하고 `pinned_module_revision` 워커 유지 | `active_accepted_work > 0`인 동안 `can_retire()`는 무조건 `False` 반환 (`SIGTERM` 전송 보류) |
| **4. 부수효과 및 이벤트 커밋 (`Fenced Write`)** | 실행 중 워커 | 매 DB 반영 직전 `SELECT fencing_token ... FOR KEY SHARE` == `expected_fence` | - 파일 산출물은 `*.tmp` 쓰기 → `os.fsync(fd)` → `sha256` 계산/검증 → `os.replace()` 원자적 교체 후 DB 체크포인트에 `(fencing_token, artifact_sha256)` 기록 | `fencing_token` 불일치 시 즉시 `StaleExecutionFenceError` 발생, DB `rollback()`, 임시 파일 삭제 |
| **5. 취소 (`Cancel`)** | API → Executor / 워커 | 사용자 취소 요청 (`action='cancel'`) | - `run_execution_commands`에 `cancel` 명령 삽입<br>- 소유 워커가 `(pid, process_started_at, pgid)` 일치 확인 후 프로세스 그룹에 `SIGTERM` → 유예 후 `SIGKILL`, `status='cancelled'` 마감 | 신원 불일치 시 커널 신호 전송 금지 (`skipped_mismatch`) |
| **6. 재시작 및 고아 회수 (`Restart / Reconcile`)** | `reconcile_fenced_orphans` | 리스 만료(`lease_expires_at < now`) 또는 소유 세대 `retired` | - `FOR UPDATE NOWAIT` 잠금 아래 `(pid, process_started_at, pgid)` 및 `(generation_id, lease_token)` 재확인<br>- `NoSuchProcess`(`ABSENT`) 확인 시 `status='interrupted'` 마감 (**자동 replay/재큐잉 절대 금지**) | `AccessDenied` / `OSError`는 `UNKNOWN`으로 두어 신호 전송 및 상태 변경 보류 (`skipped_unknown`); 살아있는 리스는 `skipped_live_lease` |
| **7. 세대 은퇴 (`Retire`)** | 배포 컨트롤러 | `runtime.state == "draining"` **AND** `active_admissions == 0` **AND** `active_accepted_work == 0` **AND** `finalizers == 0` | - 슬롯 프로세스에 `SIGTERM` 전송 후 `runtime_generations.state = 'retired'`, `retired_at = now` 기록 | 드레인 타임아웃 초과 시 강제 은퇴 대신 `recovery_pending`으로 전환하여 운영자/오케스트레이터 점검 대기 |

---

## 3. 배포 전역 차단(Global Blocking) 경로와 `EvidenceCacheKey v2` / `ValidationReceipt v2` 명세

### 3.1 현재 배포 파이프라인이 전역 차단을 일으키는 코드 경로

1. **단일 직렬 컨트롤러 잠금 (`src/ttakkari/deployment.py:424-465`, `generation_deployment.py:265`)**:
   - `Controller.serve()`는 `~/.ttakkari/deploy/controller.lock`을 배타 잠금(`flock`)하고 한 번에 단 1건의 `foreground` 잡만 `backend-restart.lock` 아래에서 실행한다.
2. **단일 `main` 누적 SHA 결합 (`src/ttakkari/deployment.py:217-269`)**:
   - 모든 모듈 변경이 단일 `main` 커밋 SHA에 누적되므로, 선행 커밋(`063002a` 또는 `c8f1015`)에 단위 테스트 결함 1건이 들어가면 후속 무관 모듈 커밋도 동일 트리 SHA에 묶여 함께 차단된다.
3. **무차별 전체 테스트 게이트 (`src/ttakkari/generation_deployment.py:130-191`)**:
   - `validate_release()`는 변경 범위와 무관하게 매 배포마다 `git archive` + `uv sync --frozen` + `pytest -q --timeout=120 -x` 전체 스위트를 실행한다.
4. **ADR 0066 `delivery/validation.py`의 미연동 (`src/ttakkari/delivery/validation.py:136-184`)**:
   - `delivery_pipeline_enabled=False`일 뿐 아니라, Stage 2(`full`)에서 동일하게 전체 `pytest`를 돌리고 배포 컨트롤러와 검증 영수증을 공유하지 않는다.
5. **복구 대기 시 전역 중단 (`src/ttakkari/deployment.py:492-495`)**:
   - `recovery_pending` / `rolling_back` 상태 존재 시 후속 전환을 멈추는 **이 fail-closed 안전장치는 정당하며 절대 축소하지 않는다.** 격리해야 할 대상은 공유 런타임 전환 이전의 **모듈 변경 검증 실패**다.

### 3.2 좁은 DB-Free 정적 표본 실측(`35 passed in 3.34s`)과 한계 명시

- 격리 worktree(`back-platform-v2-a`)에서 `PYTHONPATH=/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/src`로 고정하고 실행한 5개 파일(`tests/test_contract_marker.py`, `tests/test_migration_single_head.py`, `unit_tests/test_executor_migration_contract.py`, `unit_tests/test_executor_reconcile.py`, `unit_tests/test_creative_v2_lease_handoff.py`)의 `--noconftest` 실행 결과는 **`35 passed in 3.34s`**이다 (초회 보고의 `39 passed in 5.00s` 및 "전체 계약 게이트 5초 보장" 주장은 철회).
- **엄격한 한계 명시**:
  1. 이 `35 passed in 3.34s` 결과는 **좁은 DB-free 정적/단위 표본**일 뿐이다.
  2. `--noconftest`는 `tests/conftest.py`의 세션 픽스처(`_schema_and_app`)를 우회하므로, **DB-touching 계약 테스트나 실제 PostgreSQL 트리거(`0064`의 4개 PL/pgSQL 트리거 등), 운영 슬롯 드레인 동작을 입증하지 않는다.**
  3. 따라서 P3에서 `pytest -m contract`를 정리할 때는 DB 불필요 정적 계약(`contract_static`)과 DB 스키마/트리거 계약(`contract_db`)을 명시적으로 분리해야 하며, `--noconftest` 우회만으로 전체 계약 검증 완료를 주장해서는 안 된다.

### 3.3 결정적 `EvidenceCacheKey v2` 및 `ValidationReceipt v2` 명세 (현재 미통합 / 설계 목표)

초회 보고의 경로 기반 v1 키는 파일 수정 시 거짓 양성(stale pass)을 낼 수 있으므로 **전면 폐기**하고, 모든 입력의 **바이트 내용 SHA-256 해시**를 강제하는 `platform-v2-evidence-key-v2` 및 `platform-v2-validation-receipt-v2`를 정의·구현하였다 (`A-extract-runtime-delivery-evidence.py:compute_evidence_cache_key`, `issue_validation_receipt`, `verify_validation_receipt` 및 `test_a_runtime_delivery_evidence.py`의 6종 변조 거부 음성 대조 테스트로 검증 완료).

```json
{
  "schema_version": "platform-v2-evidence-key-v2",
  "module_id": "creative",
  "gate_mode": "module_scoped",
  "candidate_src_tree_sha": "f2e5572cbc38e4234157be9c4c83090a1358be83",
  "changed_file_hashes": {
    "src/ttakkari/creative/render.py": "<sha256>"
  },
  "module_source_file_hashes": {
    "src/ttakkari/api/creative_v2.py": "<sha256>",
    "src/ttakkari/creative/...": "<sha256>"
  },
  "transitive_dependency_closure_hashes": {
    "src/ttakkari/config.py": "<sha256>",
    "src/ttakkari/host_resource.py": "<sha256>",
    "src/ttakkari/models.py": "<sha256>",
    "...": "<sha256>"
  },
  "selected_test_file_hashes": {
    "tests/test_creative_studio.py": "<sha256>",
    "unit_tests/test_creative_v2_lease_handoff.py": "<sha256>"
  },
  "test_harness_hashes": {
    "tests/conftest.py": "<sha256>",
    "scripts/pytest_slots.py": "<sha256>"
  },
  "suite_selector_args": [
    "-q", "--timeout=120", "-m", "contract",
    "tests/test_creative_studio.py", "unit_tests/test_creative_v2_lease_handoff.py"
  ],
  "dependency_lock_hashes": {
    "pyproject.toml": "<sha256>",
    "uv.lock": "<sha256>"
  },
  "runtime_environment": {
    "python_version": "3.13.x",
    "platform": "darwin-arm64",
    "pytest_plugins": ["pytest-asyncio", "pytest-timeout"],
    "relevant_env": {"TTAKKARI_RUNTIME_GENERATION": "0"}
  },
  "schema_and_migration_hashes": {
    "models_py_sha256": "<sha256>",
    "alembic_env_py_sha256": "<sha256>",
    "migration_head": "0067_collector_outbox_retry",
    "migration_file_hashes": {
      "alembic/versions/0001_initial.py": "<sha256>",
      "... (all 67 migration files)": "<sha256>"
    }
  },
  "deploy_contract_hashes": {
    "deploy/generation-contract.json": "<sha256>",
    "deploy/run-executor-manifest.json": "<sha256>"
  }
}
```

#### 무효화(Invalidation) 및 Fail-Closed 승격 규칙
1. **1바이트 내용 변경 즉시 무효화**:
   - 대상 모듈 소스 파일, 전이 의존성 클로저(`transitive_dependency_closure_hashes`) 내 파일, 선택된 테스트 파일, `tests/conftest.py` 및 테스트 헬퍼, `pyproject.toml`/`uv.lock`, `models.py`/`alembic/env.py`/67개 마이그레이션 파일 중 어느 하나, `deploy/generation-contract.json`/`deploy/run-executor-manifest.json` 중 **단 1바이트라도 바뀌면 `evidence_cache_key`가 즉시 달라지며 기존 영수증은 거부(`evidence_key_mismatch`)**된다.
   - Python 패치 버전(`3.13.x`)이나 `suite_selector_args`가 바뀌어도 즉시 무효화된다.
2. **불완전 클로저·동적 import·미분류 경로에 대한 Fail-Closed (`broad_fail_closed`)**:
   - 변경 파일이 비어 있거나, 디스크에 없거나, `src/ttakkari`·`tests`·`unit_tests` 밖의 미분류 경로이거나, `importlib.import_module`/`__import__` 등 동적 import로 인해 전이 의존성 클로저가 불완전(`closure_complete=False`)하거나, 공유 코어(`config`, `models`, `db`, `runs`, `security`, `generation*`, `executor*`, `alembic/*`, `deploy/*`)를 건드리면 **`module_scoped` 선택 실행을 즉시 거부하고 `broad_fail_closed` 전체 게이트로 승격**한다.
3. **Provenance / Trust 검증 (`ValidationReceipt v2`)**:
   - 영수증은 `candidate_commit_sha`, `candidate_src_tree_sha`, 실제 테스트 프로세스가 로드한 `import_origin_file`(`expected_src_root` 하위인지 검증), 발급 시각/만료 시각(`ttl_seconds <= 86400`), 그리고 HMAC-SHA256 `signature`를 포함하며, 하나라도 어긋나면 배포 게이트가 즉시 거부한다.
4. **현재 운영 상태와의 구분**:
   - 본 `EvidenceCacheKey v2` / `ValidationReceipt v2`는 현재 `A-extract-runtime-delivery-evidence.py`에 참조 구현 및 단위 테스트로 작성된 **목표 설계(Target Specification)**이며, 실제 운영 `generation_deployment.py`에는 P3 단계에서 통합한다. 통합 전까지 현재 운영의 전체 회귀·보안·`recovery_pending` 게이트는 일절 축소하지 않는다.

---

## 4. 최소 공용 기반 변경(Minimum Foundation) 계약·구현 순서·검증 시나리오

루트 오케스트레이터 지시에 따라, P3에서 구현할 **최소 공용 기반 변경 5단계의 선후 의존성 순서, 변경 파일, 검증 시나리오**를 단일 명세로 확정한다.

### 4.1 단계별 구현 순서 (Ordered Implementation Sequence)

| 순서 | 단계 ID 및 명칭 | 대상 파일 (`src/ttakkari/...`) | 핵심 변경 계약 | 선행 조건 |
| :---: | :--- | :--- | :--- | :--- |
| **Step 0** | **`FND-00` `origin/main` 기존 차단 테스트 정리 (`LOG-195`)** | `tests/test_desk_qwen_service.py` (`src/ttakkari/runs/desk_qwen.py`의 `PROMPT_VERSION = "harness-v3-topic-boundary"`와 정합성 맞춤) | 현재 `origin/main`(`c8f10156`)의 배포 게이트를 막고 있는 결정적 기대값 불일치 1건 해소 | 없음 (최우선) |
| **Step 1** | **`FND-01` 2계층 드레인 계약(`Admission` vs `Accepted Work`) 및 TTS 영속화 + 레거시 자동 재큐잉 차단** | `generation.py`, `tts/jobs.py`, `tts/service.py`, `models.py`, 신규 Expand-only 마이그레이션(`0068_tts_payload_chunks.py`), `runs/manager.py` (`resume_interrupted`) | 1) `tts_jobs`에 `chunks_json`(JSONB, nullable expand-only) 추가하여 `_pending_texts` 메모리 의존 제거<br>2) `_fail_queued_draining` 제거 및 `generation.enter_accepted_work()` 도입으로 이미 `202 Accepted`된 작업의 `draining` 중 실행 보장<br>3) `runs/manager.py:1941` `resume_interrupted()`의 무조건 자동 재큐잉 비활성화 | Step 0 |
| **Step 2** | **`FND-02` Creative V2 비동기 태스크 드레인 추적 및 체크포인트 내구성 보강** | `api/creative_v2.py`, `creative/v2/asset_generation_managed.py`, `generation_service.py` | 1) `_ASSET_GENERATION_TASKS`를 `RuntimeGeneration`의 `accepted_work` / `finalizers`에 등록하여 실행 중 슬롯 조기 `SIGTERM` 방지<br>2) `_write_checkpoint`에 `os.fsync()` 및 산출물 `sha256` 검증 추가, 작업 단위 `fencing_token` 연동 | Step 1 |
| **Step 3** | **`FND-03` Desk Qwen 세대 외부 단일 소유권 분리** | `runs/desk_qwen_service.py`, `generation_service.py`, `config.py` | API 세대 슬롯이 각자 `llama-server` 자식 프로세스를 스폰하지 않고 외부 단일 관리 인스턴스(`desk_qwen_base_url`)를 공유하도록 전환 | Step 1 |
| **Step 4** | **`FND-04` ADR 0036 `RunExecutor` 활성화 및 Ownership/Fence Cutover 검증** | `deploy/generation-contract.json`, `deploy/run-executor-manifest.json`, `executor.py`, `executor_reconcile.py` | `run_executor.activation="enabled"` 및 `run_executor_mode="remote"` 전환을 격리 환경에서 검증하여 `Run` 프로세스 그룹(`runtime_process_groups`)과 주기 작업이 API 슬롯 재배포와 무관하게 유지됨을 입증 | Step 1, Step 2, Step 3 |
| **Step 5** | **`FND-05` `EvidenceCacheKey v2` / `ValidationReceipt v2` 배포 게이트 연동** | `delivery/validation.py`, `generation_deployment.py`, `deploy_gate.py`, `tests/conftest.py` | 1) 정적 계약(`contract_static`)과 DB 계약(`contract_db`) 픽스처 분리<br>2) `compute_evidence_cache_key` 및 `verify_validation_receipt`를 `delivery/validation.py`와 `generation_deployment.py`에 연결 (공유 코어/스키마/보안/불완전 클로저는 무조건 `broad_fail_closed`) | Step 4 |

### 4.2 공용 기반 및 모듈 전환 인수 시나리오 (Acceptance Scenarios)

각 구현 단계는 아래 구체적 인수 시나리오(실행 명령 + 기대 상태 전이 + 음성 대조)를 통과해야 완료로 인정된다.

1. **`ACC-01` (TTS Accepted-Work Drain & Zero `generation_draining` Failure)**:
   - **시나리오**: 슬롯 A(`active`)에서 `POST /api/tts/jobs`로 3개 작업을 수락(`202 Accepted`, 1개 `running`, 2개 `queued`)한 직후 슬롯 A를 `draining`으로 전환하고 슬롯 B를 `active`로 승격한다.
   - **통과 기준**:
     - 슬롯 A(또는 `RunExecutor`)는 DB `tts_jobs.chunks_json`에서 원문을 읽어 남은 2개 `queued` 작업까지 모두 `status="succeeded"`로 완료한다 (`error_code="generation_draining"` 발생 **0건**).
     - 3개 작업이 모두 끝날 때까지 슬롯 A의 `can_retire()`는 `False`를 유지하며, 완료 직후 `True`가 되어 정상 은퇴한다.
2. **`ACC-02` (Creative V2 In-Flight Preservation Across Slot Drain)**:
   - **시나리오**: `POST /api/creative/v2/plans/{pid}/production/generate-assets` 실행 중(ComfyUI 완료 후 하위 TTS 진입 직전) API 슬롯이 `draining`으로 전환된다.
   - **통과 기준**:
     - 상위 자산 생성 태스크가 `accepted_work`로 추적되어 슬롯이 조기 종료되지 않고, 하위 `tts.service.budget()` 호출이 `enter_accepted_work`를 통해 승인되어 전체 자산 생성이 `succeeded`로 완료된다.
     - 체크포인트 파일은 `fsync` 및 `sha256` 검증을 거치며, 구세대 펜스(`stale fencing_token`)를 가진 중복 워커가 쓰기를 시도하면 즉시 거부된다.
3. **`ACC-03` (Restart Reconcile Without Blind Replay)**:
   - **시나리오**: 실행 중이던 `Run`의 리더 프로세스가 강제 종료(`NoSuchProcess`)된 상태에서 재기동/Reconcile을 실행한다 (`runtime_generation=True` 및 `False` 양쪽 모두).
   - **통과 기준**:
     - 살아있는 리스/프로세스(`pid`, `process_started_at`, `pgid` 일치)는 건드리지 않는다(`skipped_live_lease`).
     - `AccessDenied`/`OSError`는 `UNKNOWN`으로 보존한다(`skipped_unknown`).
     - `NoSuchProcess`(`ABSENT`)가 확인된 Run만 `status="interrupted"`로 마감하며, **어느 기동 모드에서도 자동 재큐잉(`resume_interrupted` replay)이 발생하지 않는다(`auto_requeued_count == 0`).**
4. **`ACC-04` (`EvidenceCacheKey v2` Tamper Rejection & Broad Fail-Closed)**:
   - **시나리오**: 모듈 소스, 전이 의존성 파일(`models.py` 등), 테스트 파일, `tests/conftest.py`, `uv.lock`, 또는 67개 마이그레이션 파일 중 단 1바이트를 수정하거나, 잘못된 체크아웃에서 import된 영수증을 제시한다.
   - **통과 기준**:
     - `verify_validation_receipt()`가 100% `valid=False`로 거부하고 전체 게이트(`broad_fail_closed`) 재실행을 강제한다.
     - 유효한 영수증이 있더라도 전환 시점의 실시간 프로브(`/api/health`, `/api/ready`, `/api/version`)는 절대 캐시하지 않고 매번 실행한다.
5. **`ACC-05` (P4 목표: 독립 모듈 릴리스 시 Core API 무재배포 및 구버전 드레인)**:
   - **시나리오** *(현재 미구현 목표 아키텍처 인수 기준)*: 첫 독립 모듈(잠정 1순위: `Creative Studio`) 코드만 수정하여 모듈 번들을 `v1` → `v2`로 전환한다.
   - **통과 기준**:
     - 전환 전후 Core API 슬롯(`8791/8792/8793`)의 프로세스 `pid`와 `runtime_generations.id`가 불변이다 (`Core redeploy count == 0`).
     - 전환 이전에 시작된 `v1` 잡은 `pinned_module_revision == v1`로 끝까지 완료되고, 전환 직후 요청은 `v2`로 처리된다.
     - 모듈 스키마 변경이 필요한 경우 **Expand-only(nullable 컬럼/신규 테이블 추가)만 허용**하며, 파괴적 변경이나 공유 테이블 마이그레이션이 포함되면 즉시 Core 광역 배포로 승격한다.
     - 포인터(`active.json`)를 `previous_revision`으로 되돌리면 Core 재시작 없이 1초 미만(`<1s`)에 `v1`으로 롤백된다.
     - 단, `Creative Studio`의 최종 1순위 확정은 Worker B(DB/FK/파일 소유권 교정본) 및 Worker C(프런트/테스트 교정본) 검증 완료 후 루트 통합에서 확정하며, 현재는 **`provisional` (잠정 1순위)**로 유지한다.
6. **`ACC-06` (Gate B 운영 전환 전 필수 선행 실증 — Non-Circular Preconditions)**:
   - **비순환 게이트 분리**:
     - **Gate A (P3 격리 코드 구현 및 테스트 착수 게이트)**: 본 교정 보고서의 계약·상태 전이·인수 시나리오 정의만으로 즉시 충족됨 (`P0`의 운영 백업/live 미확인 항목이 P3 코드 작성을 막지 않음).
     - **Gate B (P2 최종 운영 서명 및 P4/P6 실제 운영 활성화 게이트)**: 실제 운영 전환(`activation="enabled"` 또는 운영 배포) 전에는 반드시 (1) 최근 백업 및 `restore_rehearsal.sh` 성공 영수증 확인, (2) 실시간 활성 API 슬롯 및 `supervisor.json` 리비전 직접 확인, (3) 격리 환경에서의 Ownership Cutover / Rollback E2E 리허설 통과가 선행되어야 한다.

---

## 5. 잔여 Blocker 및 미검증(`확인 필요`) 원장

| 구분 | 항목 | 현재 상태 | 해소 경로 및 게이트 귀속 |
| :--- | :--- | :---: | :--- |
| **Blocker 1** | Worker B(`c1e9cd73`) 및 Worker C(`9a3215de`) 감사 산출물 교정·재검증 미완료 | `revising / 확인 필요` | 루트 오케스트레이터가 B(`d492` 교정 중)와 C(교정 중)의 재검증 통과본을 수합해 최종 모듈 경계 및 `Creative Studio`(`provisional`) 1순위 여부를 확정 (**P2 최종 확정 게이트**) |
| **Blocker 2** | `origin/main`(`c8f10156`)의 `tests/test_desk_qwen_service.py` 기대값 불일치(`LOG-195`) | `implemented defect` | P3 `Step 0 (FND-00)`에서 즉시 수정 가능 |
| **Unverified 1** | 현재 실시간 서빙 중인 API 리비전/슬롯 및 `~/.ttakkari/deploy/supervisor.json` 원문 | `unknown / 확인 필요` | **Gate B (운영 전환 전 선행조건)**: 운영 배포/전환 직전 `/api/version` 실시간 프로브 및 감독 상태 확인 |
| **Unverified 2** | `~/.ttakkari-backups/latest.json` 최근 백업 시각 및 복원 리허설(`restore_rehearsal.sh`) 최신 실행 영수증 | `unknown / 확인 필요` | **Gate B (운영 전환 전 선행조건)**: 운영 DB 마이그레이션(`0068`) 및 Executor 활성화 직전 백업·복원 리허설 실행 |
| **Unverified 3** | 배포 실패 기간별 전체 raw 이벤트 전수 분모 (`ops_list_deploys` 50건 상한 너머) | `unknown / 확인 필요` | 정성적 원인(`deploy:pytest` 지배)은 증명되었으므로 아키텍처 설계에는 영향 없음; P6 전후 성능 비교 시에는 동일 측정 구간/모수로 실측 |
