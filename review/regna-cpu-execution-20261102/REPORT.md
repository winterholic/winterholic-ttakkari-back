# Regna N3 Qwen2.5 first-six CPU operation

Date: 2026-11-02 (Asia/Seoul)

## Result

Implemented the fixed `regna_q25_first6_cpu` backend operation in an isolated worktree based on the active deployed backend revision `83302612c8edaaa40844a423e7223d2ee346e382`. The implementation commit is `fb692505680c7244500ca34c0a478c85515f22dc` (`feat: add fixed Regna Qwen2.5 CPU operation`). The original backend worktree was dirty before work began and was left untouched.

This is source-only work. It has not been pushed, merged, deployed, or used to dispatch a model. No model was run or downloaded. No Regna source, plan, oracle, scorer, or historical run was changed.

## Capability boundary

- The API accepts only `{"kind":"regna_q25_first6_cpu"}` from a work session. Pydantic rejects extra fields. The owner work session alone can read or cancel that operation. Personal `host_gpu` authorization and implementation were not changed.
- The backend pins plan SHA-256 `b7d9bccd070b6da2b7377ab53f394ab1b6d696a22488063da547daf0cad1deeb`, the frozen source manifest and every file it lists, the R4 runner SHA-256 `dd15cb286eae686d2c41968016bd2234c20dc2e1d06e172886cffe39309a1ebc`, and the exact model/runtime/template paths, sizes, and hashes. It checks the exact six cell IDs/seeds and CPU profile before importing verified runner source.
- Each start creates a new grant bound to the initiating session, job, plan, source, assets, output and claim paths. A durable exclusive claim prevents replay. No caller-selected command, argv, model, path, prompt, cell, or retry is accepted.
- Existing durable HostOperation start/status/cancel and child-process-group cleanup are reused. The operation takes a fresh `ResourceLeaseManager` lease, preflights available memory and a complete process observation, then calls the frozen R4 dispatcher sequentially on CPU. Limits are 6 threads, 32k context, one child at a time, 120 seconds per cell, 900 seconds overall, 30 seconds cleanup reserve, and 6 GiB maximum server RSS.
- RSS identity/measurement failure stops only the exact Popen-owned model child and makes the run fail. Normal lease release requires per-child `wait4_reaped` and `stopped` process receipts plus an exited RSS monitor. After supervisor loss, the job reconciler may release only after it independently proves the owned process group absent; incomplete raw evidence still leaves the operation failed. Unknown group/cleanup state keeps the lease.
- Cell raw files are checked for their exact cell/seed/generation path, unique reference, bytes/hash/readback, request body hash, complete SSE bytes/hash, exact integer tokenizer usage, and receipt order. The R4 source schema was read directly in `/Users/winterholic/development/hyperez/regna-ios`; no runtime dispatch was performed.

## Files changed

- `src/ttakkari/research/regna_q25_first6.py`
- `src/ttakkari/api/operations.py`
- `src/ttakkari/operations/jobs.py`
- `src/ttakkari/operations/plans.py`
- `src/ttakkari/operations/runner.py`
- `src/ttakkari/orchestrator/mcp_server.py`
- `tests/test_regna_q25_execution.py`
- `tests/test_research_q25_dispatch.py`
- `docs/api-contract.md`
- `docs/adr/0037-execution-capabilities-and-host-gpu.md`

## Verification

Commands ran from `/Users/winterholic/development/projects/winterholic-ttakkari/back-regna-cpu-execution-20261102`. `UV_CACHE_DIR` was directed to `/private/tmp/ttakkari-regna-uv-cache` after the default uv cache was denied in the nested command sandbox; this changed only the package cache location.

Focused capability and dispatch tests, exit 0:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
.................................                                        [100%]
db reset: 33 selective (avg 1.9/120 tables), 0 forced full, 0 fell back to full
33 passed in 3.44s
```

Ruff on every changed Python file, exit 0:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff check src/ttakkari/research/regna_q25_first6.py src/ttakkari/api/operations.py src/ttakkari/operations/jobs.py src/ttakkari/operations/plans.py src/ttakkari/operations/runner.py src/ttakkari/orchestrator/mcp_server.py tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
All checks passed!
```

Representative API contract and arbitrary-input rejection regressions, exit 0:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_research_v2_api_contract.py tests/test_host_operations.py -k 'test_research_v2_api_contract or arbitrary_input or heavy_kind_outside_allowlist'
........................................................................ [ 91%]
.......                                                                  [100%]
db reset: 79 selective (avg 6.1/120 tables), 0 forced full, 0 fell back to full
79 passed, 64 deselected in 5.02s
```

The requested broader host-operation, host-GPU, Research Lab, and resource-lease regression set was also run once:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_host_operations.py tests/test_host_gpu.py tests/test_research_lab.py tests/test_research_v2_api_contract.py unit_tests/test_host_resource.py unit_tests/test_host_resource_recovery.py unit_tests/test_host_resource_process_state.py unit_tests/test_host_resource_recovery_hardening.py
exit 1
49 failed, 241 passed in 173.20s
```

Observed failures included `SubprocessError: Exception occurred in preexec_fn` while existing host-operation tests spawned their child in the Codex nested command sandbox, and strict host-GPU/resource tests reporting `process table ... enumeration was incomplete` / process state `unknown` in that same test environment. One inline performance-baseline test also returned `OSError:` with no detail; its cause is unverified. These failures were not retried via another engine or process-inspection path. The broad regression result is therefore partial; the focused Q25 tests and representative API regression above passed after the final code changes.

## Security and lifecycle cases covered

The new tests cover frozen plan/source/runner/asset pins; exact cells and CPU policy; fixed no-argument entrypoint; source loader argument parsing; malformed/caller-selected input; wrong owner, scope, plan/source hash and path; expiry and duplicate claim; unavailable memory and incomplete process observation; missing/malformed raw and cross-cell raw-reference reuse; raw request/SSE/tokenizer integrity; cleanup boolean without wait proof; synthetic wait4/stopped proof; lease owner/generation mismatch; normal, timeout, and mid-run cancellation with a synthetic child; uncertain cleanup and dead-supervisor lease retention; work-only API start and owner-bound status/cancel.

Luna high (`gpt-6-luna`, high effort) independently reviewed the backend changes. Its first pass found missing R4 source pinning, fail-open RSS supervision, cross-cell raw-reference reuse, boolean-only cleanup release, and overly strict dead-supervisor recovery. These were corrected and the focused tests were rerun. The follow-up Luna high review was still running when this report was prepared; its result is not represented as completed verification.

## Follow-up verification

An independent reviewer can repeat the source checks from this worktree with:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff check src/ttakkari/research/regna_q25_first6.py src/ttakkari/api/operations.py src/ttakkari/operations/jobs.py src/ttakkari/operations/plans.py src/ttakkari/operations/runner.py src/ttakkari/orchestrator/mcp_server.py tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_research_v2_api_contract.py tests/test_host_operations.py -k 'test_research_v2_api_contract or arbitrary_input or heavy_kind_outside_allowlist'
```

Once this branch passes independent mainline review and is deployed, an authorized work session can start only the pinned operation via `POST /api/agent/operations` with `{"kind":"regna_q25_first6_cpu"}` (or MCP `host_operation_start(kind="regna_q25_first6_cpu")`), poll status, and cancel through the existing lifecycle. That production operation was not run in this task.
