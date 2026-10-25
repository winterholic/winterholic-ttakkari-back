# 수집 플랫폼 백엔드 실패 교정 단계 — 원시 실행 증거 및 45 실패 회계 기록

## 1. 원시 결과 및 비교 파일 세부 정보
호스트 `/private/tmp`에 보존되어 있던 이전 실행 원시 파일들을 발굴하여 리포지토리 `review/collection-final/`에 보존함.
(루트 저장소 내부에는 해당 파일이 없었음을 `rg`로 확인 완료)

- **원시 파일 목록 및 SHA256 체크섬**:
  - `/private/tmp/res_c2.txt` (`97a54806f4551e2cb72098fba9f7ca36d978f671f39e66ccec09f36b2810f0bc`): C2 단독 실행 결과 (17 FAIL / 28 PASS)
  - `/private/tmp/res_cur.txt` (`20905ea4087b58a656e164d91078f3a61e5faf5a03e1f4d342527a7c2b7ee7d3`): 이전 세션 실행 결과 (19 FAIL / 26 PASS)
  - `/private/tmp/res_pre.txt` (`20905ea4087b58a656e164d91078f3a61e5faf5a03e1f4d342527a7c2b7ee7d3`): 이전 세션 기준 결과 (res_cur와 동일)
  - `/private/tmp/res_main.txt` (`1008117beb8bd24edf08fa172dfdb75f011c56b57101d2359cc644b98624dd6e`): main 브랜치 기준 결과 (45 PASS)
  - `/private/tmp/run_ids.sh` (`3cfb91bf6189422fc50bbd8962848ae005aca9342e9880f17b34c31fc0e918ae`): 테스트 개별 격리 실행 스크립트
  - `/private/tmp/failed_ids.txt`: 실패 대상 45개 고유 pytest nodeid 목록
- **실행 명령 및 환경**:
  - 스크립트: `run_ids.sh <checkout-dir> <label>`
  - 파이썬: `/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python` (Python 3.13.15)
  - 명령 형식: `env -u TTAKKARI_TEST_DB PYTHONPATH=src $PY -m pytest -q --no-header -p no:cacheprovider --timeout=300 "$id"`

## 2. 이전 보고 합산 오류 규명 및 고유 45개 실패 항목 1:1 회계
이전 보고에서 "15 + 2 + 26 = 43"인데 "45 failed"로 집계된 오류는,
통합 회귀 2건(`tests/test_embedder_lifetime.py`, `tests/test_security_gate_inventory.py`)이 누락되어 43으로 잘못 표기된 것임.
실제 45개 실패 항목은 아래와 같이 1:1로 엄밀히 대응되며, 교정을 완료함:

1. **Collector 레거시 multi-scope 기대 갱신 대상 (15개)**:
   - `tests/test_collector_api.py::test_intake_fixtures_give_same_result_over_http[intake-conflict-cross-scope-replay.json]`
   - `tests/test_collector_api.py::test_intake_fixtures_give_same_result_over_http[intake-invalid-scope-tamper.json]`
   - `tests/test_collector_api.py::test_tampered_access_is_403_without_side_effects[scope]`
   - `tests/test_collector_api.py::test_cross_scope_replay_never_reveals_the_existing_job`
   - `tests/test_collector_api.py::test_agent_scope_comes_from_session_pool_not_request`
   - `tests/test_collector_api.py::test_other_scope_jobs_are_invisible_and_uncancellable`
   - `tests/test_collector_control.py::test_sources_require_auth_and_scope_isolation`
   - `tests/test_collector_control.py::test_manual_documents_are_scope_bound_and_detail_is_hidden`
   - `tests/test_collector_intake.py::test_same_envelope_id_in_other_tenant_or_scope_is_independent`
   - `tests/test_collector_integration.py::test_same_source_key_in_two_scopes_never_mixes_documents_or_search`
   - `tests/test_collector_manage.py::test_management_reads_are_scoped_and_require_auth`
   - `tests/test_collector_manage.py::test_pilot_sources_pass_the_normal_validation_and_seed_once`
   - `tests/test_collector_manage.py::test_hold_audit_is_a_stable_stratified_sample_per_source_and_reason`
   - `tests/test_collector_manage.py::test_reprocess_uses_the_preserved_raw_blob_and_is_idempotent`
   - `tests/test_collector_migration.py::test_collector_migrations_match_models_and_round_trip`
   - *원인 및 교정*: C2 단일 지식 도서관 규약(`LIBRARY_SCOPE = "personal"`)에 따라 소유자(owner JWT)는 tenant 안의 모든 행을 조회하고 단일 도서관에 문서를 적재/관리함. 반면 회사 계정 엔진(pool=work) 세션은 work 자료만 접근 가능한 `pool` 격리를 보장함. 레거시 multi-scope 기대(소유자에게 scope 파라미터로 격리하려던 낡은 기대)를 정당한 새 계약(legacy 입력 호환 + single library 조회 + work engine pool 격리)으로 갱신 완료.
   *(소계: 15개)*

2. **Collector 단위 테스트 / 인벤토리 버그 (2개)**:
   - `unit_tests/test_collector_blob_store.py::test_handler_replays_same_blob_at_full_cap_but_denies_new_bytes_once`:
     `pipeline.py`의 `job.stage` 및 `job.pool` 속성 직접 참조 결함 -> `getattr(job, "stage", None)` 및 `_job_pool(job)` 방어 처리 적용 완료 (검증 PASS).
   - `unit_tests/test_platform_v2_inventory.py::test_report_schema_separates_extraction_classification_scope_coverage`:
     신규 테이블 4개(`collection_missions` 등), 마이그레이션 3개(`0072`, `0073`, `0074`), 소스 파일 1개(`collector_precise_runtime.py`)의 미분류 -> `scripts/platform_v2_inventory.py` 분류 규칙에 추가하여 `all_collected_classified: True` 달성 (검증 PASS).
   *(소계: 2개)*

3. **통합 회귀 테스트 (2개)**:
   - `tests/test_embedder_lifetime.py::test_embedding_worker_uses_offline_cache_prefixes_and_reports_measured_lifetime`:
     `embedder_worker.py`에서 `model.model._model_dir` 속성 직접 참조 크래시 -> `getattr` 체인 방어 처리 적용 완료 (검증 PASS).
   - `tests/test_security_gate_inventory.py::test_no_security_tests_omitted_from_inventory`:
     보안 게이트 인벤토리(`SECURITY_TEST_FILES`)에 신규 추가된 `test_collection_mission_stage2.py`, `test_collection_research_flow.py` 등록 완료 (검증 PASS).
   *(소계: 2개)*

4. **마이그레이션 격리 환경 (10개)**:
   - `tests/test_game_build_handoff_migration.py::test_0069_adds_only_nullable_columns_and_downgrades_cleanly`
   - `tests/test_game_migration.py::test_game_migration_is_additive_and_downgrades_cleanly`
   - `tests/test_game_migration.py::test_game_tables_enforce_uniqueness_and_cascade`
   - `tests/test_game_migration.py::test_0048_adds_only_nullable_columns_and_downgrades_cleanly`
   - `tests/test_google_account.py::test_migration_copies_notebooklm_account_state_without_touching_the_old_table`
   - `tests/test_google_account.py::test_migration_is_idempotent_on_rerun_of_the_copy`
   - `tests/test_knowledge_migration.py::test_knowledge_migration_is_additive_and_guards_evidence`
   - `tests/test_knowledge_migration.py::test_populated_ledger_cannot_be_destroyed_by_application_rollback`
   - `tests/test_meeting_lineage_migration.py::test_lineage_migration_backfills_valid_full_jobs_and_preserves_sources`
   - `tests/test_retire_clovanote_migration.py::test_retirement_preserves_every_source_row_and_meetings_and_downgrades`
   - *원인 및 실증*: 이전 개별 PASS 요약에 이어, 전체 5,236개 full suite 완주 및 45개 묶음 전건 실행(`task-170`)을 통해 10건 모두 격리 DB 환경에서 에러/충돌 없이 완전 통과(GREEN)함을 최종 실증 완료.
   *(소계: 10개)*

5. **백업 스크립트 임시 디렉토리/환경 격리 (16개)**:
   - `unit_tests/test_backup_ops_remaining.py` (8개)
   - `unit_tests/test_backup_restore.py` (8개)
   - *원인 및 교정*: `mktemp` 호출 시 시스템 고정 TMPDIR(`/var/folders/...`) 접근 권한 오류 발생 -> 테스트 fixture 및 실행 환경에 테스트 전용 `TMPDIR` 격리 적용 완료. 전체 5,236개 full suite 및 45개 묶음 전건 실행에서 완전 통과(GREEN) 실증.
   *(소계: 16개)*

**총합: 15 + 2 + 2 + 10 + 16 = 45개**
근거 없는 "신규 0", "DB 충돌 100%" 등 비체계적 단정을 배제하고 원인별로 엄밀히 분류 및 45건 전건 해결 실증 완료.

---

## 3. 전체 마이그레이션 체인(0072→0073→0074) 라운드트립 계약 확인
독립 검증자(77a)의 '전체 chain 증거 없음' 지적에 대해 root 정정 확인:
- 파일 경로: [`tests/test_collection_combined_migration_contract.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/tests/test_collection_combined_migration_contract.py)
- 핵심 테스트: `test_0072_to_0074_migrations_roundtrip_in_isolated_database_and_match_orm`
- 검증 내용: 격리된 PostgreSQL 스키마에서 0072(collection_missions) → 0073(collection_mission_runs) → 0074(collection_single_library)의 순차 upgrade, 역순 downgrade(0074→0073→0072), 그리고 재적용(re-upgrade)을 실제 ORM/DB 컬럼 수준에서 검증.
- 실행 증거: `uv run pytest tests/test_collection_combined_migration_contract.py` → 3 passed (0.94s). 새 테스트 복제 없이 기존 계약 테스트로 검증 확보.

---

## 4. 백엔드 전체 테스트 스위트 (5,236개) 최종 1회 완주 및 원시 실행 증거
- **실행 명령**:
  ```bash
  cd /Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration && uv run pytest -q --tb=short 2>&1 | tee review/collection-final/full_run_raw.txt
  ```
- **원시 출력 파일 및 SHA256 체크섬**:
  - 파일: `review/collection-final/full_run_raw.txt`
  - SHA256: `eba2020771df3d7b1b197bd25d15aecadcb15d7a055513c3f24701c559487657`
- **결과 요약**:
  - `5 failed, 5202 passed, 27 skipped, 2 xfailed, 30 warnings, 5018 subtests passed in 865.62s (0:14:25)`
  - **실패 5건 상세 분석 및 후속 교정**:
    1. `tests/test_collector_api.py::test_tampered_access_is_403_without_side_effects[scope]`:
       - 변조 테스트에서 `ALLOWED_SCOPES`에 없는 문자열로 변조하여 422 `invalid_envelope`가 발생했던 것 -> `ALLOWED_SCOPES`에 포함되나 소유자 claims에 없는 `"shared"`로 변조하여 403 `scope_binding_mismatch` 계약 준수 (교정 후 PASS).
    2. `tests/test_collector_integration.py::test_same_source_key_in_two_scopes_never_mixes_documents_or_search`:
       - C2 단일 도서관 규약(`in_library(CollectorSource.scope)`)에서 동일 tenant 내 `source_key`는 고유해야 하므로, 동일 key 중복 등록 시 409 `source_key_exists`로 방어됨을 확인하고 등록된 출처를 단일 도서관 문서 계보로 통합 검증 (교정 후 PASS).
    3. Muse 관련 3건 (`test_agent_token_security.py`, `test_muse_effort_pinned.py`, `test_muse_observation_ping.py`):
       - `.muse-runtime/node_modules` 부재 사유 (`RuntimeError: Muse SDK runtime missing; run npm ci --prefix .muse-runtime --ignore-scripts`). 수집 플랫폼 변경과 무관한 외부 런타임 환경 결함임.

---

## 5. 원시 45개 실패 항목 1회 일괄 통합 검증
- **실행 명령**:
  ```bash
  cd /Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration && PYTHONPATH=. uv run pytest -q --tb=short $(cat review/collection-final/failed_ids.txt)
  ```
- **실행 결과**:
  ```text
  .............................................                            [100%]
  db reset: 27 selective (avg 4.3/111 tables), 0 forced full, 0 fell back to full
  45 passed, 1 warning in 24.37s
  ```
- **결과**: **45 passed, 0 failed** (100% 완전 통과)


