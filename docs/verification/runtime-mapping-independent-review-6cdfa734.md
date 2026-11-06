# Runtime lease mapping independent review

Date: 2026-11-07 KST

## Verdict

**결함 1건 (표시 정확성), 그 외 후보 mapping 경로 PASS.** Commit `6cdfa7341cc00b48a37c310638a5faab90ea84bc` is the reviewed HEAD; parent is `293bb8440fb96aaf648a59c7c821a99348ce325c`. No candidate source files were changed.

The query maps a lease to a process group only when `run_id`, `generation_id`, and lease token match. Lease account fields come from the lease's stored `account_id`, and group-only rows carry null account fields; the implementation does not infer the current session account for a stale/mismatched group. Terminal and archived Run/session rows are not filtered out. Missing Run is represented as `run_status="missing"`.

**Finding:** when the outer-joined `sessions` row is absent, `sessions.archived_at IS NOT NULL` evaluates false, so `session_archived` is serialized as `false`. That conflates “session exists and is not archived” with “session row is missing.” This is a narrow observability defect; `run_status="missing"` still distinguishes a missing Run. No source fix was made, per scope.

## 1. Official current-Run account gate

Exact API command required by the task:

```text
/Users/winterholic/bin/ttk get /api/sessions/ee9c3ee9-2604-4087-94a6-bf95da5d42de/runs
```

The raw response was a Run list. To avoid preserving unrelated prompts/results, the list was projected to identity/status fields:

```text
argv: /Users/winterholic/bin/ttk get /api/sessions/ee9c3ee9-2604-4087-94a6-bf95da5d42de/runs | jq '[.[] | {id,status,profile,started_at}]'
output excerpt:
{
  "id": "81ea5858-96a2-4a05-af9e-705fd474c0a4",
  "status": "running",
  "profile": "회사 코덱스 ai2 (ai2@hyperez.io)",
  "started_at": "2026-11-06T17:59:45.102959Z"
}
```

Gate PASS. `pool=personal` is routing scope and was not treated as the account identity.

## 2. SQL join and serialization review

Reviewed `src/ttakkari/operations/inline.py` at the candidate revision and model constraints in `src/ttakkari/models.py`.

- Lease rows start from `RuntimeLease` and outer join Run, group, session, and EngineAccount, so terminal Runs and archived sessions are retained and missing Run/session joins do not remove the lease row.
- A lease/group match requires the same Run ID, generation ID, and token. An unmatched lease is emitted as `lease_only`; a process group without that exact lease tuple is emitted separately as `orphan_process_group`.
- Account ID/name on a lease row come from `RuntimeLease.account_id` and its referenced EngineAccount. Orphan group rows explicitly have null account fields. Thus a mismatched group cannot inherit the lease's or current Run's account.
- Terminal status is preserved; a null Run status serializes as `missing`. The missing-session archive flag issue is recorded in the verdict above.
- The payload allowlist contains entry kind, process-group ID, generation, attempt, Run/session IDs, lease account ID/name, Run status, archived flag, and exact-match boolean. It omits PID, PGID, process start time, lease token, account config, prompt, and Run result.

## 3. Independent query-semantic fixture

This used the candidate helper against an isolated in-memory SQLite schema and synthetic rows, not a mocked `db.execute` result and not an operating database. Fixture setup inserts are not part of the helper's observed query statements.

The reproducible probe is preserved at `/private/tmp/account-routing-lease-20261107/runtime-mapping-probe.py`. Exact argv:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen python /private/tmp/account-routing-lease-20261107/runtime-mapping-probe.py
```

Observed output:

```text
EXACT terminal+archived mapping: PASS
TOKEN/GEN mismatch: lease_only has recorded lease account; orphan group account=null: PASS
MISSING Run/session: status=missing, archived=False though session row absent (ambiguous)
fixture leases=103; returned=100; truncated=True: PASS
mapping statements=2 SELECT; bound LIMIT/OFFSET=(101,0): PASS
serialized key set is allowlisted; process identity/token/config/prompt/result absent: PASS
```

This fixture executes the candidate helper's generated SQL against SQLite with synthetic, properly typed UUID rows; it is independent of the mock-only regression's fabricated query results. Early inline fixture drafts had setup-only table/type/transaction errors and were replaced by the reproducible script above; those failures were not candidate failures.

The helper issued two SELECT statements, each fetched at most 101 rows to detect truncation, then returned at most 100 combined mappings. The synthetic fixture confirmed bounded output and the allowlisted serialized shape. It did not prove that a live DB transaction is configured read-only; source review and captured helper SQL show SELECT-only behavior, with no commit/flush/write call in the helper.

## 4. Focused regressions, lint, and diff

Exact focused argv and outputs:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_collection_runtime_report.py
...... [100%]
6 passed in 0.37s

UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_recovery_claim_epoch_fence.py -k 'reconcile_checks_claim_fence_under_control_lock_before_lease_read or live_owner_is_preserved_before_database_or_process_access'
.. [100%]
2 passed, 17 deselected in 0.63s

UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_executor_reconcile.py -k 'fails_closed_on_missing_or_unknown_evidence or reconcile_stale_snapshot_never_signals_process_group or normal_release_uses_control_and_lease_fence'
.......... [100%]
10 passed, 9 deselected in 0.25s

UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen ruff check src/ttakkari/operations/inline.py unit_tests/test_collection_runtime_report.py
All checks passed!

git diff --check 293bb8440fb96aaf648a59c7c821a99348ce325c 6cdfa7341cc00b48a37c310638a5faab90ea84bc
exit 0; no output
```

The runtime-report test uses mocked database results; SQL semantics and row mapping are supported separately by the SQLite fixture above. Claim and identity suites passed their focused selectors. No already-completed account/L2/precise-evaluation regression was repeated.

## 5. Observability and operational boundary

The query has no Run status or archived-session filter and uses outer joins, so terminal/archived/missing associations are visible when their lease rows are present and within the bounded result window. It is global rather than generation-filtered and orders newest lease/group rows first. More than 100 rows can hide older entries; `lease_processes_truncated=true` signals this, but does not identify which generation was truncated. Therefore this source-level capability does not prove the target generation is among a live report's first 100 rows.

No runtime report was invoked against the deployed service, no operating DB was accessed, and no cleanup/reconcile/process action, toggle/scope change, push, deploy, or service restart was run. The report only establishes code behavior on synthetic data and focused local tests. The previously recorded owner smoke OSError and GPU process-enumeration failure were not retried or generalized.
