# Local API retirement / rollback lifecycle (base 61c285b)

Scope: local-mode API (rollback path) consumers: NotebookLM/Reverse/index (`background_consumers`),
Creative/game (`queued_work`), TTS drain. Activation stays disabled. No new lease/fence.

## Counterexamples (DB-free, `unit_tests/test_local_api_retirement.py`)
Red `local-api-retirement-red.txt`: 10 failed in 3.81s. Observed defects:
- `generation_service.ready` ignored consumer inflight tasks, a pump inside a claim round and Creative/game `run_tracked` jobs (`drain_ready` True).
- Draining transition did not stop new claims (Creative/game loop kept draining the backlog inside one work lock).
- `generation_service.lifespan` (local) cut claimed work with `background_consumers.stop(drain_seconds=0)`; shielded `run_tracked` jobs were abandoned while the engine was disposed; TTS `drain` was never used.
- `runtime_generation=false` (legacy `main.lifespan`) ran no Creative/game consumer, so work queued by a remote API stayed queued after rollback; its shutdown also used `stop(0)`.
- GPT independent verification FAIL: `background_consumers.stop()` waited for pump loops via unbounded `asyncio.gather(*loops)` before starting drain timer. If a pump was held (e.g. IO/claim), `stop()` and `finish_claimed_work()` hung indefinitely without timeout results or warnings (reproduction: fake pump held with event, `stop(drain_seconds=0.01)` -> 100ms later `stop_done=False`). Red: 2 failed in 2.06s in `unit_tests/test_local_api_retirement.py`.

## Correction
- `background_consumers`: `pause_claiming`/`resume_claiming`, `pumping`/`busy()`, `drain`/`stop` return True (finished) or False (bound expired, rest cancelled). Stop sequence calls `stop_claiming()` first to freeze new row claims and preserves un-claimed `queued` rows (NotebookLM/Reverse/index) intact during and after drain.
- `background_consumers.stop`: pump loop 대기(`asyncio.wait(loops, timeout=time_left())`)와 in-flight drain 전체를 `drain_seconds` 상한 내로 제한. 상한 초과 시 미종료 루프 태스크 취소 및 경고 로그, 잔여 in-flight 취소(`_cancel_inflight()`), 명시적 False 반환. 제시간 완료 시 잔여 시간으로 `drain()` 자연 완료 유지. 모듈 `stop()`도 bool 반환.
- `queued_work`: `inflight()`, `drain(timeout)` (closes claims, bounded wait, cancels leftovers, returns bool).
- `generation_service`: `ready` includes `consumer_work`; `coordinate` pauses on draining and resumes on return to active (local only); `finish_claimed_work` (shared bound 1800s, per-part result, warning on cut); `start_legacy_queued_consumers`.
- `main.lifespan` (legacy) starts those consumers and uses `finish_claimed_work` instead of `stop(0)`.

## Evidence
- Green `local-api-retirement-green.txt`: 14 passed in 2.05s (includes held pump timeout and queued row preservation checks).
- `local-api-retirement-regression.txt`: 121 passed in 48.30s (12 DB-free unit_tests files: test_local_api_retirement.py, test_tts_claim_stop_race.py, test_executor_integrated_drain.py, test_executor_safety.py, test_run_executor_drain.py, test_run_executor_services.py, test_executor_queued_consumers.py, test_creative_v2_asset_queue.py, test_creative_v2_asset_generation_e2e.py, test_desk_qwen_isolation.py, test_qwen_flash_monitor_jobs.py, test_run_executor_unit.py). 27,591줄의 loopback traceback 파일은 git rm 후 요약본으로 대체.
- DB-backed tests are UNVERIFIED after this change: tests/test_background_consumers.py, test_game_build_queue.py, test_tts_queue.py, test_tts_executor_isolation.py, test_desk_qwen_service.py, test_generation_bootstrap.py, test_background_consumer_migration.py, test_migration_single_head.py, test_generation_deployment.py.
- `test_generation_hardening.py` 기존 결함: FakeSlots.ensure_executor 부재 5건 중 4건은 fake 추가 시 해결되나 1건은 제품 코드 IdleRequired/StageError 거동 관련으로 제품 변경이 필요하여 지침대로 제품/테스트 미수정 유지.
- `scripts/pytest_slots.py` could not open `/tmp/ttk-pytest-slots/slot-0.lock` (EPERM), so pytest ran directly.
- Step2 heartbeat coordination note: executor `drain()` emits initial `row.heartbeat_at = utcnow()` and updates tick/commands, while background consumer pumps operate under the executor or local API without separate DB heartbeat stamps. Full ownership lease binding during long consumer drains is tracked under Step2 verification.
- Not measured: real launchd, production, full suite, STT jobs shutdown (still `jobs.stop()`).
