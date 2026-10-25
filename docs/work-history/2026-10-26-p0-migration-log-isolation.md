# P0 release gate: in-process migration logging interference

Base: `77c1238a2b8ade01c096dc74b4f4da3a52c89849`, clean task worktree
`/private/tmp/tk-p0-snapshot-fix-20261025`. Shared checkout preserved.

## Observed failure and exact reproduction

Deployment `2f8896ee-fc2e-4af9-8693-6ebc5fbf179e` failed release pytest at
2026-10-25T17:23:00Z: `1 failed, 3161 passed, 11 skipped, 2 xfailed` in 944.18s.
Supported `ops_deploy_log` showed
`test_raw_diagnostics_report_received_and_saved_file_without_logging_token`
failing because caplog contained no transfer events. Its assertions for HTTP201,
received bytes, saved path and physical file had already passed. This output
does not show a failed upload. The candidate was not activated.

In the Codex command environment, using only the provided Run-owned disposable
DB and `PYTHONPATH="$PWD/src"`, the exact sequence reproduced the symptom:

```
.venv/bin/python -m pytest -q --tb=short \
  tests/test_research_v2_migration.py \
  tests/test_transfers.py::test_raw_diagnostics_report_received_and_saved_file_without_logging_token
# RED: 1 failed, 2 passed in 1.16s; transfer events == empty set
```

The newly integrated Research migration tests execute Alembic commands within
the pytest process, passing `Config("alembic.ini")`. `alembic/env.py` calls
`fileConfig` for that config, replacing process logging handlers and disabling
existing application loggers. Setting a later logger's level with caplog does
not re-enable its `disabled` flag.

A new direct regression, `test_embedded_migration_preserves_application_logging`,
stamps the isolated DB head and checks the transfer logger stays enabled, root
capture handlers retain their identity, and a probe log is captured. Before the
fix it failed in 0.66s with `uvicorn.error.transfer.disabled == True`.

## Minimal correction and verification

Only the migration test helper changes to an in-memory Alembic Config with an
absolute `script_location`. This runs the same migration environment and DB
commands without loading the CLI's process-wide logging configuration. Runtime
`alembic/env.py`, transfer logic and CLI migration logging are unchanged.

```
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q --tb=short \
  tests/test_research_v2_migration.py tests/test_transfers.py
# GREEN: 32 passed in 2.54s
# DB resets: 32 selective, 0 forced full, 0 fallback
```

Ruff fixed one pre-existing import-order issue in the touched migration test;
no remaining issues. `git diff --check`: exit0. No standalone initdb,
setpriority, browser or alternate-engine retry was performed. Full regression
belongs to the deployment controller with `skip_tests=false`; local selected
checks are not deployment success. Production P0 and remote activation remain
unexecuted. Prior broad-test results retain their original revision/scope.
