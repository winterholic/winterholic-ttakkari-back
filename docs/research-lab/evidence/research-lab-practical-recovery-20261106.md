# Research Lab Practical Execution & Dispatch Recovery Evidence (2026-11-06)

## 1. Revision State & Environment
- Target worktree: `/Users/winterholic/development/projects/winterholic-ttakkari/back-lab-practical-20261106`
- Branch: `fix/lab-practical-20261106`
- Base revision: `521f469aeb27a28a897bcac55fa969b1a5c2323e` (origin/main)
- Deployed revision: `c396370f` (ops_list_deploys confirmed succeeded)
- Dirty local main HEAD `4ea068af` preserved untouched.

## 2. Identified & Reproduced Defects

### Defect 1 (P0): `_finish_experiment_if_terminal` retry terminal recovery defect
- **Symptom**: When a trial failed and a subsequent `retry_run` succeeded, the experiment remained in a `"failed"` state forever because the superseded failed Run's status was still included in `statuses`.
- **Pre-fix verification (RED)**:
  ```text
  UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests/test_research_terminal_and_dispatch.py -k "test_finish_experiment" -v
  FAILED: AssertionError: Expected experiment to be completed, but got failed
  ```
- **Fix**: Exclude retried Run IDs (`retry_of_run_id` in `environment_snapshot`) from active terminal checks.
- **Post-fix verification (GREEN)**:
  ```text
  unit_tests/test_research_terminal_and_dispatch.py::test_finish_experiment_recovers_to_completed_when_retry_succeeds PASSED [100%]
  ```

### Defect 2 (P1): Unwired agent operations in `manage_research` & `/api/agent/research`
- **Symptom**: Agents/orchestrator using `manage_research` could not start experiment runs (`run_start`), retry failed runs (`run_retry`), or record external reports (`external_report`), receiving 422 ValidationError. Legacy `execute` only supported `retrieval_eval`.
- **Pre-fix verification (RED)**:
  ```text
  pydantic_core._pydantic_core.ValidationError: 1 validation error for AgentIn
  operation: Input should be 'list', 'create', ... [type=literal_error, input_value='run_retry']
  ```
- **Fix**: Added `"run_retry"`, `"run_start"`, `"run"`, and `"external_report"` to `AgentIn` literal and dispatched them to `retry_run`, `run_experiment`, and `record_external_report`. Added operations to `manage_research` tool declaration.
- **Post-fix verification (GREEN)**:
  ```text
  unit_tests/test_research_terminal_and_dispatch.py::test_agent_in_allows_run_retry_and_run_start_and_external_report PASSED [100%]
  ```

### Defect 3 (P1): Missing `runs` and `best_result` in `snapshot` (resume)
- **Symptom**: `service.snapshot` only returned nodes, relations, experiments, events, and evidence. Agents resuming a study had no access to run trial execution receipts, error messages, or adopted best result details.
- **Pre-fix verification (RED)**:
  ```text
  AssertionError: snapshot missing best_result
  ```
- **Fix**: Included `runs`, `best_results`, and `best_result` in snapshot; pagination totals and `has_more` now include the run/result collections.
- **Post-fix verification (GREEN)**:
  ```text
  unit_tests/test_research_terminal_and_dispatch.py::test_snapshot_includes_best_result_and_terminal_runs PASSED [100%]
  ```

### Defect 4 (P1): Incomplete report export metrics & best result linkage
- **Fix**: Enriched `export_study_report` to include `## 채택된 최적 결과물` section with objective/metrics/rationale and per-run primary metric measurements table.

## 3. Test Suite Receipt
```text
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest unit_tests/test_research_terminal_and_dispatch.py unit_tests/test_research_rules.py unit_tests/test_research_benchmark.py unit_tests/test_research_owner_receipt.py -v
============================== 69 passed in 1.10s ==============================
```

## 4. Lint Receipt
```text
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run ruff check src/ttakkari/research/runner.py src/ttakkari/research/service.py src/ttakkari/api/research.py src/ttakkari/orchestrator/mcp_server.py unit_tests/test_research_terminal_and_dispatch.py
All checks passed!
```

## 5. Unverified / Environmental Boundaries
- Original verification attempt could not connect to its PostgreSQL endpoint inside that sandbox (`PermissionError: [Errno 1] Operation not permitted` on TCP 127.0.0.1 / socket connection). Follow-up verification on 2026-11-07 reused the repository's disposable PostgreSQL fixture and passed concurrent replay/lifecycle tests; see the follow-up receipt below.
- Live API `~/bin/ttk get /api/research/studies`: unverified (`로그인 실패 400: Direct IP access is not allowed`).
- Main push and backend deployment were omitted per instructions.

## Request Replay and Resume Contract (recovery follow-up)

- API `POST /api/research/experiments/{id}/run` and MCP `manage_research(operation="run_start"|"run")` accept `data.idempotency_key` (8–120 characters). Reusing a key with the same configuration selection returns the persisted Run set, including after it becomes terminal. Reusing it with changed arguments returns HTTP 409. A deliberate new batch uses a fresh key; explicit `run_retry(run_id)` always creates one new Run preserving configuration, repeat index, and seed.
- Existing frontend callers that omit a key remain request-compatible but do not receive network replay deduplication. The UI must create one key per user-initiated batch and retain it through transport retries; a new user action creates a new key.
- Resume pagination applies `offset/limit` independently to nodes, relations, experiments, timeline, runs, and best results. `pagination.totals` includes every collection. Configurations are capped at `limit` for experiments on the current experiment page; the configuration total and `has_more_by_collection.configurations` use that same page scope. If the experiment page is empty, the configuration total is zero.
- LLM trajectory `response_completeness` distinguishes full `content` from `preview_fallback`; the raw bench response, original run entries, prompt, and preview remain preserved.

## Follow-up safety, pagination, adoption, and verification (2026-11-07)

- Implementation/regression commit: `82a81dd6d91c5b59ca949d4e086614cccb9dcee7` (`fix(research): enforce safe lab replay and adoption contracts`).
- Safe cancellation: every `cancelled` Run stops remaining batch work, including missing/unknown cancellation codes and memory-guard cancellation. An adapter-local `TimeoutError` remains `failed/TimeoutError`; only expiration of the experiment's `asyncio.timeout_at` deadline maps to `budget_exceeded`. Adapter failures are retained in `research_adapter_receipt`; deadline state is retained separately in `research_budget_receipt`.
- Adoption: the selected Run must be `succeeded`, match the experiment/configuration, and contain a finite measurement for the exact adopted metric. Tests cover missing metric, infinite metric, configuration mismatch, and successful external-report measurement.
- Replay: PostgreSQL lifecycle regression covers sequential and concurrent same-key replay, changed arguments (409), fresh-key new Run, and configuration/repetition/seed preservation. Explicit retry has a separate seed/repetition preservation regression.
- Agent dispatch: `run`, `run_start`, `run_retry`, and `external_report` names are checked between `AgentIn` and the MCP declaration. PostgreSQL API coverage exercises `run`/`run_start`, `external_report`, `run_retry`, cross-scope rejection, and archived-session rejection.
- Resume: 501-run and 501-best-result synthetic boundary checks cover `offset=0/500`, totals, and `has_more`; 501 configurations cover the bounded current-experiment-page scope. PostgreSQL API test checks configuration totals when moving to the next experiment page.
- The full selected regression command ran against the repository's disposable PostgreSQL fixture (not the operating database):
  ```text
  UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest -q tests/test_research_lab.py tests/test_research_runner_lifecycle.py tests/test_research_v2_core.py unit_tests/test_research_regna_improvements.py unit_tests/test_research_terminal_and_dispatch.py
  ........................................................................ [ 84%]
  .............                                                            [100%]
  db reset: 73 selective (avg 5.6/132 tables), 0 forced full, 0 fell back to full
  85 passed in 5.15s
  ```
- Changed-source/test ruff command:
  ```text
  UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run ruff check src/ttakkari/research/runner.py src/ttakkari/research/service.py src/ttakkari/api/research.py src/ttakkari/orchestrator/mcp_server.py src/ttakkari/research/adapters/llm_inference.py tests/test_research_lab.py tests/test_research_runner_lifecycle.py tests/test_research_v2_core.py unit_tests/test_research_regna_improvements.py unit_tests/test_research_terminal_and_dispatch.py
  All checks passed!
  ```
- Test-first boundary evidence for configuration totals:
  ```text
  UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest -q unit_tests/test_research_regna_improvements.py -k snapshot_paginates_runs
  FAILED: expected configurations total 0 for an empty experiment page; observed 1
  ```
  After scoping the count to the experiment page, that focused regression passed (`1 passed, 7 deselected in 0.36s`).
- An earlier E2E run reported 67 passed / 3 failed because two assertions did not include the newly preserved budget receipt/cancellation cause and the external-benchmark fixture omitted the required `source_url`. Those fixtures/assertions were corrected; the prior 85-test command passed before the additional safety assertions below. The latest 86-test run supersedes it.
- Real model/GPU, operating DB, live API, push, and deployment were not run. The disposable PostgreSQL integration fixture did run successfully. No protected runtime/account files were read. Current session profile remains unverified; no account identity was inferred from the previous account handoff.

## Unsafe baseline assertion correction (2026-11-07)

The two “passed” cases in the frozen `ca1fa234-counterexamples.py` are unsafe baseline observations, not desired behavior. Its memory-guard case expects `failed` despite a triggered guard, and its unknown-cancellation case expects the sibling Run to execute. `independent-1983339-20261107/README.md` now labels them explicitly as unsafe observations. The frozen source stays intact as evidence of the baseline behavior.

Current candidate assertions require guard cancellation with the adapter's original failure retained in `research_adapter_receipt`, and require no later Run after unknown or memory-guard cancellation. They also distinguish adapter-local TimeoutError from real experiment deadline expiration and require `response_completeness`/`response_source` on full/fallback trajectories.

Latest selected regression rerun after the safety assertions:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest -q tests/test_research_lab.py tests/test_research_runner_lifecycle.py tests/test_research_v2_core.py unit_tests/test_research_regna_improvements.py unit_tests/test_research_terminal_and_dispatch.py
........................................................................ [ 83%]
..............                                                           [100%]
db reset: 73 selective (avg 5.6/132 tables), 0 forced full, 0 fell back to full
86 passed in 6.10s
```

Focused safety/prompt checks after correcting the prompt assertion:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest -q unit_tests/test_research_regna_improvements.py -k 'memory_guard_stops_failed_adapter or adapter_timeout_error or llm_preview_fallback or llm_inference_preserves_prompt'
4 passed, 5 deselected in 0.46s
UV_CACHE_DIR=/private/tmp/ttakkari-lab-uv-cache uv run pytest -q tests/test_research_runner_lifecycle.py -k 'any_cancellation_stops or batch_time_budget or budget_marks_blocking_adapter'
4 passed, 44 deselected in 1.22s
```

After changing the synthetic guard fixture to initialize its sample list per instance, changed-file Ruff again reported `All checks passed!`. The exact baseline counterexamples were not used as a passing acceptance suite; the updated candidate assertions were run independently as shown above.
