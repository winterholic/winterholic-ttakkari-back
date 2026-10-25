# P0 snapshot verification: disposable copies of immutable bundles

Base: `0570f9ec` (the concurrent SysV orphan sweep was fast-forwarded without
editing its two files). The shared dirty checkout was not changed.

At the scheduled check, deployment `2a9021a7-9589-4759-9dd0-253fcd9ed21d`
for `da2a40248b06f3883aa2240ba30b90f094fa8ae0` was `failed`, release pytest:
`1 failed, 1832 passed, 10 skipped, 2 xfailed` in 697.14s. The failure remained
`tests/test_host_operations.py::test_backup_restore_snapshot_end_to_end`,
at snapshot verification. Backup, isolated DB restoration and snapshot creation
had passed that test's assertions. This candidate was not activated.

The prior JSON assertion still lost the error because the supported
`ops_deploy_log` viewer clips every line at 300 characters. Summary, range and
the stored LOG-237 event did not expose the trailing error/output. No protected
raw log was opened. The assertion now formats JSON across lines and splits
`output_tail` into individual lines, so later failures can be diagnosed through
the supported viewer.

## Reproduction and cause scope

The Codex sandbox ran the actual snapshot script with the runner's minimal
environment shape, encryption and the actual result summarizer/cleanup:
ordinary files passed all stages. A separate `0444` plaintext cleanup returned
False and retained the file. The production game builder's `_freeze` creates
`0444` files and `0555` directories under `data/game-builds`; earlier
`test_game_build_snapshots` tests use that path. Native tar extraction retained
these source modes, so the disposable verification copy could not be wiped
and removed. The prior implementation also lacked writable directory handling.

Two actual encrypted snapshot/verify/cleanup regressions reproduced this:
readonly-file and readonly-tree both reached successful hash verification,
then failed on `_secure_rmtree`. This is a confirmed defect. Identity with the
clipped release error is still inferred, pending the controller's E2E result.

Only the verification extraction now uses the installed Python tarfile
`data` filter: regular files gain owner read/write and directory source modes
are ignored under the script's `umask 077`. Original files and permissions
stay unchanged. The archive writer still records original bytes/modes; hash
verification is unchanged. The filter also bounds extraction paths.

## Execution evidence

All commands below ran in `/private/tmp/tk-p0-snapshot-fix-20261025`.

- RED: `PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q
  unit_tests/test_backup_ops_remaining.py -k readonly_bundle`:
  `2 failed, 14 deselected in 0.84s` at plaintext cleanup after verification.
- GREEN: `PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q
  unit_tests/test_backup_ops_remaining.py unit_tests/test_backup_restore.py
  unit_tests/test_backup_status_allowlist.py`:
  `28 passed, 1 skipped in 5.91s`. The opt-in live Postgres rehearsal remains skipped.
- `.venv/bin/ruff check unit_tests/test_backup_ops_remaining.py
  tests/test_host_operations.py`: `All checks passed!`.
- `bash -n scripts/snapshot_data.sh` and `git diff --check`: exit 0.

Known Codex `setpriority` EPERM was not retried or bypassed. Full host-operation
E2E and complete release regression remain the deployment controller's check.
Production P0 backup/restoration and remote activation remain unexecuted.
