# P0 snapshot verification and cleanup correction

Parent: `44f88f83975124ca6b3f9d2ca66e212231cfd23e`.

Deployment reservation `f1633904-a412-4ed0-a607-2b408d2da6ec` failed in
release pytest (exit 1): `test_backup_restore_snapshot_end_to_end` reached
`snapshot_verify` and received `failed`; 1828 other tests passed. The log
truncated the operation dict, so its exact error/output is unavailable.
This is a release verification failure, not a production failure or SIGTERM.

An isolated reproduction found two concrete defects:

- `snapshot_data.sh` included symlinks in tar but excluded them from the
  manifest, producing `MISMATCH ...: manifest에 없는 파일` at verification.
- Both operation cleanup paths followed file symlinks while wiping/chmoding
  plaintext. A sentinel outside the cleanup directory was emptied. Directory
  and broken links also prevented cleanup from completing.

These defects are reproduced; their identity with the truncated release failure
remains an inference pending the controller's complete integration run.

The snapshot writer now uses one regular-file scope, excludes symlinks and
special files, and computes manifest hashes from captured archive bytes.
Cleanup shares an unlink helper that opens regular files without following
links and deletes symlinks themselves. The failing integration assertion now
prints the complete sanitized operation result as JSON if it fails again.

Evidence in the Codex worker sandbox, `PYTHONPATH=$PWD/src`:

- Before correction: snapshot-link roundtrip and cleanup file/directory/broken
  symlinks: `4 failed`; separate residual-cleanup sentinel test: `1 failed`.
- After correction: backup/restore/snapshot script tests, allowlist, and new
  cleanup regressions: `30 passed, 1 skipped in 5.92s` (live Postgres rehearsal
  opt-in remains skipped).
- Existing cleanup-failure and pidfile boundaries: `8 passed in 1.80s`.
- Ruff on changed Python, `bash -n scripts/snapshot_data.sh`, `git diff --check`:
  passed after adding explicit `check=False` to the touched test subprocess helper.

The known sandbox `os.setpriority` EPERM is not retried or bypassed. Full
operation E2E and complete release regression must run in the supported
deployment controller. No production backup/restore/remote activation has run.
