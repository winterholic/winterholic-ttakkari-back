# KB v3 API + Fixes 통합 검증 및 고정 (KB3-402, KB3-103) — 2026-10-25

## 1. 범위와 체크아웃

- **저장소 위치**: `~/development/projects/winterholic-ttakkari/back-kb-v3-api`
- **대상 브랜치**: `kb-v3/api-20261025`
- **통합 대상**:
  - API 브랜치 HEAD: `353d2d84784a0d9f485db0daef481da964d4ad9b` (KB3-103 멱등 파이프라인 잡 백필)
  - Fixes 브랜치 HEAD: `e7fb18fc9110a26ffcc0e98f8cd9f733f0d020e2` (현재성 관측 원장 단일화, kept_separate 충돌 종결, 접근 행 없는 문서 work_only 격리, L1 대소문자 매칭 등)
  - 공통 조상: `d15601b885aa68670f5375e586504b89faa44353` (integration2)
- **금지 원칙 준수**:
  - 원 구현 재개발 없음, 양쪽 의도 완전 보존.
  - main 병합, 원격 push, 백엔드 배포, PMS done 상태 변경 일체 미실행.
  - 다른 워커 생성 없음, 다른 Run의 outbox 접근 없음.
  - 공유 `TTAKKARI_TEST_DB` 미사용(`env -u TTAKKARI_TEST_DB`), 프로세스 격리 임시 PG 클러스터 활용. 광역 pkill 없음.

---

## 2. 병합 충돌 해결 내역 (5개 파일)

| 충돌 파일 | 충돌 내용 | 해결 및 양쪽 의도 보존 방식 |
|---|---|---|
| `src/ttakkari/api/kb_library.py` | 모듈 docstring 충돌 | API v3 통합 공간 설명(KB3-402)과 fixes의 "접근 행 없는 문서는 work_only 격리" 보안 규칙을 단일 docstring으로 통합 |
| `src/ttakkari/cli.py` | CLI 서브커맨드 핸들러 정의 충돌 | API 쪽의 `_cmd_kb_backfill`, `_cmd_kb_enqueue_jobs`와 fixes 쪽의 `_cmd_kb_backfill_access`를 모두 보존 등록 |
| `src/ttakkari/kb_library.py` | `scope_filter` 및 `register_text` 충돌 | `scope_filter`를 `kb_access.document_access_filter` 단일 규칙으로 통합(v3 `access` 컨텍스트 및 레거시 `scope` 인자 동시 지원; 접근 행 없는 문서는 work_only로 처리되어 personal에 미노출). `ensure_document_access` 및 `enqueue_pipeline_job` 호출 모두 보존 |
| `src/ttakkari/kb_retrieval.py` | `_metadata_filter_predicates` domain 필터 충돌 | URL 호스트 기반 domain(fixes의 `source_domain_expression()`)과 사용자가 명시한 주제 및 엔터티 domain에서 유도된 토픽(API의 `derived`)을 `or_`로 결합하여 양쪽 검색 경로 모두 정상 동작 보존 |
| `src/ttakkari/kb_store.py` | `ingest_source` 내 `source_meta` 충돌 | URL 출처의 `domain_from_locator` 도메인 메타와 호출자가 전달한 `source_meta` 딕셔너리를 병합하여 `extra["source_meta"]`로 저장 |

---

## 3. 리드 지적 사항 분석 및 통합 교정 (TDD)

### (1) `kb_backfill.py`의 `--classify-legacy` 안전 규칙 교정
- **문제**: 기존 `kb_backfill.py`와 API 작업 기록에서 `--classify-legacy`가 접근 행 없는 문서를 무조건 `personal/global`로 생성하여, 과거 미분류 문서가 회사 자료일 경우 전역에 공개되는 노출 확대 위험이 존재했음.
- **교정**:
  - `src/ttakkari/kb_backfill.py`의 `apply_plan`에서 `kb_access_backfill.classify` 안전 규칙을 사용하도록 수정.
  - 출처 근거가 없는 미상(unknown) 문서는 `work`(`work_only`)로 fail-closed 격리.
  - `research:personal:...` 또는 Collector origin_meta에 `scope="personal"` 근거가 확인된 경우만 `personal/global` 허용.
- **TDD 검증**:
  - `tests/test_kb_v3_backfill.py`에 `test_classify_legacy_does_not_expose_unprovenanced_documents` 테스트 추가 및 기존 단언 교정.
  - 교정 전: RED (`AssertionError: ('personal', 'personal', 'global') == ('work', 'work', 'work_only')`).
  - 교정 후: GREEN 통과.

### (2) 새 `kb-backfill-access`와 잡 백필의 순서 및 겹침 확인
- `kb-backfill-access` (`kb_access_backfill.py`): DB에 접근 레코드가 없는 문서에 `KbDocumentAccess`를 사전에 배치 채움.
- 잡 백필 (`kb_backfill.py` `run_enqueue_jobs_backfill`): 잡이 없는 현재 리비전에 `enqueue_pipeline_job`을 생성.
- **적용 순서**: `kb-backfill-access`를 먼저 수행하여 접근 행을 채운 뒤 잡 백필 또는 파이프라인 워커를 구동해야, personal이어야 할 문서의 엔터티/주장이 파이프라인에서 fail-closed(work_only)로 낮춰지지 않고 온전히 global로 보존됨.

### (3) A→B→A 복귀 및 현재성 단일 관측 원장 (`currency.py`) 연동 확인
- `kb_library._latest_revision`이 fixes의 `kb_ontology.currency.current_revision` 단일 구현을 호출하도록 통합됨.
- `kb_backfill.py`의 모든 현재 리비전 조회(`_latest_revision`)가 관측 ledger의 `(event_time.desc(), recorded_at.desc(), id.desc())`를 최우선으로 따름.
- A→B→A 복귀 상태에서 잡 백필(`run_enqueue_jobs_backfill`)을 실행하면 최신 관측 리비전인 A가 올바르게 현재 잡 대상으로 선택됨을 확인.

### (4) 작업 기록 안전성 서술 정정
- `docs/work-history/2026-10-25-kb-v3-api.md`의 라인 40 남은 위험 평가에서 "36건 규모에서는 마이그레이션 소요 시간이 약 0.5초로 statement_timeout 30초를 넘을 위험이 없다"고 단정한 문장을, "운영 문서 36건 표본만으로는 실제 운영 전체 access 행 수와 잠금 시간의 안전을 확정할 수 없어 `미측정`으로 정정한다(실운영 원문/DB는 직접 읽거나 쓰지 않음)"로 수정 반영.

### (5) L1 영어 시드 매칭 버그 교정에 따른 API 테스트 동기화
- fixes의 `extract_l1` 대소문자 매칭 교정으로 인해 `K8S_DOC`의 "서비스는 Pod 집합을 노출한다" 패시지에 대문자 `Pod`가 파이프라인에서 정상 연결되면서, 별칭 relink 시험(`test_alias_relink_connects_existing_passages_with_exact_evidence`)이 미연결 패시지를 찾지 못하던 현상 확인.
- 시험 픽스처 본문을 의도에 맞게 조정하여 별칭 relink 동작 및 1건 연결 단언을 성공시킴.

---

## 4. 검증 결과 증거

모든 시험은 `env -u TTAKKARI_TEST_DB` 환경에서 pytest conftest가 띄운 독립 임시 PostgreSQL 클러스터에서 수행되었으며, 공유 DB나 운영 자원은 일체 사용하지 않았다.

### (1) 핵심 단위 테스트
- `env -u TTAKKARI_TEST_DB uv run pytest -q unit_tests/test_kb_access.py unit_tests/test_kb_backfill_pure.py`
  - 결과: `11 passed in 0.48s`

### (2) 종합 회귀 테스트 스위트
- **실행 명령**:
  ```bash
  env -u TTAKKARI_TEST_DB uv run pytest -q tests/test_kb_v3_* unit_tests/test_kb_access.py tests/test_kb_retrieval.py tests/test_generation_deployment.py tests/test_route_inventory.py tests/test_kb_library_api.py tests/test_kb_observations.py
  ```
- **실행 출력**:
  ```text
  ........................................................................ [ 20%]
  ........................................................................ [ 41%]
  ................................s....................................... [ 61%]
  ........................................................................ [ 82%]
  ...............................................................          [100%]
  db reset: 211 selective (avg 5.9/119 tables), 0 forced full, 0 fell back to full
  350 passed, 1 skipped in 40.26s
  ```

### (3) 주요 개별 스위트 통과 내역
- `tests/test_kb_v3_api_and_mcp.py`: `20 passed in 5.18s`
- `tests/test_kb_v3_backfill.py`: `12 passed in 2.82s` (미상 격리 좁은 RED→GREEN 포함)
- `tests/test_kb_v3_fixes_realdb.py`: `20 passed in 2.42s` (반례 3종 A→B→A/오래된 스냅샷/역전, kept_separate 종결 등)
- `tests/test_kb_v3_fixes_backfill.py`: `4 passed in 1.07s`
- `tests/test_kb_v3_generation_coexistence.py`: `2 passed in 5.50s`
- `tests/test_kb_v3_relation_promotion.py`: `11 passed in 0.88s`
- `tests/test_kb_v3_integration_flow.py`: `15 passed in 2.86s`
- `tests/test_generation_deployment.py`: `49 passed in 3.33s`
- `tests/test_kb_retrieval.py`: `9 passed in 0.92s`
- `tests/test_route_inventory.py`: `7 passed in 0.95s`

---

## 5. 알려진 한계 및 미검증 항목

1. **운영 DB 규모 검증**: 운영 DB의 실제 본문 크기(최대 28,667자) 및 총 access 행 수, 0072 마이그레이션 ACCESS EXCLUSIVE 잠금 시간은 직접 측정하지 못했다(`미측정`).
2. **독립 검증 필요**: 본 통합 커밋 이후 리드가 다른 공급자(provider) 환경에서 독립 2차 검증을 진행할 예정이며, main merge/push/deploy는 보류 상태로 남긴다.
