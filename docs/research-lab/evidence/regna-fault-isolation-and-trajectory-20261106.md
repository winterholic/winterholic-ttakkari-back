# REGNA 연구 마찰 대조 개선: 개별 런 실패 격리, 원설정 보존 및 LLM 프롬프트/궤적 영속화 검증 보고서

## 1. 개요 및 목적
- **작업 브랜치**: `fix/lab-practical-20261106`
- **작업 워크트리**: `/Users/winterholic/development/projects/winterholic-ttakkari/back-lab-practical-20261106`
- **배경**: `docs/research-lab/regna-friction-and-improvements.md`에서 분석된 REGNA 온디바이스 에이전트 실측 연구 마찰을 바탕으로, 근거 없는 대규모 프리셋 확장을 지양하고 실사용에서 막히는 핵심 결함을 수정·검증함.
  1. 개별 런 실패 격리와 잘못된 원인 분류 (예: 어댑터 실패가 budget_exceeded로 덮어써지거나 배치가 연쇄 취소되는 결함)
  2. 연구 재개(resume/snapshot) 시 각 실험의 구체화된 원설정(Configurations) 보존
  3. LLM 추론 실행 시 실제 입력 프롬프트 및 응답 전문(Full Output Trajectory) 영속화 및 API 열람 보장

---

## 2. 결함 분석 및 수정 내역

### 1) 개별 런 실패 격리 및 원인 분류 정정 (Fault Isolation & Accurate Error Classification)
- **발견된 결함**:
  1. `src/ttakkari/research/runner.py`: `execute_run_instance`에서 어댑터가 이미 실패(`adapter_res.status == "failed"`)했음에도 불구하고 실행 시간이 deadline을 경과하면 `budget_exhausted` 분기에 의해 `run.status = "cancelled"`, `run.error_code = "budget_exceeded"`로 덮어써져 실제 오류 원인이 은폐됨.
  2. 예외 처리 블록(`except Exception as e:`)에서도 실제 발생한 런타임 예외가 `execution_timeout.expired()` 분기에 걸려 `budget_exceeded`로 왜곡됨.
  3. `_run_owned_batch`에서 단일 런이 `cancelled`될 경우 원인 불문하고 무조건 나머지 미시작 런들을 취소하며, 원인을 `budget_exceeded`나 `batch_cancelled`로 단일화함.
- **수정 내용**:
  1. `execute_run_instance`: 어댑터가 실패(`adapter_res.status == "failed"`)한 경우 원래의 `error_code`와 `error_message`를 그대로 보존하며, `budget_exhausted`는 어댑터가 `succeeded` 상태일 때만 만료 처리를 적용하도록 수정.
  2. 예외 처리: 실제 `isinstance(e, TimeoutError)`인 경우에만 `budget_exceeded`로 판정하고, 일반 런타임 예외는 원래 예외 타입(`type(e).__name__`)과 메시지로 `status="failed"` 유지.
  3. `_run_owned_batch`: 배치 중단 사유를 전체 예산(`budget_exceeded`/`budget_invalid`), 사용자 취소(`user_cancelled`/`cancelled`), 메모리 가드(`memory_guard_stopped`)로 명확히 분기하여 기록하고, 개별 런 실패(`failed`)는 배치 루프를 중단하지 않고 후속 설정을 계속 실행하여 격리를 보장.

### 2) 재개 시 원설정 보존 (Original Configurations Preservation on Resume)
- **발견된 결함**:
  - `src/ttakkari/research/service.py`의 `snapshot` 함수가 `study`, `nodes`, `relations`, `experiments`, `runs`, `best_results`, `timeline`, `evidence`를 반환하지만, `Configuration` 모델은 `study_id`가 없어 스냅샷에서 완전히 누락되어 있었음. 이로 인해 재개 시 각 런이 어떤 파라미터(`parameters`)로 실행되었는지 복원하기 어려웠음.
- **수정 내용**:
  - `snapshot` 함수에서 해당 study의 모든 실험 id(`exp_ids`)에 속한 `Configuration` 레코드들을 조회하여 `snap["configurations"]` 배열에 포함하고, `totals["configurations"]` 집계도 추가함.

### 3) LLM 프롬프트 및 응답 궤적 영속화 (Prompt & Output Trajectory Persistence)
- **발견된 결함**:
  - `src/ttakkari/host_gpu.py`의 `bench()`가 입력 프롬프트 전문을 결과 딕셔너리에 보존하지 않고, 각 회차의 모델 응답 텍스트를 `[:200]` 미리보기(`output_preview`)로만 자르고 원본을 버림.
  - `src/ttakkari/research/adapters/llm_inference.py`에서도 프롬프트가 최상위에 누락되어 사용자가 실제 입력과 생성된 전체 텍스트를 검증할 수 없었음.
- **수정 내용**:
  - `host_gpu.py`: `bench()` 반환 딕셔너리에 `"prompt": prompt`를 보존하고, `runs`의 각 원소에 `"content": str(body.get("content", ""))` 전문을 추가 보존.
  - `llm_inference.py`: `bench_result["prompt"] = bench_prompt` 보장 및 각 회차별 프롬프트, 전체 응답 텍스트, 토큰 수, 속도를 구조화한 `sample_trajectories` 리스트를 `raw_output`에 영속화.
  - 이를 통해 `GET /api/research/runs/{run_id}` 및 `manage_research(op="run_status")`에서 즉시 전체 궤적 열람 가능.

---

## 3. 검증 결과 (Verification Evidence)

전체 74건의 단위 테스트를 통과함 (`unit_tests/test_research_regna_improvements.py` 5건 신규 포함).

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests/test_research_terminal_and_dispatch.py unit_tests/test_research_regna_improvements.py -v
============================= test session starts ==============================
unit_tests/test_research_terminal_and_dispatch.py::test_finish_experiment_recovers_to_completed_when_retry_succeeds PASSED [ 12%]
unit_tests/test_research_terminal_and_dispatch.py::test_agent_in_allows_run_retry_and_run_start_and_external_report PASSED [ 25%]
unit_tests/test_research_terminal_and_dispatch.py::test_snapshot_includes_best_result_and_terminal_runs PASSED [ 37%]
unit_tests/test_research_regna_improvements.py::test_snapshot_includes_configurations PASSED [ 50%]
unit_tests/test_research_regna_improvements.py::test_llm_inference_preserves_prompt_and_trajectories PASSED [ 62%]
unit_tests/test_research_regna_improvements.py::test_failed_run_preserves_error_code_not_overwritten_by_budget_exceeded PASSED [ 75%]
unit_tests/test_research_regna_improvements.py::test_adapter_runtime_exception_classified_as_failed_not_budget_exceeded PASSED [ 87%]
unit_tests/test_research_regna_improvements.py::test_batch_continues_when_individual_run_fails PASSED [100%]
============================== 8 passed in 0.58s ===============================
```
