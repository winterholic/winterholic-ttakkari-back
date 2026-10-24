# 따까리 플랫폼 V2 요구사항 정본 및 전체 진행 상태 원장 (`00-request-and-status.md`)

- 문서 소유: Worker A (Runtime / Delivery / Architecture, 세션 ID `e65430e3-0775-4e2c-910a-5050cdb7d216`)
- 갱신 시각: `2026-10-24T21:54:00+09:00`
- 승인 및 단계 상태:
  - **2026-10-24 사용자 전면 구현 승인 완료**.
  - **초회 감사 완료는 전체 리팩토링 프로젝트 완료가 아니며, 다음 구현을 위한 추가 사용자 승인 대기 단계도 아니다.**
  - 모든 병렬 워커(A, B, C)의 결과가 돌아와 검증·통합되기 전에는 단독으로 모듈 경계를 최종 확정하거나 소스/운영(Live) 동작 변경을 수행하지 않는다 (소스 동작 변경 보류 유지).
  - **중요 (Worker B 검증 대기 격리)**: Worker B(`c1e9cd73-83ef-4240-99c8-809e500bd903`, `back-platform-v2-b` HEAD `86373cedda20e298ad2b82d857352f845ae70980`)의 초회 감사 보고는 실제 실행 모델이 Flash였으며 기준 트리가 공유 `/back` 로컬 HEAD(`607f8bda0df7dbdd458e4f0e54d703ce9edd3def`)였다. 루트 오케스트레이터가 **독립 검증 워커 `1d994e50-c900-4f90-9975-1ff082522a35` (`auto/verification`, GPT-6 Luna 예정)**를 시작했으므로, **검증 완료 전까지 B의 수치(`25모듈 / 497 route / 102 tool / 88 table / 62 migration / 212 config / 321 .py / unmatched 0`, `Creative = artcraft_tasks 1 / FK 0`, `collector = 2 tables`, `cycle = 34`)는 모두 `확인 필요`(`verification_pending`)로 취급하며 확정 모듈 경계나 구현 근거로 채택하지 않는다.**
  - 루트(메인) 오케스트레이터가 A/B(검증 후)/C 감사 보고와 증거를 통합하여 즉시 `P2`(목표 아키텍처·계약 확정) → `P3`~`P6`(공용 기반 및 모듈 전환 코드 구현·배포·실증) 작업을 연속 배정한다.
- 대상 저장소:
  - Backend: `/Users/winterholic/development/projects/winterholic-ttakkari/back` (`origin/main` = `c8f10156845d007ec6496a65d6d8417be19260bd`, 공유 작업 트리 로컬 HEAD = `607f8bda0df7dbdd458e4f0e54d703ce9edd3def`, Live = `9f82c9c16cd604e81ddf43180b0be2e895ec85cb`)
  - Frontend: `/Users/winterholic/development/projects/winterholic-ttakkari/front` (`origin/main` = `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf`)
- PMS 프로젝트 ID: `e62fbc3f-b2d3-40ad-b5fc-687c8ccff286` (개인 범위, `P2` 선행조건 = `P0` + `P1` 동시 충족)

---

## 0. 병렬 감사·검증 워커 ID·담당·산출물 원장 (Parallel Audit & Verification Workers Ledger)

후속 컨텍스트나 새 세션에서도 즉시 이어받을 수 있도록 병렬 감사 워커 및 독립 검증 워커의 확정 ID, 역할, 작업 공간, 담당 산출물, 검증 상태를 기록한다.

| 워커 구분 | 확정 Worker / Session ID | 담당 영역 | 격리 Worktree / 브랜치 / HEAD | 담당 산출물 경로 (`back` 저장소 기준) | 현재 상태 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Worker A** | `e65430e3-0775-4e2c-910a-5050cdb7d216` | **Runtime / Delivery / Architecture 판단** (P0 운영 기준선, ADR0036/0066/세대배포/리스/펜싱, TTS·Qwen 결함, 전역 차단 경로, EvidenceCacheKey, 아키텍처 대안, Baseline Drift 대조) | `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a` (`audit/platform-v2-a-runtime-delivery-20261024`, base `origin/main` `c8f10156845d007ec6496a65d6d8417be19260bd`) | - `review/platform-v2/20261024/A-runtime-delivery.md`<br>- `review/platform-v2/20261024/A-runtime-delivery-evidence.json`<br>- `review/platform-v2/20261024/A-extract-runtime-delivery-evidence.py`<br>- `review/platform-v2/20261024/test_a_runtime_delivery_evidence.py`<br>- `docs/design/platform-v2/00-request-and-status.md`<br>- `docs/design/platform-v2/architecture-options.md` | **초회 감사·설계 및 Baseline Drift 기록 완료** (루트 통합 대기, 소스 동작 변경 보류 유지) |
| **Worker B** | `c1e9cd73-83ef-4240-99c8-809e500bd903` | **Backend / Data / Dependency 감사** (모듈 인벤토리, 의존성 그래프, 순환 참조, DB 테이블·마이그레이션·파일 데이터 소유권) | `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-b` (`audit/platform-v2-b-backend-20261024`, HEAD `86373cedda20e298ad2b82d857352f845ae70980`, base `607f8bda0df7dbdd458e4f0e54d703ce9edd3def`) | - `docs/design/platform-v2/backend-module-inventory.{md,json,csv}`<br>- `docs/design/platform-v2/backend-dependency-graph.{md,json,mmd}`<br>- `docs/design/platform-v2/data-ownership.md`<br>- `review/platform-v2/20261024/backend/{audit_summary.json,unmatched_ledger.json,scan_backend.py}` | **검증 대기 (`verification_pending / 확인 필요`)**: 실제 실행 모델이 Flash였고 `607f8bda` 기준(111커밋 과거)으로 추출됨. 독립 검증 워커 `1d994e50-c900-4f90-9975-1ff082522a35` 검증 완료 전까지 B의 수치를 확정 경계·구현 근거로 채택 금지 |
| **Worker B 독립 검증 워커** | `1d994e50-c900-4f90-9975-1ff082522a35` | **Worker B 산출물 독립 검증 (`auto/verification`, GPT-6 Luna 예정)** (B의 모듈/라우트/도구/테이블/마이그레이션/순환참조 수치 및 `origin/main` `c8f10156` 정본 대조 검증) | 루트 배정 검증 컨텍스트 | Worker B(`c1e9cd73-83ef-4240-99c8-809e500bd903`) 감사 산출물 및 `scan_backend.py` 검증 보고 | **실행 중 (`running`)** |
| **Worker C** | `9a3215de-01d4-4efd-b3ff-9e282de9b766` | **Frontend / Test Architecture 감사** (프런트 라우트·API 호출 매핑, 백엔드/프런트 테스트 분류·실측 기준선, TIA 테스트 영향 제안) | 격리 분석 수행 | - `docs/design/platform-v2/frontend-feature-map.{md,json,csv}`<br>- `docs/design/platform-v2/test-architecture-and-baseline.md`<br>- `docs/design/platform-v2/test-impact-proposal.md`<br>- `review/platform-v2/20261024/frontend-tests/{summary.json,backend-test-classification.json,frontend-api-inventory.json,frontend-routes.json,frontend-test-inventory.json}` | **초회 감사 산출물 기록됨** (루트 통합 대기) |
| *(참고)* 기존 UI 워커 | `6e33860e-3773-48bf-8df5-a3dc2c330a3e` | 메인 화면 카드 겹침·잘림 CSS 수정 (플랫폼 V2 범위 외 선행 종료 작업) | `front` 전용 격리 worktree (종료됨) | `front` 커밋 `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf` (`back` 수정 및 배포 예약 0건 확인 완료) | 종료 (혼입 없음 확인) |

### 0.1 Worker B 검증 대기(`확인 필요`) 주장 격리 및 Baseline Drift (`607f8bda` vs `c8f10156`) 정밀 대조

1. **확정 경계 및 구현 근거로 아직 채택하지 않는 Worker B의 미검증 주장 (`확인 필요`)**:
   - `25모듈 / 497 route / 102 tool / 88 table / 62 migration / 212 config / 321 .py / unmatched 0`
   - `Creative = artcraft_tasks 1 / FK 0` (참고: `artcraft_tasks`는 `src/ttakkari/artcraft.py` 및 `api/artcraft.py`의 레거시 테이블이며 `src/ttakkari/creative/` V1/V2 파일 매니페스트 스토어와 별개이고, `creative/v2/run_extraction.py`가 `ChatSession`/`Run`(`worker_task_type="creative_intent"`)을 생성하는 결합이 누락되어 있음 — 검증 워커 `1d994e50-c900-4f90-9975-1ff082522a35`에서 최종 판정 예정)
   - `collector = 2 tables` (`607f8bda` 시점 수치로, `origin/main` `c8f10156`의 `0063`·`0065`·`0066`·`0067` 마이그레이션이 추가한 신규 `collector_*` 14개 테이블 및 `kb_*` 3개 테이블이 빠져 있음)
   - `cycle = 34`
2. **공유 `/back` 로컬 HEAD(`607f8bda0df7dbdd458e4f0e54d703ce9edd3def`)와 `origin/main`(`c8f10156845d007ec6496a65d6d8417be19260bd`) 간 Baseline Drift 실측 Diff**:
   - **커밋 차이**: `git rev-list --count 607f8bda0df7dbdd458e4f0e54d703ce9edd3def..c8f10156845d007ec6496a65d6d8417be19260bd` = **`111`커밋** (`607f8bda..c8f10156` 전체 `git diff --stat`: **`253 files changed, 68,266 insertions(+), 1,568 deletions(-)`**)
   - **`src/ttakkari` 파이썬 파일 수 및 라인 수 변화**:
     - `607f8bda`: **`321`개 파일 / `71,985`줄**
     - `c8f10156` (`origin/main`): **`385`개 파일 / `94,439`줄** (**순증 `+64`개 `.py` 파일, `+22,454`줄**)
   - **Alembic 마이그레이션 수 및 신규 마이그레이션 5개 (`62`개 → `67`개, 신규 테이블 `21`개)**:
     1. `0063_collector_stage_items.py`: `collector_source_policies`, `collector_host_state`, `collector_fetch_cache`, `collector_items`, `collector_dedupe_bands` (**+5 tables**)
     2. `0064_run_executor.py`: `run_executor_states`, `run_execution_commands`, `run_execution_leases`, `run_process_groups` (**+4 tables**) 및 `runtime_generations.execution_mode` 컬럼 추가
     3. `0065_collector_delivery.py`: `collector_sources`, `kb_document_access`, `kb_observations`, `collector_deliveries`, `collector_daily_budgets`, `collector_reviews`, `collector_quality_models`, `kb_passage_vectors` (**+8 tables**)
     4. `0066_collector_budget.py`: `collector_budget_buckets`, `collector_budget_reservations`, `collector_cache`, `collector_runtime_flags` (**+4 tables**)
     5. `0067_collector_outbox_retry.py`: `collector_outbox` 테이블에 `attempts`, `not_before`, `last_error` 컬럼 추가
   - **`607f8bda..c8f10156` 구간에 신설된 `src/ttakkari` 64개 Python 파일 도메인별 분류**:
     - `creative/v2` 및 API (**20개**): `api/creative_v2.py`, `creative/v2/{__init__,asset_generation,asset_generation_managed,Asset_plan,audio_mix,catalog,compile,contracts,dispatch,Layout_validation,migration,plans,prompts,quality_gate,registries,remotion_renderer,run_extraction,style_dna,verification}.py` (소문자 파일명 기준)
     - `collector` (**21개**): `collector/{active_probe,bandit,budget,calibration,content_extract,Contextual_bandit,contracts,corroboration,coverage,decisions,delivery,discovery,Fetch_pipeline,frontier,host_governor,items,minhash,novelty,outbox,quality_model,source_pack}.py`
     - `kb` 도서관 (**4개**): `api/kb.py`, `kb_contracts.py`, `kb_delivery.py`, `kb_library.py`
     - `harness_view` (**4개**): `api/harness.py`, `harness_view/{__init__,collector,models}.py`
     - `executor` 및 `runs` 테스트 DB 격리 (**3개**): `executor.py`, `executor_reconcile.py`, `runs/test_database.py`
     - `bench` 및 로컬 LLM 평가기 (**12개**): `bench/{__init__,cli,judge,Metrics,runner,sanitize,schema,scorer,sidecar,statistical,storage}.py`, `local_llm_evaluator.py`

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
   - AI CLI 어댑터(`claude`, `codex`, `antigravity`, `muse`, `qwen-local`), 영속 잡/명령 큐(`run_execution_commands`, `tts_jobs` 등), 실행 소유권(`RunExecutionLease`, `fencing_token`, `run_process_groups`), 취소·재개, 호스트 자원 리스(`ResourceLeaseManager` GPU/CPU/메모리 예산), 장기 실행 보장.
3. **Domain Plane (도메인 평면)**:
   - 따까리의 실제 사용자 기능 모듈들(Creative Studio V1/V2, Game Studio, Collector Platform, Knowledge/RAG Library, Memory/Ontology, Meetings/STT, Local TTS, NotebookLM, Reverse Engineering, Design System, TossInvest, PMS/Planner/Checklists, Shares/Transfers/Workspaces 등)을 각자의 데이터 소유권과 실행 특성에 따라 분류·격리.
4. **Delivery Plane (배달 평면)**:
   - ChangeSet 격리(`worktrees`, `change_sets`), 변경 영향 분석(역의존성·공유 계약·스키마·보안), 선택적 테스트 게이트, 계약 검증(`pytest -m contract`), 불변 릴리스 아카이브, 트래픽 전환/핫스왑, 롤백, 결정적 검증 증거(`EvidenceCacheKey` 및 `ValidationReceipt`).

### 1.3 절대 불변 조건 (Hard Invariants)

1. **기능·데이터 무손실**: 모든 기존 기능과 사용자 데이터를 100% 보존한다. DB 재구축이나 데이터 유실을 절대 금지한다.
2. **실행 중 작업 보호**: 실행 중인 `Run`이나 미디어·수집 작업을 배포나 전환 때문에 임의로 중단(`kill`)하거나 강제 실패시키지 않는다.
3. **임의 자동 복구(Blind Replay/Resume) 금지**: 소유권이 증명되지 않은 작업을 재기동 시 자동으로 처음부터 다시 돌리거나 이어서 실행하지 않는다. 커널 프로세스 신원(`pid`, `pgid`, `process_started_at`)과 리스·펜싱 토큰을 대조해 살아있는 소유자는 보존하고, 죽은 소유자는 펜싱 후 안전하게 실패/수동 재개 대기로 마감한다.
4. **Exactly-Once 가정 금지**: 모든 명령·이벤트·외부 부수효과는 멱등 키(`idempotency_key`), 펜싱 토큰(`fencing_token`), 내용 해시(`sha256`), 원자적 교체(`os.replace`), 또는 명시적 보상(compensation)으로 중복을 방지한다.
5. **외부 인터페이스 하위 호환**: 기존 HTTP API 경로, MCP 도구 카탈로그, 프런트엔드 라우트·계약을 깨지 않는다.
6. **Core 3슬롯과 모듈 릴리스 생명주기의 분리**: Core API의 무중단 세대 슬롯(`8791`/`8792`/`8793`)과 개별 모듈의 릴리스·실행 생명주기를 혼동하지 않는다. 모든 모듈에 3슬롯을 복제하지 않는다.
7. **외부 무거운 인프라 도입 금지**: 신규 Redis, 외부 메시지 브로커, Kubernetes, 서비스 메시, 분산 DB 클러스터 도입을 금지한다. 기존 단일 호스트 PostgreSQL(Job/Command/Queue/Lease), 로컬 파일 캐시/가중치/아티팩트 저장소, `ResourceLeaseManager`를 재사용한다.
8. **운영·보안 경계 준수**: 가드 우회, 보호된 `~/.ttakkari` 설정·계정·하네스·로그 직접 스캔, 에이전트 내부에서의 `launchctl`/재시작 스크립트 실행, 미검증 감독(Supervisor) 교체를 금지한다.

---

## 2. PMS 단계별(P0~P6) 진행 상태 원장

PMS 프로젝트 `e62fbc3f-b2d3-40ad-b5fc-687c8ccff286`의 7개 단계에 대해 `계획(Plan)`, `코드(Code)`, `테스트(Test)`, `운영(Live)`, `미검증(Unverified)` 상태를 분리하여 기록한다.

| PMS 단계 ID | 단계명 | 현재 PMS 상태 | 계획(Plan) | 코드(Code) | 테스트(Test) | 운영(Live) / 실측(Measured) | 미검증 / 잔여 과제 (`확인 필요`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **`P0`** (`06d3fd18-d2b6-425c-b280-89afa6c3b836`) | **P0 운영·복구·성능 기준선** | `doing` | 완료 | 감사 추출기(`A-extract-runtime-delivery-evidence.py`) 및 백업/복원 스크립트 구현됨 | 추출기 단위 테스트 및 계약 테스트 39건 통과 (`5.00s`) | 활성 리비전(`9f82c9c1`, Slot 1 `8792`), 감독 리비전(`e817fc19`), 배포 소요(`1,576.48s`, pytest `1,045.97s`), 실패 분포(34그룹/56회 중 pytest 33그룹/54회), 호스트 부하(Swap `95.3%`), 프런트(`5.48s`) 실측 완료 | `~/.ttakkari-backups/latest.json` 실제 최근 백업 시각 및 `supervisor.json` 원문은 보호 영역 밖이라 `확인 필요` |
| **`P1`** (`9f414a3c-4533-4e1d-93ec-52ecb901864d`) | **P1 전체 기능·데이터·의존성 감사** | `doing` | 완료 | A(런타임·배달), B(백엔드·데이터, 검증 대기), C(프런트·테스트) 감사 스크립트 및 인벤토리 산출됨 | A/C 정적 검증 통과; B 산출물은 독립 검증 워커 `1d994e50-c900-4f90-9975-1ff082522a35` 검증 중 (`확인 필요`) | `origin/main`(`c8f10156`, 385파일/94,439줄/67마이그레이션) 및 로컬 공유 작업 트리(`607f8bda`, 111커밋 차이) Baseline Drift 정밀 대조 완료 | 독립 검증 워커(`1d994e50-c900-4f90-9975-1ff082522a35`) 검증 완료 후 Worker B의 기준 커밋(`607f8bda`)과 `origin/main`(`c8f10156`) 사이 111커밋 증분(+64 `.py`, +5 마이그레이션, +21 테이블) 통합 필요 |
| **`P2`** (`916a7835-b5e6-4daf-a687-3f3e647eb9ec`) | **P2 목표 아키텍처·계약·전환 전략** *(선행조건: `P0` + `P1` 모두 충족)* | `todo` | 대안·계약 초안 완료 (`architecture-options.md`, 모든 워커 결과 검증·통합 전 경계 미확정) | 미착수 (현 단계 소스/운영 동작 변경 보류 유지) | 미실행 (P2 확정 후 계약 테스트 추가) | 미적용 | 루트 오케스트레이터가 A(`e65430e3`), B(`c1e9cd73` + 검증 워커 `1d994e50`), C(`9a3215de`) 결과를 통합해 최종 모듈 경계, Split vs Retain 결정표, API/Job/Event/Module 계약서 확정 후 즉시 P3 배정 (사용자 추가 승인 대기 아님) |
| **`P3`** (`ec3786e9-27ba-4d49-9f35-65e6f21c0bf4`) | **P3 공용 기반·테스트·Delivery 구현** | `todo` | 설계 완료 (`A-runtime-delivery.md` 6.1절) | 기존 ADR 0036(`0064`)/0066(`0062`) 구현되어 있으나 `feature-off` 및 일부 결함(TTS/Qwen/전체 pytest) 존재 | 기존 단위 테스트 존재, 신규 기반 테스트는 구현 시 작성 | `feature-off` | 1) TTS payload DB 영속화 및 draining 보호, 2) Qwen 세대 외부 분리, 3) `conftest.py` 정적 게이트 DB 분리 + `EvidenceCacheKey` 연동, 4) `RunExecutor` 활성화 |
| **`P4`** (`47f15831-5f97-4429-b381-fddd87be1549`) | **P4 첫 독립 모듈 실제 전환** | `todo` | 1순위 후보 `Creative Studio` 선정 및 전환 설계 완료 | 미착수 | 미실행 | 미적용 | `Creative Studio` 모듈 릴리스 번들/워커 분리 및 9대 실증 시나리오 실측 검증 필요 |
| **`P5`** (`8fd29eab-c45f-4097-a6f5-35ab57515d8d`) | **P5 나머지 기능 단계 전환** | `todo` | 후보 우선순위(`Game Studio` → `Collector/KB` → 기타) 수립 | 미착수 | 미실행 | 미적용 | P4 실측 성공 후 순차 진행 |
| **`P6`** (`4fce0913-3e1c-4bdf-87de-0978cb739180`) | **P6 통합·보안·복구·운영 전환** | `todo` | 검증 및 롤백 기준 정의 완료 | 미착수 | 미실행 | 미적용 | 전후 성능 비교 실측, 마이그레이션·롤백 가이드 완성, 잔여 부채 정리 |

---

## 3. 최종 13대 산출물(13 Final Deliverables) 커버리지 및 미완 원장

전체 프로젝트가 최종 완료되기 위해 반드시 갖춰야 하는 13대 산출물의 현재 상태, 담당 워커, 산출 파일 경로, 잔여 작업을 아래 원장으로 관리한다.

| 번호 | 산출물 명칭 | 주담당 / 협조 | 현재 상태 | 현재 확보된 산출물 경로 (절대 경로) | 완료를 위한 잔여 작업 (미완 원장) |
| :---: | :--- | :---: | :---: | :--- | :--- |
| **1** | **전체 모듈 인벤토리 (Full Module Inventory)** | B, C (협조 A) | **초안 확보 (`P1`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/design/platform-v2/backend-module-inventory.md` (`.json`, `.csv`)<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/design/platform-v2/frontend-feature-map.md` (`.json`, `.csv`)<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery-evidence.json` | Worker B 인벤토리(`607f8bda` 기준 321파일)에 Worker A가 추출한 `origin/main`(`c8f10156` 기준 385파일) 증분 모듈(`creative/v2`, `kb_*`, `bench` 등) 병합 |
| **2** | **현재 의존성 그래프 (Current Dependency Graph)** | B, A | **초안 확보 (`P1`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/design/platform-v2/backend-dependency-graph.md` (`.json`, `.mmd`)<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery-evidence.json` (`codebase_metrics.modules`) | `origin/main` 최신 커밋 기준 순환 의존성 해소 대상 확정 |
| **3** | **목표 아키텍처 다이어그램 (Target Architecture Diagram)** | A | **대안 수립 (`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/architecture-options.md` | 메인 통합 후 권장안(대안 2)을 확정 다이어그램으로 승격 |
| **4** | **분리 대 유지 결정표 (Split vs Retain Decisions)** | A, B, C | **기준 및 초안 수립 (`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/architecture-options.md`<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/design/platform-v2/backend-module-inventory.md` | 전체 도메인 모듈별 최종 실행 형태(In-process library / Durable worker / Independent module release) 확정 서명 |
| **5** | **공유 자원 및 데이터 소유권 원장 (Shared Resources + Data Ownership)** | B, A | **초안 확보 (`P1`/`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/design/platform-v2/data-ownership.md`<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery.md` (GPU 리스, Qwen, TTS, PG 테이블, 파일 저장소) | `0063`~`0067` 신규 테이블(`run_executor_*`, `collector_outbox_*` 등)을 `data-ownership.md` 표에 통합 |
| **6** | **API·Job·Event·Module 계약 명세 (Contracts)** | A, B, C | **핵심 런타임·배달 계약 수립 (`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/architecture-options.md`<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery.md` | P3/P4 구현 시 JSON 스키마 및 `pytest -m contract` 자동 검증 테스트 코드로 고정 |
| **7** | **테스트 및 Delivery 파이프라인 재설계 (Test + Delivery Redesign)** | C, A | **초안 확보 (`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/design/platform-v2/test-architecture-and-baseline.md`<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back/docs/design/platform-v2/test-impact-proposal.md`<br>- `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery.md` (5장) | P3에서 `tests/conftest.py` 정적 분리, `EvidenceCacheKey`, `generation_deployment.py` 영수증 검증 실제 구현 |
| **8** | **단계별 WBS·PMS 및 선행조건 (Phased WBS-PMS + Preconditions)** | A (메인 총괄) | **본 문서 및 PMS 등록 완료 (`P0`~`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/00-request-and-status.md`<br>- PMS Project `e62fbc3f-b2d3-40ad-b5fc-687c8ccff286` | P3~P6 세부 구현 태스크 분할 및 진행률 갱신 |
| **9** | **실제 리팩토링된 코드 (Actual Refactored Code)** | 메인이 후속 워커 배정 | **미실행 (`P3`~`P5` 대상)** | - 현 단계는 운영·소스 변경 금지 규칙에 따라 감사 추출기 및 테스트만 작성 | P3(기반: TTS/Qwen/Executor/Delivery) → P4(Creative Studio) → P5(후속 모듈) 코드 구현 및 `main` 병합 |
| **10** | **통합 및 배포 증거 (Integration + Deployment Evidence)** | 메인이 후속 워커 배정 | **P0 기준선만 확보 (`P3`~`P6` 대상)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery-evidence.json` (P0 기준선) | P3~P6 변경분 실제 배포 예약(`request_backend_deploy`) 및 9대 실증 시나리오 실행 영수증 수집 |
| **11** | **전후 실측 성능 비교 (Before-After Measured Performance)** | A, C (P0 완료 / P6 후측정) | **Before 기준선 실측 완료 (`P6` After 대기)** | - Before: 배포 전체 `1,576.48s`, 백엔드 `pytest` `1,045.97s`, 정적 계약(무conftest) `5.00s`, 프런트 `vitest` `5.48s` | P3/P4/P6 구현 후 동일 조건에서 After 실측치 측정 및 비교표 작성 (사전 수치 조작 절대 금지) |
| **12** | **마이그레이션 및 롤백 가이드 (Migration + Rollback Guide)** | A | **설계안 확보 (`P2`)** | - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/docs/design/platform-v2/architecture-options.md` | P3/P4/P6 실제 롤백 리허설 결과 반영 |
| **13** | **미결 항목·위험·기술 부채 원장 (Open Items, Risks, Debt)** | A, B, C | **초회 원장 확보 (`P1`/`P2`)** | - 본 문서 5절 및 `A-runtime-delivery.md` | 각 단계 종료 시 해소 여부 추적 |

---

## 4. 9대 필수 실증 시나리오 및 성공 지표 정의

플랫폼 V2 전환 완료를 주장하기 위해서는 P4(첫 독립 모듈 전환) 및 P6(최종 통합)에서 아래 **9개 시나리오를 실제 실행 증거(명령, 로그, 타임스탬프, 응답 본문)로 입증**해야 한다.

### 4.1 9대 필수 실증 시나리오 (9 Verification Scenarios)

1. **Scenario 1 (Module-Only Edit)**: 대상 독립 모듈(1차: `Creative Studio`) 내부 파일만 수정한 ChangeSet 생성 및 범위 격리 검증 (`diff_guard` 및 변경 영향 분류기가 `module_scoped`로 판정).
2. **Scenario 2 (Module-Only Test)**: 전체 4,473개 백엔드 테스트(17분 25초)를 돌리지 않고, 빠른 계약 묶음(`pytest -m contract`) + 해당 모듈 테스트 스위트만 선택 실행하여 검증 통과 및 `ValidationReceipt`(`EvidenceCacheKey` 포함) 발급.
3. **Scenario 3 (Module-Only Release)**: 해당 모듈 아카이브/번들만 새 리비전으로 빌드·활성화.
4. **Scenario 4 (Zero Core Redeploy)**: 위 모듈 단독 릴리스 과정에서 Core API 세대 슬롯(`8791`/`8792`/`8793`)의 프로세스 PID와 세대 ID(`5ef462e5-...` 등)가 재시작·교체되지 않음을 증명 (`Core redeploy count = 0`).
5. **Scenario 5 (Preserve or Drain Old Job)**: 모듈 버전 전환 시점에 이미 실행 중이던 이전 버전의 모듈 작업(또는 `Run`, `TTS`, `Creative` 렌더/자산 생성)이 끊기거나 `generation_draining`으로 강제 실패하지 않고 끝까지 완료(preserve/drain)됨을 입증.
6. **Scenario 6 (New Request Uses New Version)**: 전환 직후 들어온 신규 모듈 요청이 즉시 새 모듈 리비전으로 라우팅·실행됨을 응답·영수증의 버전 필드로 입증.
7. **Scenario 7 (Module Failure Isolation)**: 대상 모듈 ChangeSet에 의도적 실패(또는 런타임 오류)를 주입했을 때 해당 모듈 릴리스만 차단·격리되고, 무관한 다른 모듈(`Desk`, `Games`, `Collector`) 및 Core API의 배포·운영은 정상 진행됨을 입증.
8. **Scenario 8 (Module Rollback)**: 모듈 릴리스 포인터를 직전 정상 버전으로 즉시 롤백하고, Core API 재기동이나 DB 손실 없이 정상 동작이 복구됨을 입증.
9. **Scenario 9 (Restart & Safe Restore)**: 프로세스 재시작 시 살아있는 소유자 프로세스의 작업은 보존되고, 죽은 소유자의 작업은 임의 자동 재실행(blind replay) 없이 펜싱·정리된 뒤 명시적 재시도로 안전하게 복구됨을 입증.

### 4.2 성공 지표 (Success Metrics)

| 지표명 | P0 현재 기준선 (Measured Baseline) | 목표 기준 (Target) | 측정 및 검증 방법 |
| :--- | :--- | :--- | :--- |
| **Unnecessary Core Deploy** | 모듈 1줄 수정에도 Core API 세대 전체 배포 (`100%`) | **`0건`** (독립 모듈 단독 변경 시 Core API 슬롯 무교체) | 모듈 배포 전후 `/api/version`의 `pid` 및 `runtime_generations.id` 불변 확인 |
| **Unrelated Blocked Deploy** | 국소 모듈 테스트 1건 실패(`LOG-194`, `LOG-195`)로 전체 `main` 배포 차단 | **`0건`** (실패한 ChangeSet만 격리, 타 모듈 ChangeSet 정상 병합·배포) | Scenario 7 실증 로그 |
| **Core Regression** | 기준선 통과 | **`0건`** (`pytest -m contract` 및 Core 스모크 100% 통과) | 매 전환 시 실시간 프로브 및 계약 테스트 |
| **Ownership Loss / Duplicate Execution** | TTS `queued` 작업이 `draining` 시 강제 실패(`generation_draining`), Creative V2 비추적 태스크 증발 위험 존재 | **유실 `0건` / 중복 실행 `0건`** | Scenario 5 및 Scenario 9 펜싱 토큰·멱등성 테스트 |
| **Changed-Size Test Time Reduction** | 배포 게이트 `pytest` **`1,045.97초` (17분 25.97초)** | **실측 단축치 기록** (사전 개선율 추정치 조작 금지; P3/P4 구현 후 실제 벽시계 시간으로만 보고) | 동일 머신에서 모듈 단독 변경 시 검증 시작~완료 벽시계 초 비교 |
| **Retry Isolation** | 실패 시 전체 스위트 `--stepwise` 재실행 | 모듈 단위 국소 재시도 및 상한(RepairPolicy max 2회) 강제 | ChangeSet 검증 로그 |
| **Contract Compatibility** | 프런트 36 라우트 / 179 API 호출 (백엔드 497 라우트 / MCP 102 도구는 Worker B 보고치로 `확인 필요` — 검증 워커 `1d994e50` 검증 중) | **하위 호환 100% 유지** | `tests/test_route_inventory.py`, `tests/test_context.py` |
| **Security Guarantee Retained** | 보안 게이트(`test_security_gate.py`) 통과 | **보안 불변 조건 100% 유지** (인증·가드·경로 격리·시크릿 보호) | `tests/test_security_gate.py`, `tests/test_security_gate_inventory.py` |

---

## 5. 초회 감사에서 식별된 핵심 위험 및 선결 과제 (Open Items & Risks Ledger)

1. **`RISK-01` (공유 `/back` 작업 트리의 미커밋 변경 43건과 111커밋 뒤처짐)**:
   - 현상: `/Users/winterholic/development/projects/winterholic-ttakkari/back`는 `607f8bda`에 머물러 있고 `collector` 관련 미커밋 파일 다수가 놓여 있다.
   - 대응: 모든 P3~P6 구현 워커는 반드시 `origin/main`(`c8f10156` 이후)에서 분기한 **독립 git worktree**에서 작업해야 하며, 공유 `/back` 작업 트리를 직접 커밋하거나 덮어쓰면 안 된다.
2. **`RISK-02` (`origin/main`의 기존 배포 차단 결함 1건 잔존 — `LOG-195`)**:
   - 현상: 현재 `origin/main` HEAD(`c8f10156`)는 `src/ttakkari/runs/desk_qwen.py`의 `PROMPT_VERSION`이 `'harness-v3-topic-boundary'`로 바뀐 반면 `tests/test_desk_qwen_service.py`가 `'harness-v2'`를 단정하고 있어 배포 게이트가 막혀 있다(`e15536bc`). (직전 `063002a`의 `LOG-194`는 `c8f10156`에서 테스트 통과 확인됨).
   - 대응: P3 첫 커밋 전에 `tests/test_desk_qwen_service.py`의 기대값 불일치를 먼저 정리해야 `main` 배포 파이프라인이 정상 개통된다.
3. **`RISK-03` (Mac Studio 호스트 Swap `95.3%` 포화)**:
   - 현상: `server_resources` 실측 결과 Swap이 `31.7 / 33.3 GiB`(`95.3%`) 사용 중이다.
   - 대응: 상주 파이썬 프로세스나 추가 `llama-server` 인스턴스를 늘리는 아키텍처(대안 3)를 엄격히 배제하고, Desk Qwen을 세대 외부 단일 프로세스로 통합하며, 무거운 테스트는 반드시 `scripts/pytest_slots.py`(동시 슬롯 2개 제한) 및 선택적 테스트 게이트를 통해 실행한다.
4. **`RISK-04` (TTS `_pending_texts` 메모리 유실 및 Creative V2 `_ASSET_GENERATION_TASKS` 미추적)**:
   - 현상: 세대 전환 시 이미 접수된 TTS 작업이 강제 실패하고, 실행 중인 Creative V2 자동 자산 생성이 추적되지 않는다.
   - 대응: P3 공용 기반 단계에서 최우선으로 수정한다.
