# P0 release gate: stale usage fixture crossed a calendar reset

Candidate `4053ee12ea928dd683260482c6d8e71c74f1db3e`, deployment
`831bcd9a-98db-4475-b072-bccef6c70a29`, failed the controller's release pytest:
`1 failed, 5373 passed, 13 skipped, 2 xfailed, 5020 subtests passed` in
1260.36s. Supported `ops_deploy_log` identified
`test_old_claude_value_is_marked_stale_with_the_reason_not_shown_as_current`:
its windows were `stale` and `unavailable`, rather than both `stale`.
The candidate was not activated.

The helper made an observation 20 hours old relative to the current clock, but
fixed the weekly reset to `1792933199` (2026-10-25T12:59:59Z). That date had
passed. Runtime `accounts._usage_out` correctly marks an expired window
`unavailable` before applying the observation's `stale` state. This was a test
fixture's changing meaning, not evidence that runtime expiry handling broke.

In the Codex command environment, using the existing task worktree and
`PYTHONPATH="$PWD/src"`:

```sh
.venv/bin/python -m pytest -q \
  unit_tests/test_usage_failure_visibility.py::test_old_claude_value_is_marked_stale_with_the_reason_not_shown_as_current --tb=short
# Original RED: 1 failed in 0.81s, same stale + unavailable assertion.
# After fixing test clocks before/after the original reset and in the far future:
# RED: 2 failed, 1 passed in 0.46s.
```

The helper now captures `now` once and uses a weekly reset seven days ahead.
It still supplies a 20-hour-old observation. The calendar cases remain in the
test. The existing expiry test now covers both fresh and stale observations,
preserving the separate contract that an expired window is unavailable.
No runtime code, expiry precedence, or deployment gate was changed.

```sh
PYTHONPATH="$PWD/src" .venv/bin/python -m pytest -q \
  unit_tests/test_usage_failure_visibility.py --tb=short
# 26 passed in 0.48s
.venv/bin/ruff check unit_tests/test_usage_failure_visibility.py
# All checks passed!
git diff --check
# exit0
```

Before editing, the clean checkout advanced by fast-forward to cumulative
Research main `6b7c3fae` (including the existing `4053ee12` inventory fix).
The Research lead was executing a separate selection/provenance stage; its
latest recorded prompt owned controller `afdbdd60/e5f8d247`, not this fixture
correction. Existing Research and V2 evidence is retained without repeating
those suites. Full regression stays with the controller, `skip_tests=false`.
Production backup, isolated restore, snapshot verification and remote
activation remain unexecuted. Known sandbox denials were not retried.
