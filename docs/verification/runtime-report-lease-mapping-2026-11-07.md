# Runtime report lease mapping verification

Date: 2026-11-07 KST

## Run profile gate

Command:

```text
/Users/winterholic/bin/ttk get /api/sessions/808ab849-afef-4f69-9d10-d098537b91ed/runs
```

Response excerpt for this Run:

```text
id=177a66f5-24ff-43a7-8846-88cd9795601e status=running
profile=회사 코덱스 ai2 (ai2@hyperez.io) model=gpt-6-luna
```

## Deployed revision and source baseline

The approved `runtime_report` owner operation reported active release revision
`529022861120e86f80400ffaec53dd3f3f7f81c6`. The source worktree was created from
`origin/main` at `40e88581cc1010287ef2b82e13188e2d6c6e8df9`. A later remote check found
`origin/main` had advanced to `293bb8440fb96aaf648a59c7c821a99348ce325c`; its intervening file list did not
include `src/ttakkari/operations/inline.py` or `unit_tests/test_collection_runtime_report.py`.
The deployed-529 and source versions of `inline.py` were identical before this change.

Relevant deployed-source filter in `runtime_report`:

```text
ChatSession.archived_at IS NULL
Run.status IN (running, queued, interrupted)
ORDER BY running first, then created_at descending
LIMIT 40
```

The report has a separate generation-level lease and process-group count, but its `recovery_runs` query
does not return terminal Runs, archived sessions, or group records without a matching eligible Run.
The approved runtime report returned revision 529, the target generation `1a41586a-3377-4977-9bca-bd7cb4870b89`
at revision `19d99b3f4d845af8c213a20ccea8df007450eac9` as draining with one lease and one process group,
and zero matching rows in the 19 returned `recovery_runs`. This establishes an observation gap, not the
target Run's terminal state or cause.

## Change and cleanup boundary

`runtime_report` now returns up to 100 mappings for lease-only records, exact lease/process-group matches,
and process groups without an exact lease match. Each row maps generation, process group/attempt when
present, Run, session, account ID/name, Run status, archived state, and exact lease-match state. The next
row sets `lease_processes_truncated=true`. The mapping query does not select PID, PGID, process start time,
lease token, account config, Run prompt, or Run result. Existing generation diagnostics retain their
pre-existing PID fields; this change adds none to the new mapping fields.

No cleanup was called or added. Existing `reconcile_worker_lease` is restricted to an owned worker and an
expected `running` Run, and rechecks the active claim generation/revision/epoch plus recorded lease and
process identity fences. It cannot safely reconcile archived/terminal Runs, a missing Run, or an unowned
orphan process group. A separate owner-only reconciliation operation would need to reload current claim
and lease state under lock, require terminal/non-archived ownership criteria explicitly, compare stored
generation/token/attempt and PID-start-time/process-group identity, and fail closed on unknown identity;
it must never signal a process solely from a stale report. Until that narrowly scoped operation exists,
these records remain diagnostic-only.

## Verification receipts

Test-first reproduction:

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_collection_runtime_report.py -k maps_bounded
output: 1 failed, 5 deselected; KeyError: 'lease_processes_limit'
```

Focused runtime-report regression:

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_collection_runtime_report.py
output: ...... [100%]
6 passed in 0.42s
```

Claim epoch and live-owner guard regression:

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_recovery_claim_epoch_fence.py -k 'reconcile_checks_claim_fence_under_control_lock_before_lease_read or live_owner_is_preserved_before_database_or_process_access'
output: .. [100%]
2 passed, 17 deselected in 0.56s
```

Identity and unknown-evidence reconciliation regression:

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_executor_reconcile.py -k 'fails_closed_on_missing_or_unknown_evidence or reconcile_stale_snapshot_never_signals_process_group or normal_release_uses_control_and_lease_fence'
output: .......... [100%]
10 passed, 9 deselected in 0.28s
```

Changed-Python lint and diff check:

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen ruff check src/ttakkari/operations/inline.py unit_tests/test_collection_runtime_report.py
output: All checks passed!

argv: git diff --check
output: exit 0; no output
```

After rebasing the isolated change onto the latest fetched source `293bb8440fb96aaf648a59c7c821a99348ce325c`,
the same focused checks were rerun and passed with the outputs above. Rebase output:

```text
argv: git rebase FETCH_HEAD
output: Successfully rebased and updated refs/heads/codex/account-routing-lease-diagnostics-20261107.
```

The combined owner-operation smoke test reached and passed its `runtime_report` assertions, then failed later
in the same test at the unrelated `performance_baseline` assertion with `OSError:`. The code path uses
`shutil.disk_usage(settings.home)`; sandbox filesystem access is a possible cause, inferred rather than
verified. The test was not rerun unchanged.

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_collection_runtime_report.py tests/test_host_operations.py::test_inline_reports_are_recorded
output: ......F [100%]
        AssertionError: OSError:
        1 failed, 6 passed in 1.17s
```

An adjacent GPU resource-lease PID-reuse test also could not complete its strict process enumeration in the
Codex test environment and failed closed with `enumeration was incomplete; resource lease remains held`.
This test exercises the GPU resource lease subsystem, not Run executor leases; no workaround or process
signal was attempted.

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_host_resource_recovery.py -k 'orphan_lease_owner_lookup_failure_keeps_the_lease or orphan_lease_owner_pid_reuse_is_identity_mismatch_and_leaves_that_process_alone'
output: ...F [100%]
        GpuError: orphan lease recovery needs a complete process table; enumeration was incomplete, resource lease remains held
        1 failed, 3 passed, 26 deselected in 0.52s
```

## Independent verification input

Review the final commit on branch `codex/account-routing-lease-diagnostics-20261107` against its rebased
`origin/main` parent. Run only the focused test and lint commands above; inspect that archived/terminal/missing
Run rows remain visible, output stays capped at 100 with truncation reported, stale/mismatched leases do not
inherit the current account, and the new mapping contains no process identity, token, prompt, or result fields.
Do not push or deploy as part of this verification stage.

## Missing-session flag correction (2026-11-07)

The independent review found that `archived_at IS NOT NULL` alone serializes a missing outer-joined
`ChatSession` as `false`. Both lease and orphan-process-group mappings now test whether the joined session
row exists first; `session_archived` is `null` when it does not, and remains `false`/`true` for existing
active/archived sessions. `run_status="missing"` remains unchanged for a missing Run.

The new `runtime-mapping-missing-session-probe.py` is a correction-specific independent probe. It runs
`_lease_process_mappings` against an in-memory SQLite schema with synthetic rows and checks both SQL
mapping branches for active, archived, missing-session, and missing-Run/missing-session cases. It is not
a production database check or a live runtime report; it does not establish deployment behavior.

RED (before the source fix):

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen python runtime-mapping-missing-session-probe.py
output:
AssertionError: ('lease_only', 'absent_session', False)
exit: 1 (expected failure: missing joined session was incorrectly false)
```

GREEN (after the source fix):

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen python runtime-mapping-missing-session-probe.py
output:
lease SQL: active=false, archived=true, missing session=unknown; missing Run/session=unknown: PASS
orphan-group SQL: active=false, archived=true, missing session=unknown; missing Run/session=unknown: PASS
exit: 0
```

Focused verification after the correction:

```text
argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_collection_runtime_report.py
output: ...... [100%]; 6 passed in 0.34s

argv: UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen ruff check src/ttakkari/operations/inline.py runtime-mapping-missing-session-probe.py
output: All checks passed!

argv: git diff --check
output: exit 0; no output
```

No claim2/identity10, account/L2, or precision-evaluation regressions were rerun because this nullable
serialization fix does not alter those paths. Existing owner-smoke OSError and GPU process-enumeration
failure were not retried. No operating DB, cleanup/reconcile, process action, service restart, push, or
deploy was performed.

## Integration preparation (2026-11-07)

The current Run profile gate was rechecked before integration:

```text
argv: /Users/winterholic/bin/ttk get /api/sessions/808ab849-afef-4f69-9d10-d098537b91ed/runs | jq '[.[] | select(.status == "running") | {id,status,profile}]'
output: id=28ab85cf-0194-434e-8e6d-f27739dfe565, status=running, profile=회사 코덱스 ai2 (ai2@hyperez.io)
```

GitHub account listing showed `winterholic` registered and active. Latest remote main was fetched into
`refs/remotes/origin/main`:

```text
argv: GH_AS_USER=winterholic GH_AS_TOKEN="$(gh auth token --hostname github.com --user winterholic)" git -c credential.helper= -c 'credential.helper=!f() { echo username=$GH_AS_USER; echo password=$GH_AS_TOKEN; }; f' fetch origin main:refs/remotes/origin/main
output: 293bb8440fb96aaf648a59c7c821a99348ce325c (origin/main)
```

The isolated candidate branch was already a descendant of `origin/main`, so no merge/rebase or source
conflict resolution was needed. The candidate HEAD exactly matches the independently reviewed
`ee52abc78113a617e3821e4e3934ce28f1ab8c27`; `origin/main`, original integration baseline
`40e88581cc1010287ef2b82e13188e2d6c6e8df9`, and the earlier precision switch
`b51df6fd70c9dfa008d3ae3977b184bf8d9180e9` are ancestors (each `git merge-base --is-ancestor ... HEAD`
returned exit 0). This preserves the R3/L2/b51 baseline and adds the reviewed candidate unchanged.

Independent evidence is retained in this repository:

- `runtime-mapping-independent-review-6cdfa734.md` records the original missing-session finding and
  prior scope/limitations.
- `runtime-mapping-independent-review-ee52abc7.md` records the correction PASS and independent probe
  results for `lease_only`, matched lease/group, and orphan group rows.
- `runtime-mapping-independent-probe-ee52abc7.py` is the independent synthetic SQLite SQL probe.

The candidate's existing focused runtime-report test, correction-specific SQLite probe, Ruff, and diff
check receipts above remain the verification evidence. No claim2/identity10 or account/L2/precision full
regressions were repeated. The push and deployment reservation receipts will be appended after the
corresponding operations; the deployment activation status will not be polled.
