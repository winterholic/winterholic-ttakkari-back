# Step2 executor safety correction

Base: `61ba006ffcc716be025e1efad49c1c992e70671f`; test-first commit: `352a5dd`.
Worktree: `back-platform-v2-executor-safety-20261025`.

The original 16 regression cases failed on the baseline (1.63s). Raw command,
cwd and output are in `executor-safety-red.txt`. They passed after correction
(`executor-safety-green.txt`, 16 passed/1.45s). A further offline-state shutdown
case failed before adding the registration-based drain guard
(`executor-safety-offline-red.txt`, 1 failed/1 passed).

Changes:
- Pointer loss does not report a clean drain while handles/work remain.
- Drain timeout continues waiting for natural completion before manager shutdown.
- A registered executor also drains after its database state becomes offline.
- Process start time and exact command are checked against the owner's immutable
  release marker and interpreter before signalling. Unknown identity refuses stop.
- A live process past the timeout is not booted out. No DB owner means no bootout.
- Activation gates precede stop. API-only revisions preserve a healthy executor;
  explicit `replace=True` is a separate Python lifecycle operation. The CLI and
  deployed controller still need integration verification for executor releases.

Validation:
- `scripts/pytest_slots.py --timeout 90 -- uv run pytest -q
  unit_tests/test_executor_safety.py unit_tests/test_run_executor_drain.py
  unit_tests/test_run_executor_unit.py unit_tests/test_run_executor_services.py
  unit_tests/test_executor_reconcile.py tests/test_generation_deployment.py
  tests/test_run_executor.py`: 129 passed in 6.56s. Command/cwd/output:
  `executor-safety-regression.txt`.
- After the equivalent Ruff SIM102 cleanup, final safety delta: 17 passed in
  0.49s (`executor-safety-final.txt`). Ruff on the three changed Python files
  reports `All checks passed!`; `git diff --check` is clean.
- Python pitfall scan found one existing text-open-without-encoding in the
  untouched deployment logging code (baseline had the same call). Not expanded
  into this correction. No configured mypy/pyright command in pyproject.toml.

All process operations in these safety tests are fakes; related integration
tests use isolated PostgreSQL. Actual launchd, host child signals, long-running
model tasks, API redeployment, executor cutover/rollback and operational recovery
are unverified. Activation remains disabled. New lease/fence protocols were not
introduced. The shared dirty `back` and other workers' worktrees were preserved.
