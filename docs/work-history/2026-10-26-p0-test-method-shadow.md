# P0 release gate: inherited method shadow in Copilot test teardown

Base: `320e3519994675ec8a0da58a0b87a172bb9b86a2`, task worktree
`/private/tmp/tk-p0-snapshot-fix-20261025`; shared checkout untouched.

## Direct release evidence

`backend_deploy_status(218cf844-3ef8-4f45-9985-4b0682c7bb0b)` returned
`failed`, exact revision above, `skip_tests=false`, updated
2026-10-25T16:33:40.134595Z. Release pytest reported
`1 failed, 2733 passed, 11 skipped, 2 xfailed, 6 subtests passed in 922.70s`.
Supported `ops_deploy_log` range63-99 showed
`test_requeue_before_spawn_keeps_the_resume_handoff[column]` failing with
`calls.n == 0`, final status `succeeded`, error `None`.
The failure-injection wrapper was never reached; this output does not show a
production resume-handoff loss. Production P0 and remote activation remain unverified.

## Reproduction and cause

The preceding Copilot authentication-fallback test patched inherited `_attempt`
on the global manager instance. MonkeyPatch undo restores the original bound
method as an instance attribute. It consequently shadows subsequent patches of
`RunManager._attempt`, including the pool-timeout injection in the resume test.

Codex command sandbox, isolated disposable PostgreSQL and fake provider agents:

```
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q \
  tests/test_copilot_integration.py \
  tests/test_run_db_pool_budget.py::test_requeue_before_spawn_keeps_the_resume_handoff -x
# RED: 1 failed, 4 passed in 1.32s
# same calls.n=0 / succeeded / None as the release
```

Change only the Copilot test to patch the defining class and accept `self`.
Pytest can then restore the class definition without adding an instance shadow.
Neither runtime code nor pass criteria are weakened. Repository search for
`_attempt` setattr patches finds the Copilot class patch and the pool-test class
patch; no remaining instance `_attempt` patch in tests/unit_tests.

```
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q \
  tests/test_copilot_integration.py tests/test_run_db_pool_budget.py
# GREEN: 21 passed in 3.87s
```

These include all three handoff sources, bounded pool retries, side-effect
protection and Copilot authentication fallback. Full regression belongs to the
deployment controller; it is not duplicated locally. Known Codex setpriority
EPERM and browser Mach IPC denial were not retried or bypassed.

## Remaining boundary

Deploy only the committed clean candidate with `skip_tests=false`; reservation
acceptance is not success. After successful exact revision/readiness verification,
use supported host operations for backup status/runtime/performance, DB backup,
isolated restoration, data snapshot and verification sequentially, then continue
remote/mixed-generation/lossless/rollback/model/MCP/resume validation.

## Concurrent main integration

The first normal push was rejected as non-fast-forward: another lead had advanced
main with Research Lab V2. Fetch established base
`e9e32609ab5d5b8dbd5db25581658bbddcd9354a`; rebasing this two-file test-only
correction onto it completed without conflict. The incoming changes include
migration `0072_research_lab_v2`; the original 0071 single-head observation is
historical, and the cumulative candidate needs its current schema contract gate.
Existing research acceptance evidence remains scoped to that project's records.
The controller must validate the whole cumulative candidate, not just this fix.

## Cumulative schema preparation failures and correction

The incoming main actually contains both 0072 and 0073; its current head is
`0073_research_reuse_source`. The existing local Run-owned test DB predates the
new named `use_alter` foreign keys. On the cumulative candidate, test setup's
`Base.metadata.drop_all` attempted to drop a new constraint absent in that older
schema: `UndefinedObjectError: fk_research_experiments_source_exp` (the source_run
constraint also appears in the gate errors). The 21 selected cases therefore
reported setup errors, not failures of their behavior. Normal pre-push failed
in the contract and changed-test stages for the same fixture reason; nothing
was pushed. Its ruff stage passed.

Trying the documented independent-cluster mode once (`env -u TTAKKARI_TEST_DB`)
failed in Codex at `pg_sandbox.start_cluster -> initdb`, exit1; stderr was not
shown by conftest. Cause unknown, not evidence of host-wide failure. That path
was not retried or moved to another engine. Subsequent verification uses the
provided Run-owned disposable DB; no production DB access or URL disclosure.

Test setup now reflects actual existing constraints before dropping tables,
then creates current model metadata. This handles old/new/empty schemas without
assuming that old tables already have today's named constraints. The existing
production-DB guard remains in place. Add two regression cases in a unique
transactional probe schema: empty schema and legacy tables missing a newly named
alter-FK. Both must rebuild with the expected FK. The startup failure itself
provided RED before this fixture correction.

The incoming schema-policy unit test also had a stale expected current head:
`unit_tests/test_schema_head_admission.py` RED was `1 failed, 7 passed in 0.32s`,
0072 reported online instead of the test's expected current. Update 0072 to online
and add 0073 as current without weakening multiple-head rejection.

Cumulative GREEN:
```
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q --tb=short -x \
  tests/test_fixture_reset.py tests/test_copilot_integration.py \
  tests/test_run_db_pool_budget.py unit_tests/test_schema_head_admission.py
# 40 passed in 5.22s
# 32 selective resets, 1 forced full, 1 fallback; 115 model tables
```
Ruff on all four touched Python files and diff-check pass after import sorting.
The 21-pass result above belongs to the original 320e351 base plus method fix;
the 40-pass result is the cumulative e9e32609 integration plus schema corrections.
Full regression, actual deployment, production P0, and remote remain unverified.
