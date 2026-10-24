# Context index cancellation verification

**Verdict: FAIL for the full requested contract.** Candidate `27a01fd21c82fc95721b54fe747b3a060062aaa0` (parent `05ae55daf6fb3a0105feee3e5921816884513c7d`); independent execution used Codex/GPT-6, not Flash. The manual-request recovery fix works on the background-consumer `pump()` → `run_claimed()` path, but `indexer.index_all()` bypasses `run_claimed()` and drops an existing manual request when canceled.

## Evidence

- 13 requested DB-free files: **125 passed in 50.00s**. Exact command (absolute pytest path because this verification ran in `/private/tmp`):

  ```sh
  PYTHONPATH=src:. /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest -q -p no:cacheprovider \
    unit_tests/test_local_api_retirement.py \
    unit_tests/test_tts_claim_stop_race.py \
    unit_tests/test_executor_integrated_drain.py \
    unit_tests/test_executor_safety.py \
    unit_tests/test_run_executor_drain.py \
    unit_tests/test_run_executor_services.py \
    unit_tests/test_executor_queued_consumers.py \
    unit_tests/test_creative_v2_asset_queue.py \
    unit_tests/test_creative_v2_asset_generation_e2e.py \
    unit_tests/test_desk_qwen_isolation.py \
    unit_tests/test_qwen_flash_monitor_jobs.py \
    unit_tests/test_run_executor_unit.py \
    unit_tests/test_context_index_queue.py
  # 125 passed in 50.00s
  ```
- Focused queue tests: **4 passed in 0.22s**. Isolated mutation removing `requeue_at = req`: **1 failed, 3 passed**; the manual-cancel restoration assertion failed.
- SQLite in-memory end-to-end probe through `BackgroundConsumers.stop(drain_seconds=0.01)`: `stop=False`, original request restored, `index_started_at` cleared; restart re-pumped once and completed (`attempts=2`, final request/started markers clear, `last_indexed_at` set).
- Two synchronized claims: exactly one `True`, one `False`, no exceptions. SQLite concurrency evidence only; PostgreSQL transaction behavior unverified.
- **Counterexample:** canceling `indexer.index_all()` while it owns a manually queued source left `index_requested_at=None`, `index_started_at=None`, and no cached claim request. `index_all()` calls `claim()` then its `finally` calls `release(sid)` without the saved request time (`src/ttakkari/context/indexer.py:204-218`). This path is also present in the parent, so it is an existing uncovered path rather than a regression introduced by this commit. Minimal correction: restore the saved request on `CancelledError` in `index_all()` while leaving unrequested periodic claims unqueued.
- Two other interleavings also reproduced on both candidate and parent: a request arriving between claim’s SELECT and UPDATE was accepted then erased; cancellation after claim commit but before task creation left `index_started_at` set and immediate re-pump returned `0`. These are pre-existing boundaries. Guard the UPDATE with the observed request timestamp; make claim commit-to-task handoff cancellation-safe with conditional cleanup/requeue.
- Generic `RuntimeError` from `index_source` is logged and clears `index_started_at`, but leaves `last_error=None` and the request cleared. The candidate test does not assert a durable failure marker; this behavior is unchanged from the parent.
- `ruff check src/ttakkari/context/index_queue.py tests/test_background_consumers.py unit_tests/test_context_index_queue.py`: `All checks passed!`. `git diff 05ae55d..HEAD --check`: passed. Candidate source worktree stayed unchanged; only this review note was added.

## Not verified

`tests/test_background_consumers.py` and the existing DB-backed suite were not run. The prior Codex command-sandbox loopback `Errno 1` is recorded in the handoff; per instruction it was not retried or bypassed. PostgreSQL behavior, the new DB integration test, and remote state remain unverified. The candidate is local only; no push or deployment was attempted. `manage_work`/`message_worker(verification_outcome)` tools are not available in this execution, so no worker event was sent.
