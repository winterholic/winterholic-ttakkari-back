# KB v3 Verify Operation Lifecycle & Success Gate Fixes Report

- **Date**: 2026-10-27
- **Base Commit**: `a873a2a650e4ae3e627346056a82a67826e41e97`
- **Final Commit**: `658d2db2ea7953b45ec6cbe000bbc97741fdac7f`
- **Worktree**: `/Users/winterholic/development/projects/winterholic-ttakkari/back-kb-v3-operation-fixes-20261027`
- **Branch**: `fix/kb-v3-operation-fixes-20261027`
- **Session**: Antigravity / Gemini 3.8 Flash (High) unattended

---

## 1. 배경 및 독립 실패 분석

독립 검증(`review/kb-v3/l2-independent-20261027/report.md`)에서 발견된 핵심 문제:
1. **False success**: `held` 3건, 0 L2 호출, 0 assertion/evidence/conflict 상태임에도 `succeeded` 반환 (`f774928b`).
2. **글로벌 백로그 침범**: `verify_kb_v3`가 전역 `worker.pump()` 및 `_tasks` gather를 수행하여 큐의 무관한 사용자 일감을 선점하고 다른 태스크까지 블로킹 대기함.
3. **INLINE 라이프사이클 계약 위반**: 무거운 작업(문서 수집, L2 모델 대기)임에도 읽기 전용 즉시 작업 계약인 `INLINE`에 등록되어, 취소 시에도 요청 코루틴이 계속 실행되어 `succeeded`로 덮어써버리고 동시성 제어(단일 heavy, dedup)가 불가능했음.
4. **1e5d의 제품 코드 오염**: Flash 작업자 모델 정책(`verification_required`, `recommended_verifier: Claude 4.6`)이 제품 응답 딕셔너리에 하드코딩되어 제품 코드를 오염시킴.

---

## 2. 교정 내역 (계약별 최소 교정)

### 1) Success Gate 엄격화
- **정확히 3개 문서 / 3개 ready 잡** 확인: `actual_doc_count == 3` and `len(final_jobs) == 3` and `all(j.status == 'ready' for j in final_jobs)`.
- **실제 L2 extraction 호출 성공 증거**: 각 잡의 `extracting` 단계에서 `calls_made > 0`, `adapter != 'disabled'`, `available is True`, `status == 'succeeded'` 확인.
- **Source-backed exact quoted claim/evidence**: `KbAssertion`에 결속된 `KbEvidence`의 `span_start:span_end` 구간이 `KbPassage.body`에서 비어있지 않은 유효 인용구인지 검증.
- **30일 vs 90일 정책 간 동일 주체/동일 predicate 상충 검증**: 두 정책 문서 리비전에서 파생된 assertion 쌍이 동일한 `subject_entity_id`, 동일한 `predicate`, 상이한 `norm_object_value`를 가지며, 이를 결속하는 `KbConflict`(status in `('open', 'disputed')`)가 실재해야만 승인.
- **정직한 상태 보고**: failed/held/degraded/partial/disabled 상태를 숨기지 않고 실제 사유와 함께 반환하며, `counts.documents`도 실제 DB 쿼리 결과로 보고.
- **민감정보 보호**: 원문, prompt, DB URL, password는 반환하지 않음.
- **1e5 하드코딩 완전 제거**: `verification_required`, `recommended_verifier` 제품 응답 제거.

### 2) 글로벌 worker.pump 및 공유 _tasks gather 제거
- 문서는 제품 ingest 및 `enqueue_pipeline_job` 후 트랜잭션을 commit하여 일반 제품 큐 소유로 명시.
- 런타임에서는 실시간 자동 워커가 큐를 처리하므로 오퍼레이션은 정확히 3개 잡의 상태만 관측(observation)하는 구조로 전환.
- 테스트 등 명시적 worker 전달 시에도 오직 대상 3개 잡 ID만 안전하게 claim하고 단일 task handle만 finite await.
- 매 반복마다 남은 상한(`deadline - time.monotonic()`)을 계산하여 적용, 상한 도달 시 `partial`과 실제 잡 상태 반환.

### 3) Host Operation 라이프사이클 계약 복원
- `plans.INLINE`에서 `kb_v3_verify` 제거.
- `plans.HEAVY`에 `kb_v3_verify` 등록 및 3분(`TIMEOUT_SECONDS['kb_v3_verify'] = 180`) 상한 설정.
- `plans.make_plan`: `python -m ttakkari.operations.kb_v3_verify <job_id>`로 러너 분리 프로세스 계획 수립.
- `runner._summarize`: stdout에 기록된 요약 JSON을 파싱하고, `status != 'succeeded'`인 경우 러너 실패 처리 및 세부 사유 보존.
- 동시 실행 시 동일 kind는 `deduplicated=True`로 기존 잡 재사용, 다른 heavy 작업 실행 중이면 `Conflict` 발생.
- 러너 취소(`jobs.cancel`) 시 프로세스 그룹 정리 및 `cancelled` 상태 보존으로 최종 성공 덮어쓰기 방지.

### 4) 재실행 멱등성 및 증거 범위 격리
- 멱등 3 고정 key(`KEY_PDF`, `KEY_POLICY_30D`, `KEY_POLICY_90D`) 유지.
- 이전 실행에서 L2 disabled로 degraded였던 잡만 현재 L2 활성화 시 `retry_pipeline_job(..., from_stage='extracting')`으로 정상 재처리.
- 과거/stale assertion 및 무관한 엔터티의 conflict를 집계에서 철저히 배제(현재 3개 revision_id 범위 한정).

---

## 3. TDD 검증 결과

### 1) 음성 RED 단계
- **명령**: `uv run pytest tests/test_kb_v3_verify_operation.py`
- **결과**: `6 failed, 5 passed in 1.26s` (Exit Code: 1)
- **실패 항목**:
  - `test_operation_registration_and_strict_schema`: plans.HEAVY 등록 부재
  - `test_verify_gate_rejects_held_and_zero_l2`: 0-L2/held 시 succeeded 판정 취약점 노출
  - `test_verify_gate_rejects_unrelated_or_stale_conflict`: 무관한 conflict만 있을 때 차단 검증
  - `test_verify_does_not_claim_unrelated_backlog`: 큐 백로그 비침범 검증
  - `test_verify_gate_rejects_missing_exact_quote`: exact quote 누락 검증
  - `test_actual_db_counts_reported`: 실제 DB count 검증
- **증거 파일**: `review/kb-v3/operation-fixes-20261027/red-output.txt`

### 2) 최소 교정 GREEN 단계
- **명령**: `uv run pytest tests/test_kb_v3_verify_operation.py -v`
- **결과**: `14 passed in 1.77s` (Exit Code: 0)
- **통과 항목 (14건)**:
  - `test_operation_registration_and_strict_schema`: PASSED
  - `test_mcp_server_tool_includes_kb_v3_verify`: PASSED
  - `test_schema_missing_reports_unsupported`: PASSED
  - `test_verify_kb_v3_pipeline_and_conflict_detection`: PASSED
  - `test_verify_kb_v3_honestly_reports_degraded_when_l2_disabled`: PASSED
  - `test_verify_kb_v3_idempotent_reexecution`: PASSED
  - `test_verify_gate_rejects_held_and_zero_l2`: PASSED
  - `test_verify_gate_rejects_unrelated_or_stale_conflict`: PASSED
  - `test_verify_does_not_claim_unrelated_backlog`: PASSED
  - `test_verify_gate_rejects_missing_exact_quote`: PASSED
  - `test_actual_db_counts_reported`: PASSED
  - `test_verify_gate_full_success_when_all_conditions_met`: PASSED
  - `test_heavy_lifecycle_dedup_and_conflict_guard`: PASSED
  - `test_runner_summarize_kb_v3_verify`: PASSED
- **증거 파일**: `review/kb-v3/operation-fixes-20261027/green-output.txt`

### 3) 회귀 검증
- `uv run pytest tests/test_kb_v3_default_activation.py` (6 passed in 0.03s)

---

## 4. 범위 및 미실행 항목

- **실행 범위**: 격리 SQLite 메모리 DB, 모의 L2 어댑터, 단위 프로세스 수명주기 및 러너 요약 파싱.
- **미실행 항목 (`미실행: 사유`)**:
  - `미실행: PostgreSQL 서버 및 prod 호스트 오퍼레이션 실기동`: 샌드박스 보안 규칙 및 사용자 지침(prod 오퍼레이션 실행 금지, ownRun DB 미지정 시 initdb/공유DB 접근 금지) 준수.
  - `미실행: 실제 클라우드 LLM 유료 API 호출`: gpt-6-luna subscription_cli 원칙 준수 및 외부 유료 호출 금지.
  - `미실행: git push 및 backend deploy`: 사용자 지침에 따라 root 저장소가 통합 및 배포를 계속 소유.
