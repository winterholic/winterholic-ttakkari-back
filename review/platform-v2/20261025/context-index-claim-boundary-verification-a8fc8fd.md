# Context index claim boundary verification

**Verdict: PASS for the requested claim/cancellation boundaries.** Candidate `a8fc8fd8da662229be023936b6cb9607d5960849`, parent `27a01fd21c82fc95721b54fe747b3a060062aaa0`. Candidate was checked in a detached `/private/tmp` worktree; the source worktree was left unchanged except for this untracked review record.

## Evidence

- Parent red: copied only the candidate's `unit_tests/test_context_index_queue.py` into the detached parent worktree and ran it. Output: `3 failed, 4 passed in 0.27s`. The three new tests failed for periodic `index_all()` cancellation request loss, SELECT/UPDATE request race, and pre-spawn cancellation leaving `index_started_at` set.
- Candidate DB-free suite: all 13 requested files, exact configured pytest executable and flags. Output: `128 passed in 48.89s`.
- Candidate focused DB-free file is included in that run: 7 tests passed. This includes periodic manual request restoration, NULL `req_cond` behavior (`IS NULL`), no-request periodic claim cancellation, normal completion, ordinary exception, CAS race, and pump pre-spawn cancellation.
- Supplemental temporary boundary probes: `3 passed in 0.33s`. Concurrent claims on one source produced exactly one claimant; CAS loss preserved the newly queued request and a subsequent pump claimed and completed it; `_start_index` cancellation released the claim while successful task creation did not release it.
- Four isolated mutations all produced the expected test failures:
  1. `restore_req = None`: 3 failed (manual run cancellation, periodic cancellation, pump pre-spawn cancellation).
  2. `indexer.index_all()` cancellation flag forced false: 1 failed.
  3. CAS `req_cond` removed: 1 failed.
  4. `pump()` handoff cleanup removed: 1 failed.
- PostgreSQL integration test (single requested attempt):
  `PYTHONPATH=src:. /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest -q -p no:cacheprovider tests/test_background_consumers.py`
  Output: `26 passed in 3.36s`. `tests/conftest.py` creates a temporary PostgreSQL cluster when no explicit test DB is set; no `.env` file was read.
- Ruff: `All checks passed!` for all five changed Python files. `git diff 27a01fd..HEAD --check`: exit 0.

## Boundary findings

- `src/ttakkari/context/indexer.py:217-222`: `CancelledError` marks cancellation and the shielded release restores the cached manual request. Ordinary exceptions leave the request cleared; periodic claims without a manual request do not synthesize one.
- `src/ttakkari/context/index_queue.py:61-64`: CAS uses `IS NULL` when the observed request is NULL and exact equality otherwise. On a raced update the claim returns false; the queued request remains eligible for the next pump. Same-source concurrent claims remain exclusive.
- `src/ttakkari/context/index_queue.py:78-80, 128-130` and `src/ttakkari/api/assistant.py:476-483`: cancellation restoration is conditional on the cancelled handoff; successful task creation does not call release.
- SQLite exercised timezone-aware UTC writes and SQLite's naive datetime round-trip in the CAS and cancellation checks. PostgreSQL `test_background_consumers.py` exercised request timestamp restoration for periodic cancellation. A direct PostgreSQL CAS race/equality check was not separately run; PostgreSQL timestamp comparison for that interleaving remains unverified.

## Scope

No product/test source changes, push, main integration, deployment, or operational action. PostgreSQL coverage is limited to the one specified test module. The original worktree's only addition is this untracked record.
