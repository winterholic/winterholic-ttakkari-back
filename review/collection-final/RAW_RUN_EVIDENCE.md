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

---

## 6. unhold ↔ precise apply 실 2개 DB 세션 동시 경합 및 KB Delivery/Search 실증
- **검증 한계 정정**:
  - `unit_tests/test_collection_mission_triage_unhold.py::test_concurrent_unhold_and_precise_apply_preserves_envelope_hash_and_single_outbox`는 `MagicMock` DB 및 공유 리스트를 이용한 **mock 순차 단위 검사**였으며, 실제 2개 DB 세션의 동시 실행이나 KB imported receipt/search 검증을 포함하지 못했음(증거 초과 정정).
- **실제 2 DB 세션 경합 테스트 추가 및 실증**:
  - **테스트 경로**: [`tests/test_collection_missions.py::test_concurrent_two_db_sessions_unhold_and_precise_apply_race`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/tests/test_collection_missions.py)
  - **검증 시나리오**:
    1. 동일 item에 대해 Session 1(`missions.unhold_item`)과 Session 2(`precise_eval.apply_precise_result` 및 멱등 outbox 생성)를 `asyncio.gather`로 실제 2개 PostgreSQL 트랜잭션 동시 경합 실행.
    2. row lock (`.with_for_update(of=CollectorItem)`)을 통한 직렬화 및 `CollectorOutbox` 중복 없는 단 1건 생성 확인.
    3. 원본 `envelope`, `rights`, `payload_hash`, `raw_sha256`, `text_sha256`, `retention` 불변 보존 확인.
    4. `kb_delivery.deliver_once()` 실행 후 `receipt.outcome == "imported"` 확인 (추가 배달 시 0건으로 중복 배달 없음 확인).
    5. `/api/assistant/kb/search?q=직렬화`를 통한 단일 문서 검색 및 색인 정상 확인.
  - **실행 명령 및 결과**:
    ```bash
    uv run pytest tests/test_collection_missions.py -k test_concurrent_two_db_sessions_unhold_and_precise_apply_race
    ```
    ```text
    tests/test_collection_missions.py .                                      [100%]
    db reset: 1 selective (avg 3.0/111 tables), 0 forced full, 0 fell back to full
    1 passed, 40 deselected in 0.84s
    ```

---

## 7. Renderer borrowed PoliteFetcher 수명 관리 및 순차 페이지 재사용 검증
- **원인 규명**:
  - `render.default_fetch`에서 호출자가 주입한 `polite` 객체에 대해 `closer = polite.fetcher.aclose`를 반환할 경우, 첫 번째 페이지 렌더 완료 후 `closer()`가 공유 fetcher를 닫아버려 동일 job/파이프라인의 후속 페이지 수집 시 연결 실패가 발생할 위험이 확인됨.
- **교정 내역**:
  - 주입받은(borrowed) `polite`에 대해서는 호출자에게 수명 소유권이 있으므로 렌더러 완료 시 닫지 않는 `noop_closer`를 반환하도록 [`src/ttakkari/collector/render.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/src/ttakkari/collector/render.py) 수정.
- **반례 및 재사용 검증**:
  - **테스트 경로**: [`tests/test_collection_render.py::test_borrowed_polite_fetcher_is_not_closed_by_renderer_and_supports_subsequent_pages`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/tests/test_collection_render.py)
  - `render_html` 1회 실행 후 `mock_polite.fetcher.aclose` 미호출 확인 및 동일한 polite로 2번째 페이지 fetch가 정상 수행됨을 검증 완료.
  - **실행 명령 및 결과**:
    ```bash
    uv run pytest tests/test_collection_render.py -k test_borrowed_polite_fetcher_is_not_closed_by_renderer_and_supports_subsequent_pages
    ```
    ```text
    tests/test_collection_render.py .                                        [100%]
    1 passed, 10 deselected in 0.03s
    ```
---

## 8. Redirect 매 Hop 단위 robots.txt 및 Pacing 검사 실증 (77a 피드백 반영)
- **원인 규명**:
  - `PoliteFetcher.fetch`가 최초 URL에 대해서만 robots.txt를 검사하고, `SafeFetcher._fetch`가 redirect를 따라갈 때 호출하는 `before_request(hop)` 콜백에서는 pacing 대기만 수행하여, `/ok` → `/blocked` redirect 시 `/blocked` 대상 실제 GET 요청이 나가는 결함 확인.
- **교정 내역**:
  - [`src/ttakkari/collector/polite.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/src/ttakkari/collector/polite.py)의 `pace(hop)` 콜백 내에서 `policy.robots_required`일 경우 매 hop마다 `_rules(hop, policy)`를 조회하여 `rules.allowed(hop.target)` 및 hop origin의 cooldown, crawl-delay를 검사하도록 수정.
- **검증 결과 (3개 신규 테스트 전건 통과)**:
  - **테스트 경로**: [`tests/test_collector_fetch.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/tests/test_collector_fetch.py)
    1. `test_robots_disallow_on_redirect_hop_blocks_destination_get`: robots disallow 경로로의 redirect 시 목적지 GET 실행 차단 및 `PolicyBlocked("robots_disallowed")` 발생 실증.
    2. `test_allowed_redirect_hop_succeeds_with_robots`: 허용된 redirect 목적지는 정상 200 수집 성공 실증.
    3. `test_redirect_to_different_allowed_origin_enforces_destination_robots`: 다른 허용된 origin으로 redirect 시 목적지 origin의 robots.txt 규정 정당 검사 실증.
  - **실행 명령 및 결과**:
    ```bash
    uv run pytest tests/test_collector_fetch.py -k "test_robots_disallow_on_redirect_hop_blocks_destination_get or test_allowed_redirect_hop_succeeds_with_robots or test_redirect_to_different_allowed_origin_enforces_destination_robots"
    ```
    ```text
    tests/test_collector_fetch.py ...                                        [100%]
    3 passed, 72 deselected in 0.55s
    ```

---

## 9. Policy-only Public Helper의 Owned Policy-aware Client 보장
- **원인 규명**:
  - `render.default_fetch`에서 `polite`가 전달되지 않고 `policy`만 전달된 경우, 기존 코드가 `SafeFetcher`에 `host_check`만 넘겨 robots/pacing이 실행되지 않는 계약 불일치 확인.
- **교정 내역**:
  - `polite=None`이더라도 `policy`가 존재하면 [`src/ttakkari/collector/render.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/src/ttakkari/collector/render.py)가 자체 `owned_fetcher`와 `MemoryHostState` 기반 `PoliteFetcher`를 구성하여 항상 robots/pacing을 수행하도록 보장.
  - 자신이 생성한 owned client는 closer 호출 시 `owned_fetcher.aclose`로 정상 clean up되도록 소유권 분리.
- **검증 결과**:
  - **테스트 경로**: [`tests/test_collection_render.py::test_default_fetch_without_polite_uses_owned_policy_aware_client_and_closes`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-collection-integration/tests/test_collection_render.py)
  - **실행 명령 및 결과**:
    ```bash
    uv run pytest tests/test_collection_render.py -k test_default_fetch_without_polite_uses_owned_policy_aware_client_and_closes
    ```
    ```text
    tests/test_collection_render.py .                                        [100%]
    1 passed, 10 deselected in 0.08s
    ```




