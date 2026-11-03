# KB v3 L2 independent verification

- Outcome: **failed** (blocking acceptance of `kb_v3_verify` as a truthful end-to-end verifier)
- Target: `f774928b2c999da28e33e5139841742ed8544f56`
- Base: `c07ea7cd0440363abc6cacb909b3f0cd338ef43b`
- Implementer session: `26fff9a3-7859-4c2e-a79a-60bcc7a569dd` (read-only session record: Antigravity / Gemini Flash; task type `general`)
- This verification ran in a fresh Codex context. It is cross-provider; this interface did not expose a control or receipt for setting `task_type=verification`.
- Scope: source/test inspection plus a real PDF parser run and a bounded in-memory SQLite counterexample. No cloud CLI, operation endpoint, production host operation, PostgreSQL server, push, or deployment was invoked.
- The primary runtime probe was executed from a clean `git archive` of the exact target SHA. While this review was in progress, another process advanced the shared branch to `1e5d7bb264ffc62b16afaaca514d348eeb4179b5`; the verifier did not author that commit. This report's outcome remains specifically about target `f774928b`.

## Verdict

| Priority | Finding | Evidence and required correction |
|---|---|---|
| P1 | **False success is reproducible.** Three `held` jobs, zero L2 calls, assertions, evidence, and conflicts are reported as `succeeded`. | `probe-target-output.txt` reproduces the state against the exact target commit using only SQLite. Success needs explicit assertions for all three job states and the intended L2 call, exact source-backed claims/evidence, and the 30d/90d same-subject + same-predicate contradiction. `held` must never satisfy the successful-verification gate. |
| P1 | **The default worker can claim unrelated global backlog.** | `verify_kb_v3` calls `get_default_worker().pump()` without passing the three revision IDs. `KbPipelineWorker.claim_job()` selects the oldest eligible job from the whole queue. Thus this operation can run a non-fixture job and its provider call; the later gather also waits on every live task in the shared worker's `_tasks`. Restrict claims to this operation's exact job IDs and await only task handles created for those IDs. |
| P1 | **The advertised 30-second finite wait is not enforced around worker completion.** | The deadline is checked only by the outer loop; after `pump()` the code awaits `asyncio.gather(*tasks)` without a timeout. A hung task bypasses the deadline. The inline wrapper has a separate 90-second `wait_for`, which is not equivalent to the stated 30-second operation bound. Put the remaining deadline on the await and define safe handling for still-running leased jobs. |
| P1 | **Cancellation/ownership contract is not met by this inline registration.** | `kb_v3_verify` is added to `plans.INLINE`; the API runs it in the request coroutine. `jobs.cancel()` immediately marks an inline job cancelled, but does not cancel that coroutine. The coroutine can continue registering documents / invoking L2 and later `start_operation()` writes `succeeded`. No operation-specific host lease is acquired in this path. Route through an owned cancellable operation lifecycle or add a cancellation/fencing check before side effects and final status. This finding is source-based; no live operation was run. |
| P2 | **Retry resets downstream stage rows, but derived records are not shown to be reconciled.** | `retry_pipeline_job(from_stage="extracting")` resets extracting and all downstream stages, which reruns canonicalizing. It does not itself remove/reconcile prior assertions, evidence, or conflicts. Existing canonicalization re-derives conflicts from persisted assertions. Test a changed/empty retry result and ensure stale assertions/conflicts cannot remain as current verification evidence. Not executed against PostgreSQL. |

## Confirmed checks

- The bundled PDF is parsed by the real `pdf_pypdf_v1` parser, not a mock: 1,359 bytes, 3 pages, 247 extracted characters, 3 locator spans, status `succeeded`.
- Default config sets `subscription_cli` and `gpt-6-luna`; explicit `disabled` is read by the operation and not overwritten. Registry/API schemas include `kb_v3_verify`, and `StartIn` forbids extra fields. The personal-session API guard is present.
- The implementation report says 27 tests passed, but its own test uses a fake L2 adapter and the previous output is not present as an original log in this review directory. This verification did not rerun pytest: `TTAKKARI_TEST_DB` pointed at port 54000 but had no Run ID/role ownership markers, so DB ownership could not be established. No database was connected and no initdb was attempted.
- The prior implementation report is preserved at `review/kb-v3/l2-activation-20261027/report.md`. `docs/work-history/2026-10-27-kb-v3-l2-activation-and-verify-operation.md` says commit `90d9a6ce2408a6ad6cbba9c312d42a18134520a3`, while the checked-out branch's exact HEAD is `f774928b2c999da28e33e5139841742ed8544f56`. The independent report anchors its claims to the latter; the old report was not rewritten.

## Follow-up observed (outside this verification target)

The shared branch later advanced to `1e5d7bb264ffc62b16afaaca514d348eeb4179b5`. Its source diff adds explicit `held` handling and requires ready jobs plus nonzero passages/assertions/conflicts before `succeeded`, and adds cross-provider recommendation metadata. This partially closes the reproduced held-job success case. The new commit was not independently executed or fully reviewed; the code still does not gate on actual L2 calls, evidence count, or matching 30d/90d subject/predicate/value, and it leaves the global queue, gather deadline, and inline cancellation findings above unchanged in the inspected diff. No claim is made that `1e5d7bb` passes.

## Raw evidence

- `probe-target-f774928b.py` — repeatable parser and SQLite counterexample; never calls a cloud CLI. It was run against a clean archive of the exact target SHA.
- `probe-target-output.txt` — successful raw output from that exact target snapshot.
- `probe.py` and `probe-output.txt` — exploratory worktree artifacts; not authoritative for the commit result because the shared checkout changed during review.
- `probe-blocked-default-home.txt` — first attempt's raw output, which records a sandbox denial when inherited settings tried the protected default blob path. The probe was then run with `TTAKKARI_HOME` redirected to this review directory; the protected path was not retried.
- No production DB, public operation, host worker, cloud CLI, or live cancellation was exercised. Pool/lease/cancellation and stale-data conclusions above are source-based where stated.

## Verification outcome record

`verification_outcome=failed` for target `f774928b2c999da28e33e5139841742ed8544f56`. The original worker-recording `message_worker` tool is not exposed in this run, so this outcome is recorded here with its raw evidence rather than represented as an update to the implementer session.

The shared branch's follow-up commit and edits to the existing implementation report/work-history were not authored by this verification. The report and probe files in this directory are the independent evidence for `f774928b`; no push or deployment occurred.
