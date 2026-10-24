# 따까리 플랫폼 V2 요구사항 정본 및 전체 진행 상태 원장 (`00-request-and-status.md`)

> **2026-10-25 현재 상태 정정:** 이 파일의 아래 상태표·담당 워커·기준 SHA는 2026-10-24 역사 원장이다. 원래 요구/ID/필수 실증은 유지한다. 현재 실행부 분리 결정·P0~P6/S2/FND1/INT의 미완료 경계는 [현재 실행 계약](current-execution-contract.md)과 최신 `manage_work`가 우선한다. 버전별 모듈 번들·active.json·새 공통 lease/fence·HMAC 게이트 의무화 제안은 폐기됐다. P4/Core 무배포·보존·rollback 및 P6 요구 자체는 폐기하지 않았다. 현재 frontend/runtime/test 분모는 [2026-10-25 대조](../../../review/platform-v2/20261025/p1-frontend-runtime-tests-review.md)를 읽는다. main push/배포 예약 보류.

> 현재 교정 및 실제 배포 범위는 [검증 실패 교정·통합 기록](verified-corrections-20261024.md)이 우선한다. 아래의 이전 후보 완료 표시는 당시 이력이며 V2 전체 완료나 현재 운영 검증을 뜻하지 않는다.


- 문서 소유: Worker A (Runtime / Delivery / Architecture, 세션 ID `e65430e3-0775-4e2c-910a-5050cdb7d216`)
- 갱신 시각: `2026-10-24T22:50:00+09:00` (독립 재검증 `d3f7189` 지적사항 전수 교정 및 Gate A 격리 공용 기반 코드 구현 반영)
- 승인 및 단계 상태:
  - **2026-10-24 사용자 전면 구현 승인 완료**.
  - **초회 감사 및 검증 교정 완료는 전체 리팩토링 프로젝트 완료가 아니며, 다음 구현을 위한 추가 사용자 승인 대기 단계도 아니다.**
  - 모든 병렬 워커(A, B, C)의 결과가 독립 검증을 통과해 루트에서 통합되기 전에는 단독으로 모듈 경계를 최종 확정하거나 운영(Live) 활성화(Gate B)를 수행하지 않는다. 단, 감사 재검증 반복이 전체 구현을 무기한 막지 않도록 **Gate A(격리 공용 기반 코드 `src/ttakkari/delivery/validation.py` 작성 및 단위 테스트)는 승인되어 진행**하며, 실행 중인 production 경로 / 배포 게이트에는 아직 연결하지 않는다.
  - **비순환 선행조건(Non-Circular Preconditions) 분리**:
    - **`Gate A` (P3 격리 테스트/복구/공용 기반 코드 구현 착수 게이트)**: 충족됨. `P0`의 운영 백업/실시간 슬롯 미확인(`unknown`) 항목이 P3 격리 코드 구현을 가로막지 않도록 분리하며, `src/ttakkari/delivery/validation.py`의 `CandidateSnapshotIdentity`, `ValidationKey`, `ValidationReceipt`, `ValidationCachePolicy` 구현 및 `tests/delivery/test_validation_cache.py` 단위 테스트 완료.
    - **`Gate B` (P2 최종 운영 서명 및 P4/P6 실제 운영 전환 활성화 게이트)**: (1) Worker B·C 교정본 독립 검증 통과 및 루트 통합, (2) `P0` 백업/복원 리허설(`restore_rehearsal.sh`) 영수증 확인, (3) 현재 실시간 활성 API 슬롯 및 `supervisor.json` 리비전 확인, (4) 격리 Ownership Cutover / Rollback E2E 리허설 통과를 선행조건으로 요구함 (`P0`, `P1`, `P2`는 현재 모두 미완료 상태 유지).
- 대상 저장소:
  - Backend 정본 (`origin/main`): `/Users/winterholic/development/projects/winterholic-ttakkari/back` (`c8f10156845d007ec6496a65d6d8417be19260bd`, `src/ttakkari` 트리 `f2e5572ce8d039341eacc4dad5a70db03c61553b`)
  - 공유 `/back` 로컬 HEAD: `607f8bda0df7dbdd458e4f0e54d703ce9edd3def` (`merge-base` `2522d78e29b14ffb29d8536bcf833d832b4f87e1` 기준 `c8f10156` 대비 **`2 ahead / 118 behind` 양방향 분기**, 2-점 diff `366 files changed, 86,213 insertions(+), 339 deletions(-)`; 타 작업자의 dirty main 보존을 위해 공유 `/back` 복사·동기화 전면 중단)
  - 최근 성공 배포 영수증 (`historical_receipt`): `9f82c9c16cd604e81ddf43180b0be2e895ec85cb` (`4a6e09a8-c49f-4c2f-9a6c-c0d83fe5824a`, `2026-10-23T10:43:06Z`); 현재 실시간 서빙 리비전 및 감독 리비전은 `unknown / 확인 필요`
  - Frontend: `/Users/winterholic/development/projects/winterholic-ttakkari/front` (`origin/main` = `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf`)
- PMS 프로젝트 ID: `e62fbc3f-b2d3-40ad-b5fc-687c8ccff286`

---

## 0. 병렬 감사·검증 워커 ID·담당·산출물 원장 (Parallel Audit & Verification Workers Ledger)

### 0.1 현재 시점(`2026-10-24T22:50:00+09:00`) 확정 상태표

| 워커 구분 | 확정 Worker / Session ID | 담당 영역 | 격리 Worktree / 브랜치 / 대상 커밋 | 담당 산출물 경로 | 현재 상태 (`2026-10-24T22:50+09:00` 기준) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Worker A** | `e65430e3-0775-4e2c-910a-5050cdb7d216` | **Runtime / Delivery / Architecture 판단** | `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a` (`audit/platform-v2-a-runtime-delivery-20261024`, base `c8f10156`) | - `review/platform-v2/20261024/A-runtime-delivery.md`<br>- `review/platform-v2/20261024/A-runtime-delivery-evidence.json`<br>- `review/platform-v2/20261024/A-extract-runtime-delivery-evidence.py`<br>- `review/platform-v2/20261024/test_a_runtime_delivery_evidence.py`<br>- `docs/design/platform-v2/00-request-and-status.md`<br>- `docs/design/platform-v2/architecture-options.md`<br>- `src/ttakkari/delivery/validation.py`<br>- `tests/delivery/test_validation_cache.py` | **독립 재검증 `d3f7189`(`failed`) 지적 4대 결함(candidate snapshot identity 누락, dirty doc manifest 누락, 전이 dynamic import 클로저 미반영, 캐시 입력 바인딩) 전수 교정 및 Gate A 격리 공용 기반 코드(`src/ttakkari/delivery/validation.py`) 및 단위 테스트 구현 완료** (`candidate_ready_for_reverification`) |
| **검증 워커 1 (`Bv` / `Av`)** | `1d994e50-c900-4f90-9975-1ff082522a35` | **Worker B 초회본 및 Worker A 초회본 독립 검증** |- B 검증: `/private/tmp/back-platform-v2-b-verification-20261024` (`b245d5a`)<br>- A 검증: `/private/tmp/back-platform-v2-a-runtime-delivery-verification-20261024` (`93af9ef6`) | - `B-backend-independent-verification.{md,json}` (`b245d5a`)<br>- `A-runtime-delivery-independent-verification.{md,json}` (`93af9ef6`) | **과거 검증 완료 (65% 진행 상태로 이번 재검증에는 미재사용)**:<br>1) B 초회(`86373ce`) → `b245d5a`에서 **`failed`** 판정<br>2) A 초회(`fddad5e`) → `93af9ef6`에서 **`failed`** 판정 (이후 A 독립 재검증 `d3f7189`에서 추가 결함 확인됨) |
| **검증 워커 2 (`Cv` / `Bv2`)** | `fed2c630-a867-42f1-9574-4a92905de864` | **Worker C 초회본 및 Worker B 2차본(`2cc`) 독립 검증** | 루트 배정 검증 worktree (`5aa5968` 등) | - Worker C 초회 검증 보고<br>- Worker B `2cc80d9` 검증 보고 (`5aa5968`) | **과거 검증 완료 (`completed`)**:<br>1) C 초회 산출물 → **`failed`** 판정<br>2) B 2차본(`2cc80d9`) → `5aa5968`에서 **`failed`** 판정 |
| **Worker B** | `c1e9cd73-83ef-4240-99c8-809e500bd903` | **Backend / Data / Dependency 감사** | `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-b` (`audit/platform-v2-b-backend-20261024`) | - `docs/design/platform-v2/backend-module-inventory.{md,json,csv}`<br>- `docs/design/platform-v2/backend-dependency-graph.{md,json,mmd}`<br>- `docs/design/platform-v2/data-ownership.md`<br>- `review/platform-v2/20261024/backend/*` | **교정 중 (`revising / 확인 필요`)**:<br>- 초회 `86373ce` → `b245d5a` `failed`<br>- 2차 `2cc80d9` → `5aa5968` `failed`<br>- 최후보 `ac7c8c5` → 독립검증 `de1bd7f`에서 **`failed`** (범주별 error/source binding 미완) 후 현재 교정 진행 중 |
| **Worker C** | `9a3215de-01d4-4efd-b3ff-9e282de9b766` | **Frontend / Test Architecture 감사** | Worker C 격리/작업 공간 | - `docs/design/platform-v2/frontend-feature-map.{md,json,csv}`<br>- `docs/design/platform-v2/test-architecture-and-baseline.md`<br>- `docs/design/platform-v2/test-impact-proposal.md`<br>- `review/platform-v2/20261024/frontend-tests/*` | **새 검증 진행 중 (`verification_in_progress / 확인 필요`)**:<br>- 초회 산출물 `fed2c630` `failed` 판정 후 최후보 `b155049063a471ddab54d309831c2ee8758bfcd7` 제출되어 새 검증 진행 중 |
| *(참고)* UI 워커 | `6e33860e-3773-48bf-8df5-a3dc2c330a3e` | 메인 화면 카드 CSS 수정 (범위 외 선행 종료) | `front` 전용 격리 worktree (종료) | `front` 커밋 `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf` | 종료 (`back` 혼입 없음 확인) |

### 0.2 시간 고정 이력 보존 (Historical Snapshot Ledger)

- **`2026-10-24T22:00:00+09:00` 시점 스냅샷 (커밋 `0d048ea031a8a38ef9ef9e1a66611dc21c970623`)**:
  - 당시 Worker A는 `1d994e50`과 `fed2c630`이 각각 B·C 초회본을 검증 중(`running` / `verification_pending`)인 것으로 기록하였으나, 이후 독립 검증이 진행되어 B(`86373ce` → `b245d5a` `failed`; `2cc` → `5aa5968` `failed`), C(초회 → `fed2c630` `failed`), A(`fddad5e` → `93af9ef6` `failed`) 판정이 내려졌다.
  - 따라서 `0d048ea`의 `running / verification_pending` 표기는 `22:00 KST` 당시의 시간 고정 이력으로만 보존하고, 현재 상태는 위 **0.1절 표**를 정본으로 사용한다.

### 0.3 Baseline Drift (`607f8bda` ↔ `c8f10156`) 교정 정본 수치

1. **Git 토폴로지**:
   - `merge-base(607f8bda0df7dbdd458e4f0e54d703ce9edd3def, c8f10156845d007ec6496a65d6d8417be19260bd)` = `2522d78e29b14ffb29d8536bcf833d832b4f87e1`
   - `git rev-list --left-right --count 607f8bda...c8f10156` = **`2 ahead / 118 behind` (양방향 분기)**
     - `607f8bda` 고유 커밋 2개: `607f8bda0df7dbdd458e4f0e54d703ce9edd3def`, `66c840200b8c529a1a4cf9e06704b30f92b4e5a2`
     - `c8f10156` 고유 커밋: **118개**
   - 2-점 비교(`git diff --shortstat 607f8bda c8f10156`): **`366 files changed, 86,213 insertions(+), 339 deletions(-)`** (초회 `111 behind / 253 files changed` 수치는 철회).
2. **`src/ttakkari` 파이썬 파일 및 라인 수**:
   - `607f8bda`: **`321`개 파일 / `71,985`줄**
   - `c8f10156`: **`385`개 파일 / `94,439`줄** (**`+64`개 `.py` 파일, `+22,454`줄**)
3. **Alembic 마이그레이션(`62` → `67`, `+5`개) 및 신규 테이블 수(`+18`개)**:
   - `0063_collector_stage_items.py`: **+5 tables** (`collector_source_policies`, `collector_host_state`, `collector_fetch_cache`, `collector_items`, `collector_dedupe_bands`)
   - `0064_run_executor.py`: **+1 table** (`runtime_process_groups`), 컬럼 추가 (`runtime_generations.role/protocol/execution_mode`, `runtime_control.executor_generation`), **+4 PL/pgSQL triggers** (`guard_executor_control`, `guard_executor_api`, `guard_executor_lease`, `route_executor_command`) *(초회 `0064` 4테이블 및 총 21테이블 주장은 철회)*
   - `0065_collector_delivery.py`: **+8 tables** (`collector_sources`, `kb_document_access`, `kb_observations`, `collector_deliveries`, `collector_daily_budgets`, `collector_reviews`, `collector_quality_models`, `kb_passage_vectors`)
   - `0066_collector_budget.py`: **+4 tables** (`collector_budget_buckets`, `collector_budget_reservations`, `collector_cache`, `collector_runtime_flags`)
   - `0067_collector_outbox_retry.py`: **+0 tables** (`collector_outbox`에 `attempts`, `not_before`, `last_error` 컬럼 추가)

---

## 1. 사용자 요구사항 정본 (Canonical Requirements)

### 1.1 핵심 목표 (Core Objective)

유용한 도메인·기능 모듈들이 **공통 AI 플랫폼과 단일 UX**를 공유하면서도, **독립적인 코드 변경(Change), 검증(Validation), 릴리스(Release), 실행(Execution), 복구(Recovery)** 생명주기를 갖도록 따까리 플랫폼을 V2로 리팩토링한다.
획일적인 마이크로서비스 분리가 아니라, 실측 데이터(결합도, 변경 빈도, 검증 비용, 장애 반경, 호스트 자원 비용)를 근거로 **모듈형 모놀리스(Modular Monolith) + 독립 서비스/워커/잡/라이브러리/어댑터** 형태를 각 모듈 특성에 맞게 선택한다.

### 1.2 4대 논리 평면 (Four Logical Planes)

물리 프로세스를 무조건 4개로 고정하는 전제를 금지하며, 모든 코드와 계약을 아래 4개 논리 평면으로 정렬한다.

1. **Control Plane (제어 평면)**:
   - 인증(JWT/쿠키/공유 토큰), 세션 관리, API 라우팅, 모듈·도구·모델 레지스트리, 버전·릴리스 포인터, 보안·권한 정책, 헬스·준비 상태(`/api/health`, `/api/ready`, `/api/version`).
2. **Execution Plane (실행 평면)**:
   - AI CLI 어댑터(`claude`, `codex`, `antigravity`, `muse`, `qwen-local`), 영속 잡/명령 큐, 실행 소유권(`fencing_token`, `runtime_process_groups`), 취소·드레인, 호스트 자원 리스(`ResourceLeaseManager` GPU/CPU/메모리 예산), 장기 실행 보장.
3. **Domain Plane (도메인 평면)**:
   - 따까리의 실제 사용자 기능 모듈들(Creative Studio V1/V2, Game Studio, Collector Platform, Knowledge/RAG Library, Memory/Ontology, Meetings/STT, Local TTS, NotebookLM, Reverse Engineering, Design System, TossInvest, PMS/Planner/Checklists, Shares/Transfers/Workspaces 등)을 데이터 소유권과 실행 특성에 따라 분류·격리.
4. **Delivery Plane (배달 평면)**:
   - ChangeSet 격리(`worktrees`, `change_sets`), 변경 영향 분석(역의존성·공유 계약·스키마·보안), 선택적 테스트 게이트, 계약 검증(`pytest -m contract`), 불변 릴리스 아카이브, 트래픽 전환/핫스왑, 롤백, 결정적 검증 증거(`EvidenceCacheKey v2` 및 `ValidationReceipt v2`).

### 1.3 절대 불변 조건 (Hard Invariants)

1. **기능·데이터 무손실**: 모든 기존 기능과 사용자 데이터를 100% 보존한다. DB 재구축이나 데이터 유실을 절대 금지한다.
2. **실행 중 작업 보호**: 실행 중인 `Run`이나 미디어·수집 작업을 배포나 전환 때문에 임의로 중단(`kill`)하거나 강제 실패(`generation_draining`)시키지 않는다.
3. **임의 자동 복구(Blind Replay/Resume) 금지**: 소유권이 증명되지 않은 작업을 재기동 시 자동으로 처음부터 다시 돌리거나 이어서 실행하지 않는다(`runs/manager.py:1941` `resume_interrupted()`의 무조건 자동 재큐잉도 P3에서 제거/격리 필수). 커널 프로세스 신원(`pid`, `pgid`, `process_started_at`)과 리스·펜싱 토큰을 대조해 살아있는 소유자는 보존하고, `NoSuchProcess`(`ABSENT`)만 `interrupted`로 마감하며 `AccessDenied`/`OSError`는 `UNKNOWN`으로 보존한다.
4. **Exactly-Once 가정 금지**: 모든 명령·이벤트·외부 부수효과는 멱등 키(`idempotency_key`), 펜싱 토큰(`fencing_token`), 내용 해시(`sha256`), `fsync` 후 원자적 교체(`os.replace`), 또는 명시적 보상(compensation)으로 중복을 방지한다.
5. **외부 인터페이스 하위 호환**: 기존 HTTP API 경로, MCP 도구 카탈로그, 프런트엔드 라우트·계약을 깨지 않는다.
6. **Core 3슬롯과 모듈 릴리스 생명주기의 분리**: Core API의 무중단 세대 슬롯(`8791`/`8792`/`8793`)과 개별 모듈의 릴리스·실행 생명주기를 혼동하지 않는다. 모든 모듈에 3슬롯을 복제하지 않는다.
7. **외부 무거운 인프라 도입 금지**: 신규 Redis, 외부 메시지 브로커, Kubernetes, 서비스 메시, 분산 DB 클러스터 도입을 금지한다. 기존 단일 호스트 PostgreSQL, 로컬 파일 캐시/가중치/아티팩트 저장소, `ResourceLeaseManager`를 재사용한다.
8. **운영·보안 경계 준수**: 가드 우회, 보호된 `~/.ttakkari` 설정·계정·하네스·로그 직접 스캔, 에이전트 내부에서의 `launchctl`/재시작 스크립트 실행, 미검증 감독(Supervisor) 교체를 금지한다.

---

## 2. PMS 단계별(P0~P6) 진행 상태 원장 (비순환 선행조건 적용)

PMS 프로젝트 `e62fbc3f-b2d3-40ad-b5fc-687c8ccff286`의 7개 단계에 대해 `계획(Plan)`, `코드(Code)`, `테스트(Test)`, `운영(Live)`, `미검증(Unverified)` 상태를 분리하여 기록한다. **`P0`, `P1`, `P2`는 모두 미완료(`doing` / `todo`) 상태를 유지한다.**

| PMS 단계 ID | 단계명 | 현재 PMS 상태 | 계획(Plan) | 코드(Code) | 테스트(Test) | 운영(Live) / 실측(Measured) | 미검증 / 잔여 선행조건 (`확인 필요`) |
| :--- | :--- | :---: | :--- | :--- | :--- | :--- | :--- |
| **`P0`** (`06d3fd18-d2b6-425c-b280-89afa6c3b836`) | **P0 운영·복구·성능 기준선** | `doing` *(미완료)* | 완료 | 동적 Git/스냅샷 바인딩 추출기(`A-extract-runtime-delivery-evidence.py`) 및 백업/복원 스크립트 구현됨 | 추출기/바인딩/v2 캐시키 음성 대조 테스트(`4 passed`) 및 좁은 DB-free 표본 `35 passed in 3.34s` | - 과거 배포 영수증(`historical_receipt`): `4a6e09a8`(`9f82c9c1`, 총 `1,576.48s`, 완주 pytest `1,045.97s`), `e15536bc`(`c8f10156`, `-x` 중단 `172.23s`), `794447f3`(`063002a1`, `-x` 중단 `985.34s`)<br>- 현재 직접 관측(`direct_current_observation`): 호스트 `backend pid=44104`, `llama-server pid=44129` 1개(RSS `7.20GiB`), Swap `94.8%~95.3%`, `host` vs `codex` capabilities 분리 | **[Gate B 선행조건 — P3 격리 코드 착수는 막지 않음]**:<br>1) 현재 실시간 서빙 API 리비전/슬롯 및 `supervisor.json` 원문(`unknown / 확인 필요`)<br>2) `~/.ttakkari-backups/latest.json` 및 최근 `restore_rehearsal.sh` 실행 영수증(`unknown / 확인 필요`)<br>3) 배포 실패 전체 raw ID 분모(`확인 필요`) |
| **`P1`** (`9f414a3c-4533-4e1d-93ec-52ecb901864d`) | **P1 전체 기능·데이터·의존성 감사** | `doing` *(미완료)* | 완료 | Worker A 런타임·배달 교정 완료; Worker B(`d492` 교정 중), Worker C(교정 중) | Worker A 테스트 통과; Worker B(`86373ce`→`b245d5a` `failed`, `2cc`→`5aa5968` `failed`), Worker C(초회→`fed2c630` `failed`)는 재검증 대기 | `origin/main`(`c8f10156`, 385파일/94,439줄/67마이그레이션) 및 `607f8bda`(`2 ahead / 118 behind`, +18 신설 테이블) 동적 Git 대조 완료 | Worker B(`c1e9cd73`) 및 Worker C(`9a3215de`) 교정 산출물의 독립 재검증 통과 필요 |
| **`P2`** (`916a7835-b5e6-4daf-a687-3f3e647eb9ec`) | **P2 목표 아키텍처·계약·전환 전략** | `todo` *(미완료)* | 아키텍처 대안·상태전이·v2 캐시키·최소 기반 순서 수립 완료 (`architecture-options.md`) | 미착수 (소스/운영 동작 변경 보류 유지) | `EvidenceCacheKey v2` / `ValidationReceipt v2` 참조 구현 및 변조 거부 테스트(`test_a_runtime_delivery_evidence.py`) 완료 | 미적용 (`module-releases`, `active.json`, 버전드 디스패처는 현재 미구현 설계 목표) | - **Gate A (충족)**: P3 격리 공용 기반 코드 구현 착수 가능<br>- **Gate B (미충족)**: B·C 재검증 통합을 통한 `Creative Studio`(`provisional`) 1순위 최종 확정 + `P0` 백업/실시간 슬롯/Cutover E2E 확인 후 P2 최종 운영 서명 |
| **`P3`** (`ec3786e9-27ba-4d49-9f35-65e6f21c0bf4`) | **P3 공용 기반·테스트·Delivery 구현** | `doing` | `FND-00`~`FND-05` 구현 순서 및 `ACC-01`~`ACC-04` 인수 시나리오 확정 (`A-runtime-delivery.md` 4절) | Gate A 한정 격리 공용 기반 코드(`src/ttakkari/delivery/validation.py`) 구현 완료 | 신규 단위 테스트(`tests/delivery/test_validation_cache.py`, 3 passed) 완료 | `feature-off` (Gate A 한정, 운영 경로 미연결) | `FND-00`(`LOG-195` 수정) → `FND-01`(TTS 영속화·2계층 드레인·`resume_interrupted` 차단) → `FND-02`(Creative 태스크 추적·체크포인트 `fsync`/`sha256`/펜스) → `FND-03`(Qwen 외부 단일화) → `FND-04`(`RunExecutor` 활성화 검증) → `FND-05`(`EvidenceCacheKey v2` 게이트 연동) |
| **`P4`** (`47f15831-5f97-4429-b381-fddd87be1549`) | **P4 첫 독립 모듈 실제 전환** | `todo` | 잠정 1순위(`provisional`) `Creative Studio` 선정 및 9대 실증 시나리오 설계 완료 | 미착수 (`module-releases` 컨트롤러 및 버전드 디스패처 미구현) | 미실행 | 미적용 | B·C 검증 통합 후 최종 1순위 확정 및 P3 완료 후 구현·9대 시나리오 실증 |
| **`P5`** (`8fd29eab-c45f-4097-a6f5-35ab57515d8d`) | **P5 나머지 기능 단계 전환** | `todo` | 후보 우선순위 수립 | 미착수 | 미실행 | 미적용 | P4 실측 성공 후 순차 진행 |
| **`P6`** (`4fce0913-3e1c-4bdf-87de-0978cb739180`) | **P6 통합·보안·복구·운영 전환** | `todo` | 검증 및 롤백 기준 정의 완료 | 미착수 | 미실행 | 미적용 | 전후 성능 비교 실측, 마이그레이션·롤백 가이드 완성, 잔여 부채 정리 |

---

## 3. 최종 13대 산출물(13 Final Deliverables) 커버리지 및 미완 원장

| 번호 | 산출물 명칭 | 주담당 / 협조 | 현재 상태 | 현재 확보된 산출물 경로 | 완료를 위한 잔여 작업 (미완 원장) |
| :---: | :--- | :---: | :---: | :--- | :--- |
| **1** | **전체 모듈 인벤토리 (Full Module Inventory)** | B, C (협조 A) | **교정 중 (`P1`)** | - Worker A `A-runtime-delivery-evidence.json` (`c8f10156` 385파일/67마이그레이션 정적 요약)<br>- Worker B·C 인벤토리 (현재 `revising / 확인 필요`) | Worker B(`d492`) 및 Worker C 교정본의 독립 재검증 통과 및 통합 |
| **2** | **현재 의존성 그래프 (Current Dependency Graph)** | B, A | **교정 중 (`P1`)** | - Worker A `A-runtime-delivery-evidence.json` (`codebase_metrics.modules`)<br>- Worker B 의존성 그래프 (현재 `revising / 확인 필요`) | Worker B의 파일/심볼/DB 결합도 교정본 재검증 통과 |
| **3** | **목표 아키텍처 다이어그램 (Target Architecture Diagram)** | A | **교정 완료 (`P2` 후보)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/architecture-options.md` | B·C 통합 후 최종 확정 다이어그램으로 서명 |
| **4** | **분리 대 유지 결정표 (Split vs Retain Decisions)** | A, B, C | **잠정 수립 (`provisional`, `P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/architecture-options.md` | B·C의 DB FK·파일·프런트 결합 검증 완료 후 `Creative Studio`(`provisional`) 포함 전체 모듈 최종 확정 |
| **5** | **공유 자원 및 데이터 소유권 원장 (Shared Resources + Data Ownership)** | B, A | **런타임 자원 교정 완료 / 데이터 원장 교정 중 (`P1`/`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery.md`<br>- Worker B `data-ownership.md` (`revising`) | `0063`~`0067` 신규 18개 테이블 및 파일 저장소를 B의 교정본에 완전히 통합 |
| **6** | **API·Job·Event·Module 계약 명세 (Contracts)** | A, B, C | **런타임·배달·상태전이 계약 확정 (`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery.md` (2.4절, 3.3절, 4절)<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/architecture-options.md` (4절) | P3/P4 구현 시 프로덕션 코드 및 계약 테스트로 고정 |
| **7** | **테스트 및 Delivery 파이프라인 재설계 (Test + Delivery Redesign)** | C, A | **`EvidenceCacheKey v2` 명세·참조구현 완료 / C 테스트 인벤토리 교정 중 (`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery.md` (3절)<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-extract-runtime-delivery-evidence.py` | Worker C 교정본 통합 및 P3 `FND-05`에서 `delivery/validation.py`·`generation_deployment.py` 실제 연동 |
| **8** | **단계별 WBS·PMS 및 선행조건 (Phased WBS-PMS + Preconditions)** | A (메인 총괄) | **비순환 게이트(Gate A / Gate B) 및 P3 구현 순서 확정** | - 본 문서 (`00-request-and-status.md`) 및 `A-runtime-delivery.md` 4절 | P3~P6 실행 시 PMS 상태 갱신 |
| **9** | **실제 리팩토링된 코드 (Actual Refactored Code)** | A (Gate A 착수), 후속 구현 워커 | **격리 기반 구현 착수 (`P3` candidate)** | - `src/ttakkari/delivery/validation.py`<br>- `tests/delivery/test_validation_cache.py` | P3 Gate A 격리 공용 기반 계약 구현 완료. 향후 `FND-00`~`FND-05` → P4 → P5 및 Gate B 운영 경로 연동 |
| **10** | **통합 및 배포 증거 (Integration + Deployment Evidence)** | 후속 구현 워커 | **과거 기준선 영수증만 확보 (`P3`~`P6` 대상)** | - `A-runtime-delivery-evidence.json` (`historical_receipt` + `direct_current_observation`) | P3~P6 실제 배포 및 9대 실증 시나리오 실행 영수증 수집 |
| **11** | **전후 실측 성능 비교 (Before-After Measured Performance)** | A, C | **Before 완주 기준선(`1,045.97s`) 및 DB-free 좁은 표본(`3.34s`) 확보** | - 완주 배포 `4a6e09a8` pytest `1,045.97s` (`-x` 조기 중단 `172.23s`/`985.34s`와 구분)<br>- 5파일 DB-free 표본 `35 passed in 3.34s` | C의 프런트/백엔드 시간 기준선 재검증 통과 및 P3/P4/P6 구현 후 동일 조건 After 실측 |
| **12** | **마이그레이션 및 롤백 가이드 (Migration + Rollback Guide)** | A | **Expand-only 스키마 규칙 및 롤백 계약 수립 (`P2`)** | - `architecture-options.md` 4절 및 `A-runtime-delivery.md` 2.4절/4절 | Gate B 백업/복원 리허설 및 P3/P4 실제 롤백 리허설 영수증 반영 |
| **13** | **미결 항목·위험·기술 부채 원장 (Open Items, Risks, Debt)** | A, B, C | **교정 완료 (`P1`/`P2`)** | - 본 문서 5절 및 `A-runtime-delivery.md` 5절 | 각 단계 종료 시 해소 여부 추적 |

---

## 4. 9대 필수 실증 시나리오 및 성공 지표 정의 (설계 목표 vs 현재 기준선 분리)

> **주의**: 아래 9대 시나리오 중 독립 모듈 번들 릴리스(`Scenario 3, 4, 6, 8`)는 현재 `src/ttakkari`에 미구현된 **P4 목표 아키텍처 인수 기준(Target Acceptance Criteria)**이며, P3/P4 구현 후 실제 실행 로그와 영수증으로 입증해야 한다.

### 4.1 9대 필수 실증 시나리오 (9 Verification Scenarios)

1. **Scenario 1 (Module-Only Edit)**: 대상 독립 모듈(잠정 1차: `Creative Studio`) 내부 파일만 수정한 ChangeSet 생성 및 범위 격리 검증 (`classify_change_impact`가 `module_scoped`로 판정하고 전이 의존성 클로저가 완전함을 확인).
2. **Scenario 2 (Module-Only Test)**: 전체 4,473개 백엔드 테스트(`1,045.97s`)를 돌리지 않고, 계약 묶음 + 해당 모듈 테스트 스위트만 선택 실행하여 통과 및 HMAC 서명된 `ValidationReceipt v2`(`EvidenceCacheKey v2` 포함) 발급.
3. **Scenario 3 (Module-Only Release — *P4 구현 목표*)**: 해당 모듈 아카이브/번들만 새 리비전으로 빌드·활성화.
4. **Scenario 4 (Zero Core Redeploy — *P4 구현 목표*)**: 위 모듈 단독 릴리스 과정에서 Core API 세대 슬롯(`8791`/`8792`/`8793`)의 프로세스 PID와 `runtime_generations.id`가 재시작·교체되지 않음을 증명 (`Core redeploy count = 0`).
5. **Scenario 5 (Preserve or Drain Old Job — *P3/P4 구현 목표*)**: 버전 전환 시점에 이미 수락·실행 중이던 이전 버전의 작업(`Run`, `TTS`, `Creative` 렌더/자산 생성)이 끊기거나 `generation_draining`으로 강제 실패하지 않고 `pinned_module_revision`에서 끝까지 완료됨을 입증 (`ACC-01`, `ACC-02`).
6. **Scenario 6 (New Request Uses New Version — *P4 구현 목표*)**: 전환 직후 들어온 신규 모듈 요청이 즉시 새 모듈 리비전으로 라우팅·실행됨을 응답 헤더(`X-Ttakkari-Module-Revision`) 및 영수증으로 입증.
7. **Scenario 7 (Module Failure Isolation & Broad Fail-Closed)**: 대상 모듈 ChangeSet에 의도적 실패를 주입했을 때 해당 모듈 릴리스만 차단되고 무관한 다른 모듈 배포는 정상 진행되며, 반대로 공유 코어/스키마/보안 파일이나 1바이트 변조 영수증이 개입되면 즉시 `broad_fail_closed`로 차단됨을 입증 (`ACC-04`).
8. **Scenario 8 (Module Rollback — *P4 구현 목표*)**: 모듈 릴리스 포인터(`active.json`)를 직전 정상 버전으로 원자적 복원(`os.replace`)하여 Core API 재기동 없이 신속 복구됨을 입증 (근거 없는 '1초 미만' 임의 수치는 배제하고 향후 실제 롤백 소요 시간 측정 대상으로 교정).
9. **Scenario 9 (Restart & Safe Restore Without Blind Replay — *P3 구현 목표*)**: 프로세스 재시작 시 살아있는 소유자(`pid`, `process_started_at`, `pgid`)의 작업은 보존(`skipped_live_lease`)하고, `AccessDenied`/`OSError`는 `UNKNOWN`으로 보존(`skipped_unknown`)하며, `NoSuchProcess`(`ABSENT`)가 확인된 작업만 임의 자동 재큐잉(`resume_interrupted` replay) 없이 `interrupted`로 안전하게 마감됨을 입증 (`ACC-03`).

### 4.2 성공 지표 (Success Metrics)

| 지표명 | P0 현재 기준선 (Evidence-Classified Baseline) | 목표 기준 (Target) | 측정 및 검증 방법 |
| :--- | :--- | :--- | :--- |
| **Unnecessary Core Deploy** | 모듈 1줄 수정에도 Core API 세대 전체 배포 (`100%`) | **`0건`** (독립 모듈 단독 변경 시 Core API 슬롯 무교체, Expand-only 외 마이그레이션 포함 시에만 Core 배포 승격) | 모듈 배포 전후 `/api/version`의 `pid` 및 `runtime_generations.id` 불변 확인 |
| **Unrelated Blocked Deploy** | 국소 모듈 테스트 1건 실패(`LOG-194`, `LOG-195`)로 전체 `main` 배포 차단 | **`0건`** (실패한 ChangeSet만 격리, 타 모듈 ChangeSet 정상 병합·배포) | Scenario 7 실증 로그 |
| **Core Regression** | 기준선 통과 | **`0건`** (계약 테스트 및 실시간 프로브 `/api/health`, `/api/ready`, `/api/version` 100% 통과) | 매 전환 시 비캐시 실시간 프로브 및 계약 테스트 |
| **Ownership Loss / Duplicate Execution** | TTS `queued` 작업 `draining` 시 강제 실패(`generation_draining`), Creative V2 비추적 태스크 및 체크포인트 `fsync` 미비, 레거시 `resume_interrupted` 자동 재큐잉 잔존 | **유실 `0건` / 미검증 자동 재실행 `0건` / 중복 부수효과 `0건`** | `ACC-01`, `ACC-02`, `ACC-03` 통과 |
| **Changed-Size Test Time Reduction** | 완주 배포 게이트(`4a6e09a8`) `pytest` **`1,045.97초`** (`-x` 조기 중단 `172.23s`/`985.34s`와 구분; 5파일 DB-free 정적 표본은 **`3.34초 / 35 passed`**) | **실측 단축치 기록** (사전 개선율 추정치 조작 금지; P3/P4 구현 후 실제 벽시계 시간으로만 보고) | 동일 머신에서 모듈 단독 변경 시 검증 시작~완료 벽시계 초 비교 |

---

## 5. 핵심 위험 및 선결 과제 원장 (Open Items & Risks Ledger)

1. **`RISK-01` (공유 `/back` 작업 트리의 `2 ahead / 118 behind` 분기 및 미커밋 변경 보존)**:
   - 현상: `/Users/winterholic/development/projects/winterholic-ttakkari/back`는 `607f8bda`(`origin/main` `c8f10156`과 `merge-base` `2522d78e` 기준 `2 ahead / 118 behind`)에 있으며 다른 작업자의 미커밋 변경이 존재한다.
   - 대응: 공유 `/back`로의 파일 복사·동기화를 전면 중단하고, 모든 감사·검증·구현은 `origin/main`에서 분기한 격리 git worktree에서만 수행한다.
2. **`RISK-02` (`origin/main`의 기존 배포 차단 결함 1건 잔존 — `LOG-195`)**:
   - 현상: `origin/main` HEAD(`c8f10156`)는 `src/ttakkari/runs/desk_qwen.py`의 `PROMPT_VERSION`이 `'harness-v3-topic-boundary'`인 반면 `tests/test_desk_qwen_service.py`가 `'harness-v2'`를 단정하여 배포 게이트가 `172.23s` 지점에서 중단된다(`e15536bc`).
   - 대응: P3 `Step 0 (FND-00)`에서 최우선으로 정리한다.
3. **`RISK-03` (Mac Studio 호스트 Swap `94.8%~95.3%` 사용 및 세대 전환 시 Qwen 중복 스폰 가능성)**:
   - 현상: 현재 호스트 스냅샷에서 `llama-server`(`pid=44129`, RSS `7.20GiB`)는 1개만 떠 있으나 Swap이 `31.5~31.7 / 33.3 GiB` 사용 중이다. 이 상태에서 세대 전환 시 새 슬롯이 추가 `llama-server` 자식 프로세스를 스폰하면 메모리 압박이 가중될 수 있다.
   - 대응: 도메인별 상주 데몬(대안 3)을 배제하고, P3 `Step 3 (FND-03)`에서 `Desk Qwen`을 세대 외부 단일 인스턴스(`desk_qwen_base_url`)로 분리한다.
4. **`RISK-04` (TTS `_pending_texts` 메모리 유실, Creative V2 `_ASSET_GENERATION_TASKS` 미추적·체크포인트 `fsync` 미비, 레거시 `resume_interrupted` 자동 재큐잉)**:
   - 현상: 세대 전환 시 이미 수락된 TTS 작업이 `generation_draining`으로 강제 실패하고, Creative V2 비동기 자산 생성 태스크가 드레인 카운터에 잡히지 않으며, 비세대 기동 경로에 `resume_interrupted()` 자동 재큐잉이 남아 있다.
   - 대응: P3 `Step 1 (FND-01)` 및 `Step 2 (FND-02)`에서 전수 수정하고 `ACC-01`~`ACC-03`으로 검증한다.
