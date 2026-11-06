# Missing-session mapping correction: independent review

Date: 2026-11-07 KST

## Verdict

**PASS for code-level integration of the nullable session-archive correction.** The corrected code preserves `false` for an existing active session and `true` for an existing archived session, while emitting null/unknown for a missing outer-joined session. `run_status="missing"` remains intact when the Run row is absent. No candidate source was modified and no merge/push/deploy was performed.

Reviewed candidate SHA: `ee52abc78113a617e3821e4e3934ce28f1ab8c27`  
Parent: `6cdfa7341cc00b48a37c310638a5faab90ea84bc`  
Working tree: clean at candidate HEAD.

## 1. Current Run profile gate

Command:

```text
/Users/winterholic/bin/ttk get /api/sessions/ee9c3ee9-2604-4087-94a6-bf95da5d42de/runs
```

The returned Run list was projected to identity/status fields for this excerpt:

```text
argv: /Users/winterholic/bin/ttk get /api/sessions/ee9c3ee9-2604-4087-94a6-bf95da5d42de/runs | jq '[.[] | {id,status,profile,started_at}]'
{
  "id": "70425ffd-a2f5-4d6c-bf03-60779605fc05",
  "status": "running",
  "profile": "회사 코덱스 ai2 (ai2@hyperez.io)",
  "started_at": "2026-11-06T18:13:45.342298Z"
}
```

Gate PASS. Neither `pool` nor the sessions-list `active_run_id` was used as account evidence.

## 2. Diff and correction-specific probe review

The `6cdfa734..ee52abc7` source diff changes only `session_archived` in the two mapping SELECT projections. Both use the equivalent of:

```sql
CASE WHEN sessions.id IS NOT NULL
     THEN sessions.archived_at IS NOT NULL
     ELSE NULL
END
```

The lease query branch covers both `lease_only` and exact `lease_process_group` rows. The orphan query branch covers `orphan_process_group`. Serialization passes SQL NULL through without boolean coercion; Run status still uses `row.run_status or "missing"`.

The supplied `runtime-mapping-missing-session-probe.py` exercises lease-only and orphan-group cases for active, archived, missing-session, and missing-Run/missing-session states. It does **not** create an exact lease/group match: its process-group fixture rows have independently generated generation/token values and no matching corresponding lease rows. I therefore did not rely on it alone; the independent probe below explicitly covers all three output kinds.

## 3. Probe results

Implementation probe exact argv and output:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen python /private/tmp/back-account-routing-lease-20261107/runtime-mapping-missing-session-probe.py

lease SQL: active=false, archived=true, missing session=unknown; missing Run/session=unknown: PASS
orphan-group SQL: active=false, archived=true, missing session=unknown; missing Run/session=unknown: PASS
```

Independent probe saved at `/private/tmp/account-routing-lease-20261107/runtime-mapping-independent-probe-ee52abc7.py`. It runs `_lease_process_mappings` against a separate in-memory SQLite fixture with genuine matched lease/group token+generation rows, lease-only rows, and groups with no exact lease. For each output kind it checks active session, archived session, absent session, and absent Run plus absent session. Exact argv and output:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen python /private/tmp/account-routing-lease-20261107/runtime-mapping-independent-probe-ee52abc7.py

lease_only: active=false, archived=true, missing session=null, missing Run/session=null: PASS
matched group: active=false, archived=true, missing session=null, missing Run/session=null: PASS
orphan group: active=false, archived=true, missing session=null, missing Run/session=null: PASS
```

This validates generated query semantics on synthetic SQLite data. It is not PostgreSQL execution or production data evidence. The earlier parent review's SQL join, account attribution, 100-row cap/truncation, output allowlist, and SELECT-only evidence remains applicable because this correction changes only the two archive-state expressions.

## 4. Narrow regression and hygiene checks

The correction-specific runtime report regression, changed-Python Ruff, and candidate diff check were run:

```text
UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen pytest -q unit_tests/test_collection_runtime_report.py
...... [100%]
6 passed in 0.34s

UV_CACHE_DIR=/private/tmp/ttakkari-lease-diagnostics-uv-cache uv run --frozen ruff check src/ttakkari/operations/inline.py runtime-mapping-missing-session-probe.py
All checks passed!

git diff --check 6cdfa7341cc00b48a37c310638a5faab90ea84bc ee52abc78113a617e3821e4e3934ce28f1ab8c27
exit 0; no output
```

`git status --short` produced no output; `git rev-parse HEAD` returned `ee52abc78113a617e3821e4e3934ce28f1ab8c27`; `git rev-parse ee52abc78113a617e3821e4e3934ce28f1ab8c27^` returned `6cdfa7341cc00b48a37c310638a5faab90ea84bc`.

Claim2 and identity10 checks and account/L2/precision-evaluation regressions were not repeated; the prior independent review remains the evidence for those unaffected paths. The previous owner-smoke OSError and GPU process-enumeration failure were not retried.

## 5. Operational boundary

No operating database was queried. No deployed runtime report was invoked, so this does not prove the target generation appears in the first 100 mappings. Deployment activation, cleanup/reconcile, process actions, service restart, account toggles, scope changes, and push were not performed. Operational visibility and deployment behavior remain unverified.
