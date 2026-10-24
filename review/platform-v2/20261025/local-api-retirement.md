# Local API retirement / rollback lifecycle (base 61c285b)

Scope: local-mode API (rollback path) consumers: NotebookLM/Reverse/index (`background_consumers`),
Creative/game (`queued_work`), TTS drain. Activation stays disabled. No new lease/fence.

## Counterexamples (DB-free, `unit_tests/test_local_api_retirement.py`)
Red `local-api-retirement-red.txt`: 10 failed in 3.81s. Observed defects:
- `generation_service.ready` ignored consumer inflight tasks, a pump inside a claim round and Creative/game `run_tracked` jobs (`drain_ready` True).
- Draining transition did not stop new claims (Creative/game loop kept draining the backlog inside one work lock).
- `generation_service.lifespan` (local) cut claimed work with `background_consumers.stop(drain_seconds=0)`; shielded `run_tracked` jobs were abandoned while the engine was disposed; TTS `drain` was never used.
- `runtime_generation=false` (legacy `main.lifespan`) ran no Creative/game consumer, so work queued by a remote API stayed queued after rollback; its shutdown also used `stop(0)`.

## Correction
- `background_consumers`: `pause_claiming`/`resume_claiming`, `pumping`/`busy()`, `drain`/`stop` return True (finished) or False (bound expired, rest cancelled). Stop sequence calls `stop_claiming()` first to freeze new row claims and preserves un-claimed `queued` rows (NotebookLM/Reverse/index) intact during and after drain.
- `queued_work`: `inflight()`, `drain(timeout)` (closes claims, bounded wait, cancels leftovers, returns bool).
- `generation_service`: `ready` includes `consumer_work`; `coordinate` pauses on draining and resumes on return to active (local only); `finish_claimed_work` (shared bound 1800s, per-part result, warning on cut); `start_legacy_queued_consumers`.
- `main.lifespan` (legacy) starts those consumers and uses `finish_claimed_work` instead of `stop(0)`.

## Evidence
- Green `local-api-retirement-green.txt`: 11 passed in 1.79s (includes queued row preservation check).
- `local-api-retirement-regression-dbfree.txt`: 117 passed in 48.13s (12 DB-free unit_tests files).
- `local-api-retirement-regression.txt`: 20-file run with DB tests: 116 passed, 184 errors, all `PermissionError: [Errno 1]` at connect to the provided test DB 127.0.0.1:53007 (sandbox loopback denial). DB-backed tests are UNVERIFIED after this change: tests/test_background_consumers.py, test_game_build_queue.py, test_tts_queue.py, test_tts_executor_isolation.py, test_desk_qwen_service.py, test_generation_bootstrap.py, test_background_consumer_migration.py, test_migration_single_head.py, test_generation_deployment.py.
- `scripts/pytest_slots.py` could not open `/tmp/ttk-pytest-slots/slot-0.lock` (EPERM), so pytest ran directly.
- Step2 heartbeat coordination note: executor `drain()` emits initial `row.heartbeat_at = utcnow()` and updates tick/commands, while background consumer pumps operate under the executor or local API without separate DB heartbeat stamps. Full ownership lease binding during long consumer drains is tracked under Step2 verification.
- Not measured: real launchd, production, full suite, STT jobs shutdown (still `jobs.stop()`).
