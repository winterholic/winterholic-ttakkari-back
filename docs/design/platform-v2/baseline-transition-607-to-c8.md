# 플랫폼 V2 베이스라인 전환 및 분모 전이 원장 (Baseline Transition: 607f8bd -> c8f1015)
> 기준 코드: `c8f10156845d007ec6496a65d6d8417be19260bd` (최신 origin/main clean snapshot)  
> 이전 로컬 기준: `607f8bda0df7dbdd458e4f0e54d703ce9edd3def` (과거 로컬 snapshot)  
> 작성일: 2026-10-24 | 작성: P1 B Backend 감사 교정 워커  

---

## 1. 베이스라인 전환 개요

P1 원 감사 commit(`86373ced`) 및 1차 교정 감사(`2cc4169`)는 로컬 머지 기준선인 `607f8bda`를 baseline으로 사용하였습니다.  
그러나 Worker A 정본 commit(`fddad5e1`)의 기반이자 원격 저장소의 공식 `origin/main` 기준선은 **`c8f10156845d007ec6496a65d6d8417be19260bd`**임이 확인되었습니다.

> [!IMPORTANT] **임의 합산 및 동일시 금지 원칙**
> - 과거 로컬 snapshot인 `607f8bda`와 최신 clean snapshot인 `c8f10156`은 분모와 커밋 이력이 서로 상이한 독립적인 상태입니다.
> - 두 스냅샷을 임의로 합산하거나 동일시하지 않으며, `607f8bda` 기반 교정 증거를 그대로 보존한 채, 본 원장을 통해 `607f8bda -> c8f10156` 간의 **추가/삭제/변경 분모와 미확인 원장**을 전수 목록화하여 분리 보존합니다.

---

## 2. Git 토폴로지 독립 검증

A 감사 문서의 기술 내용("111 behind / 64 files / 5 migrations")에 대해 `git` 실제 객체 DB 및 코드를 통해 독립 검증을 수행한 결과는 다음과 같습니다:

| 항목 | A 문서 기술 내용 | 실제 Git 독립 검증 결과 | 검증 판정 |
|---|---|---|---|
| **Behind 커밋 수** | 111 behind | **118 commits** (`607f8bda..c8f10156`) | **사실 불일치 (불일치 정정)** |
| **공통 조상 (Merge Base)** | 미기술 | `2522d78e29b14ffb29d8536bcf833d832b4f87e1` | 실측 확인 |
| **Diverged 로컬 커밋** | 미기술 | **2 commits** (`c8..607`: `607f8bd`, `66c8402`) | 실측 확인 |
| **신규 Migration 수** | 5 migrations | **5 migrations** (`0063`~`0067`) | **일치** |
| **신규 소스 파일 수** | 64 files | **64 files** (`src/**/*.py` 신규 추가) | **일치** |
| **전체 변경 파일 수** | 미기술 | **366 files** (diff 전체 파일 수) | 실측 확인 |

- **검증 결론**: A 문서의 "111 behind"는 공통 머지 베이스 계산 오류 또는 도중 시점의 수치로 추정되며, 실제 커밋 거리는 **118 커밋**입니다. 반면 `src/**/*.py` 64개 신규 파일 및 5개 신규 마이그레이션 수치는 코드베이스와 정확히 일치함을 확인하였습니다.

---

## 3. 분모 전수 대조표 (Denominators Comparison: 607 vs c8)

`c8f10156` clean isolated snapshot(`/Users/winterholic/development/ttakkari-desk-topic-20261024`)에서 독립적으로 추출한 엔티티 수치와 `607f8bda` baseline의 대조표입니다:

| 구분 (Metric) | 이전 Baseline (`607f8bda`) | 최신 Origin/Main (`c8f10156`) | 전이 차이 (Delta) | 변경 유형 |
|---|---|---|---|---|
| **Routes (API/WS)** | 497 | **579** | **+82개** | 순수 추가 (삭제 0개) |
| **MCP Tools** | 102 | **133** | **+31개** | 순수 추가 (삭제 0개) |
| **DB Tables (ORM)** | 88 | **106** | **+18개** | 순수 추가 (삭제 0개) |
| **Alembic Migrations** | 62 | **67** | **+5개** | 순수 추가 (삭제 0개) |
| **Settings / Configs** | 212 | **231** | **+19개** | 순수 추가 (삭제 0개) |
| **Source Files (`src/**/*.py`)** | 321 | **385** | **+64개** | 순수 추가 (삭제 0개) |
| **총 코드 라인 수 (LoC)** | 71,985 | **94,439** | **+22,454줄** | 순수 증가 |

---

## 4. 세부 추가/변경 내역 전수 목록

### 4.1 신규 추가 Alembic 마이그레이션 (5건)
선형 마이그레이션 체인이 `0062`에서 `0067`로 전진함 (단일 선형 체인 유지):
1. `0063_collector_stage_items.py`: 웹 수집기 스테이지 아이템 및 파이프라인 테이블 확장
2. `0064_run_executor.py`: 격리 에이전트 런타임 프로세스 그룹 및 실행자 스키마
3. `0065_collector_delivery.py`: 수집기 아웃박스 및 전달 상태 추적
4. `0066_collector_budget.py`: 수집기 일일 예산 및 도메인별 폴라이트 레이트 제한
5. `0067_collector_outbox_retry.py`: 수집기 아웃박스 재시도 및 지수 백오프 제어

### 4.2 신규 추가 DB 테이블 (18개)
1. `collector_budget_buckets` (`domain.collector`)
2. `collector_budget_reservations` (`domain.collector`)
3. `collector_cache` (`domain.collector`)
4. `collector_daily_budgets` (`domain.collector`)
5. `collector_dedupe_bands` (`domain.collector`)
6. `collector_deliveries` (`domain.collector`)
7. `collector_fetch_cache` (`domain.collector`)
8. `collector_host_state` (`domain.collector`)
9. `collector_items` (`domain.collector`)
10. `collector_quality_models` (`domain.collector`)
11. `collector_reviews` (`domain.collector`)
12. `collector_runtime_flags` (`domain.collector`)
13. `collector_source_policies` (`domain.collector`)
14. `collector_sources` (`domain.collector`)
15. `kb_document_access` (`domain.knowledge_rag`)
16. `kb_observations` (`domain.knowledge_rag`)
17. `kb_passage_vectors` (`domain.knowledge_rag`) — **주의: verifier DB fixture 충돌 관련 미확인 가설 대상 테이블**
18. `runtime_process_groups` (`exec.run_engine`)

### 4.3 신규 추가 소스 파일 (64개)
- **수집기 플랫폼 (`domain.collector`, 21개 파일)**:
  `blobs.py`, `budget.py`, `budget_models.py`, `cache.py`, `control.py`, `control_models.py`, `fetcher.py`, `learning.py`, `netguard.py`, `parse.py`, `pilot.py`, `pipeline.py`, `policy.py`, `polite.py`, `profiles.py`, `quality.py`, `quality_service.py`, `robots.py`, `runtime.py`, `store.py`, `textproc.py`
- **크리에이티브 스튜디오 V2 (`domain.creative_studio`, 18개 파일)**:
  `creative/records.py`, `creative/v2/__init__.py`, `asset_generation.py`, `asset_generation_managed.py`, `capabilities.py`, `catalog_data.py`, `dispatch.py`, `duration.py`, `engines.py`, `extraction.py`, `intent.py`, `matching.py`, `plans.py`, `probe.py`, `registry.py`, `registry_data.py`, `report.py`, `run_extraction.py`, `style_dna.py`
- **하네스 뷰 및 시스템 제어 (`core.system_routing`, 10개 파일)**:
  `harness_view/__init__.py`, `extras.py`, `layers.py`, `model.py`, `projects.py`, `safe.py`, `service.py`, `sources.py`, `taxonomy.py`, `ttakkari_side.py`
- **지식 베이스 라이브러리/검색 (`domain.knowledge_rag`, 5개 파일)**:
  `kb_delivery.py`, `kb_embedding_worker.py`, `kb_library.py`, `kb_retrieval.py`, `kb_vector_models.py`
- **실행자 격리 엔진 (`exec.run_engine`, 4개 파일)**:
  `executor.py`, `executor_reconcile.py`, `runs/test_database.py`
- **신규 API 라우터 (6개 파일)**:
  `api/collect_control.py`, `api/creative_mcp.py`, `api/creative_v2.py`, `api/harness.py`, `api/kb_agent.py`, `api/kb_library.py`

### 4.4 신규 추가 MCP 도구 (31개)
- Comfy / Creative 계열 (16개): `comfy_cancel`, `comfy_job`, `comfy_jobs`, `comfy_profiles`, `comfy_readiness`, `comfy_reconcile`, `comfy_submit`, `creative_v2_auto_assets_retry`, `creative_v2_auto_assets_start`, `creative_v2_auto_assets_status`, `creative_v2_capabilities`, `creative_v2_catalog`, `creative_v2_plan`, `creative_v2_plan_approve`, `creative_v2_plan_create`, `creative_v2_plan_edit`
- Knowledge Base 계열 (5개): `kb_document`, `kb_documents`, `kb_evidence`, `kb_search`, `knowledge_recall`
- Research / Memo / Event 계열 (10개): `manage_research`, `list_events`, `list_memo_categories`, `list_memo_tags`, `list_memos`, `search_memos`, `update_memo`, `delete_memo`, `update_event`, `delete_event`

### 4.5 신규 추가 설정 필드 (19개)
`collector_concurrency`, `collector_disk_cap_mb`, `collector_domain_rate`, `collector_max_fetch_kb`, `collector_proxy_url`, `comfy_direct_url`, `comfy_lease_timeout_seconds`, `harness_view_enabled`, `kb_vector_backend`, `kb_vector_dim`, `kb_vector_hnsw_m`, `kb_vector_hnsw_ef`, `run_executor_concurrency`, `run_executor_cpu_shares`, `run_executor_memory_mb`, `run_executor_tmp_quota_mb`, `run_executor_workspace_quota_mb`, `transfers_rate_limit_per_minute`, `transfers_stream_buffer_bytes`

---

## 5. 미확인 원장 (Unverified / Unknown Ledger)

1. **`kb_passage_vectors` 테이블의 소스 기원 및 DB 잔여 의존성**:
   - 이전 독립 검증(verifier `1d994e50`)에서 발생한 `DROP TABLE kb_passages` 의존성 충돌(`constraint kb_passage_vectors_passage_id_fkey`)과 관련하여:
   - `kb_passage_vectors`는 `607f8bda` baseline에는 없었으나 최신 `c8f10156`의 `src/ttakkari/kb_vector_models.py`에 새로 등록된 ORM 테이블로서, 소스 유래는 확인되었습니다.
   - 단, 실제 테스트 실행 시 참조된 DB의 구체적 생성 유래·접속 대상·과거 마이그레이션 이력은 미확인 상태이므로, 공유 테스트 DB 오염은 **미확인 가설**로 기록합니다. (실제 DB 조사나 drop 재실행 금지)
2. **최신 `c8f10156` 엔티티와 소스 blob 대응의 미증명 (unknown)**:
   - 최신 `c8f10156`의 라우트(579개), 도구(133개), 테이블(106개) 등과 소스 blob 간의 정확한 바인딩 및 독립 reconciliation은 전수 검증되지 않았으며, 소스 blob 대응의 미증명 부분은 **`unknown`**으로 분류합니다.
3. **64개 신규 소스 파일의 런타임 결합도 미측정**:
   - `c8f10156`에서 추가된 `collector` 파이프라인(21개 파일) 및 `creative v2`(18개 파일)의 실제 런타임 메모리 사용량, 파일시스템 I/O 부하, 외부 네트워크 레이턴시는 실측 실행 증거가 없으므로 **`미측정 (추후 런타임 프로브 필요)`**으로 분류합니다.
4. **물리 PostgreSQL DB 스키마 미대조**:
   - 본 인벤토리는 `Base.metadata`(106 tables) 및 `alembic/versions`(67 files) 정적 코드 기반이며, 실제 운영 DB의 트리거, 파티셔닝, 확장 테이블은 직접 쿼리하지 않았습니다.

---

## 6. Frontend 분리 상태 기록 (Worker C 잔여 영역)

- **Worker B의 책임 범위**: 백엔드 모듈 인벤토리, 데이터 소유권, Alembic 마이그레이션, 의존성 그래프에 한정하며 프론트엔드 소스는 수정하지 않습니다.
- **Frontend Worktree 현황 (`/Users/winterholic/development/projects/winterholic-ttakkari/front`)**:
  - Remote Ref (`origin/main`): `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf`
  - Local Checkout Ref: `630040b1b7007a219a93c9d3d4491fab714c555d` (ahead 3, behind 105)
  - Dirty Source: 30개 수정 파일(UI 컴포넌트, PMS board, Composer, types.ts 등), 7개 untracked 파일
  - 잔여 작업: Worker C(Frontend/Test)가 독립된 clean worktree에서 `dbe0c9f` 기준으로 프론트엔드 엔드포인트/도구 소비 매핑을 검증하고 dirty source hash를 분리 보고해야 합니다.
