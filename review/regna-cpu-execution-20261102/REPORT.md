# Regna N3 Qwen2.5 first-six CPU operation

Date: 2026-11-02 (Asia/Seoul)

## Result

Implemented the fixed `regna_q25_first6_cpu` backend operation in an isolated worktree based on the active deployed backend revision `83302612c8edaaa40844a423e7223d2ee346e382`. The implementation commit is `fb692505680c7244500ca34c0a478c85515f22dc` (`feat: add fixed Regna Qwen2.5 CPU operation`); the Luna-review hardening commit is `500dcdae1775e39ba3e8430fbb21f29a2f25a4ba` (`fix: close Regna Qwen2.5 supervision failure paths`); the manifest TOCTOU fix is `c32c1763` (`fix: pin manifest bytes during Regna validation`); the initial F-01 redaction commit is `9572da6f7ef5840531794496484a1641383f1c70`; the symmetric F-01 completion is `f7509bf6dc0b06366ed8d24a7784044db16ec51a` (`fix: redact conflicts involving active Q25 jobs`). The original backend worktree was dirty before work began and was left untouched.

This is source-only work. It has not been pushed, merged, deployed, or used to dispatch a model. No model was run or downloaded. No Regna source, plan, oracle, scorer, or historical run was changed.

## F-01 independent-review follow-up

The independent review reproduced one information disclosure on the newly enabled Q25 work-session path: either a Q25 start can conflict with another session's active job, or a non-Q25 heavy start can conflict with another session's active Q25 job. Both returned 409 but exposed the other job's kind and ID. This is the conflict response contract inherited from the base `83302612` implementation, newly reachable from the fixed Q25 operation, not a redesign finding for all operations. The narrow correction covers a conflict when the requested kind or the active kind is Q25: `jobs.Conflict` carries the active job as internal structured context; the API preserves detailed text only when the active job is owned by the requesting session and otherwise returns `다른 운영 작업이 진행 중입니다.` (including when owner context is absent). Conflicts unrelated to Q25 keep their prior behavior. MCP's host-operation tool forwards the API error body, so non-owner callers receive the same sanitized message.

Regression coverage verifies both directions and owners: a different work-session owner gets the fixed 409 message without the active job kind/ID; the same owner retains the prior detailed conflict. Tests create active job records, then invoke the real API route and real `jobs.prepare_heavy` path; they do not inject or mock `jobs.Conflict`. `_runner_state` is stubbed only to keep the synthetic active record from reconciliation. The source delta is `f7509bf6dc0b06366ed8d24a7784044db16ec51a`; no host process or model was started.

## Capability boundary

- The API accepts only `{"kind":"regna_q25_first6_cpu"}` from a work session. Pydantic rejects extra fields. The owner work session alone can read or cancel that operation. Personal `host_gpu` authorization and implementation were not changed.
- The backend pins plan SHA-256 `b7d9bccd070b6da2b7377ab53f394ab1b6d696a22488063da547daf0cad1deeb`, the frozen source manifest and every file it lists, the R4 runner SHA-256 `dd15cb286eae686d2c41968016bd2234c20dc2e1d06e172886cffe39309a1ebc`, and the exact model/runtime/template paths, sizes, and hashes. It checks the exact six cell IDs/seeds and CPU profile before importing verified runner source. Manifest bytes are read once, hashed against the fixed pin, and parsed from that same buffer before checking its file pins.
- Each start creates a new grant bound to the initiating session, job, plan, source, assets, output and claim paths. A durable exclusive claim prevents replay. No caller-selected command, argv, model, path, prompt, cell, or retry is accepted.
- Existing durable HostOperation start/status/cancel and child-process-group cleanup are reused. The operation takes a fresh `ResourceLeaseManager` lease, preflights available memory and a complete process observation, then calls the frozen R4 dispatcher sequentially on CPU. Limits are 6 threads, 32k context, one child at a time, 120 seconds per cell, 900 seconds overall, 30 seconds cleanup reserve, and 6 GiB maximum server RSS.
- RSS identity/measurement or monitor-thread startup failure stops only the exact Popen-owned model child and makes the run fail before a request is sent. The pinned server child inherits the HostOperation runner's process group so supervisor cleanup covers it. Normal lease release requires per-child `wait4_reaped` and `stopped` process receipts plus an exited RSS monitor. After supervisor loss, the job reconciler may release only after it independently proves the owned process group absent; incomplete raw evidence still leaves the operation failed. Unknown group/cleanup state keeps the lease.
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
- `tests/test_host_operations.py`
- `docs/api-contract.md`
- `docs/adr/0037-execution-capabilities-and-host-gpu.md`

## Verification

Commands ran from `/Users/winterholic/development/projects/winterholic-ttakkari/back-regna-cpu-execution-20261102`. `UV_CACHE_DIR` was directed to `/private/tmp/ttakkari-regna-uv-cache` after the default uv cache was denied in the nested command sandbox; this changed only the package cache location.

Focused capability and dispatch tests, exit 0:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
37 passed in 3.38s
```

Before the fix, the new manifest replacement tests failed as expected: a shrunken manifest was accepted (`DID NOT RAISE ProfileError`), and a manifest replaced between the hash read and verifier read was parsed from the second path read (`frozen source manifest has no file pins`). After the fix, both targeted cases pass within the 37-test focused run.

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
79 passed, 64 deselected in 5.51s
```

An earlier attempt ran the API regression concurrently with the focused tests, which share the test database; that run had 74 passed, 3 failed, and 2 setup errors during concurrent resets. The isolated sequential rerun above passed.

The requested broader host-operation, host-GPU, Research Lab, and resource-lease regression set was run before the second Luna-review hardening commit:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_host_operations.py tests/test_host_gpu.py tests/test_research_lab.py tests/test_research_v2_api_contract.py unit_tests/test_host_resource.py unit_tests/test_host_resource_recovery.py unit_tests/test_host_resource_process_state.py unit_tests/test_host_resource_recovery_hardening.py
exit 1
49 failed, 241 passed in 173.20s
```

Observed failures included `SubprocessError: Exception occurred in preexec_fn` while existing host-operation tests spawned their child in the Codex nested command sandbox, and strict host-GPU/resource tests reporting `process table ... enumeration was incomplete` / process state `unknown` in that same test environment. One inline performance-baseline test also returned `OSError:` with no detail; its cause is unverified. These failures were not retried via another engine or process-inspection path. The broad regression result is therefore partial; the focused Q25 tests, lint, and representative API regression above passed after the final code changes.

## Security and lifecycle cases covered

The new tests cover frozen plan/source/runner/asset pins; manifest shrinking and read-time TOCTOU; exact cells and CPU policy; fixed no-argument entrypoint; source loader argument parsing; malformed/caller-selected input; wrong owner, scope, plan/source hash and path; expiry and duplicate claim; unavailable memory and incomplete process observation; missing/malformed raw and cross-cell raw-reference reuse; raw request/SSE/tokenizer integrity; cleanup boolean without wait proof; synthetic wait4/stopped proof; lease owner/generation mismatch; normal, timeout, and mid-run cancellation with a synthetic child; uncertain cleanup and dead-supervisor lease retention; work-only API start and owner-bound status/cancel.

F-01 regression output:

```text
RED (request=Q25, active=foreign backup_db): UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_host_operations.py::test_regna_conflict_hides_other_work_session_job_details
exit 1 — actual 409 included `backup_db` and the active job UUID.

RED (request=backup_db, active=foreign Q25): UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_host_operations.py::test_non_owner_conflict_hides_active_regna_job_details
exit 1 — actual 409 included `regna_q25_first6_cpu` and the active job UUID.

GREEN exact nodes: UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_host_operations.py::test_regna_conflict_hides_other_work_session_job_details tests/test_host_operations.py::test_regna_conflict_preserves_details_for_same_work_session tests/test_host_operations.py::test_non_owner_conflict_hides_active_regna_job_details tests/test_host_operations.py::test_same_owner_conflict_preserves_active_regna_job_details
4 passed in 0.78s

GREEN filter: UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_host_operations.py -k 'regna or conflict or owner'
6 passed, 71 deselected in 0.89s

Focused Q25: UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
37 passed in 3.38s

Ruff: UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff check src/ttakkari/operations/jobs.py src/ttakkari/api/operations.py tests/test_host_operations.py
All checks passed!
```

Luna high (`gpt-6-luna`, high effort) independently reviewed the backend changes in multiple read-only passes. The reviews identified runner source pinning/TOCTOU concerns, RSS monitor startup failure, cross-cell raw-reference reuse, final-receipt/cell-log mismatch, cleanup proof, detached-child cleanup, recovery after partial raw, a wrong-argument no-child lease release, and finally a manifest read/hash TOCTOU. The implementation re-hashes and compiles the exact runner bytes; reads, hashes, and parses the exact same manifest bytes; terminates the Popen-owned child if monitor setup fails; checks receipt/cell-log equality and exact cell/generation raw paths; requires wait4/stop receipts; fixes no-child release; and keeps the model child in the enclosing owned process group. Synthetic tests verify monitor-start cleanup and manifest shrink/read-time replacement rejection or safe same-buffer parsing. The post-fix Luna high read-only review confirmed the manifest TOCTOU finding is closed by the one-buffer read/hash/parse path; that reviewer did not run tests or runtime dispatch.

## Follow-up verification

The independent F-01 source delta is based on `ea51f28ec1f56d40816f50d19da99c32b3b56b87`. Its API conflict message is passed through by the existing MCP `_call` wrapper; sanitized API error data therefore remains sanitized for `host_operation_start` callers. The prior API-79 and manifest-2 evidence is retained; this follow-up ran only the Q25 focused suite, F-01 host-operation tests, and Ruff, not the full suite. The high-effort Luna read-only reviewer identified the reverse-direction leak; the symmetric fix is committed as `f7509bf6dc0b06366ed8d24a7784044db16ec51a`. Its final acceptance review confirmed both conflict directions are redacted for other/unknown owners, same-owner details remain, and found no residual API/MCP leak. The reviewer did not run tests or host/model dispatch.

An independent reviewer can repeat the source checks from this worktree with:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run ruff check src/ttakkari/research/regna_q25_first6.py src/ttakkari/api/operations.py src/ttakkari/operations/jobs.py src/ttakkari/operations/plans.py src/ttakkari/operations/runner.py src/ttakkari/orchestrator/mcp_server.py tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_regna_q25_execution.py tests/test_research_q25_dispatch.py
UV_CACHE_DIR=/private/tmp/ttakkari-regna-uv-cache uv run pytest -q tests/test_research_v2_api_contract.py tests/test_host_operations.py -k 'test_research_v2_api_contract or arbitrary_input or heavy_kind_outside_allowlist'
```

Once this branch passes independent mainline review and is deployed, an authorized work session can start only the pinned operation via `POST /api/agent/operations` with `{"kind":"regna_q25_first6_cpu"}` (or MCP `host_operation_start(kind="regna_q25_first6_cpu")`), poll status, and cancel through the existing lifecycle. That production operation was not run in this task.
