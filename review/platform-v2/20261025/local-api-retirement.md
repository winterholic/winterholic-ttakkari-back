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
- Green `local-api-retirement-green.txt`: 14 passed in 2.32s (includes held pump timeout and reinforced queued row preservation checks).
- Queued row preservation mutation test: `test_drain_preserves_queued_rows_for_notebooklm_reverse_and_context_index`를 실제 `consumers.start()` 구동 하에서 1차 claim → `stop_claiming()` → `drain()` 경로를 타도록 강화. 보존 로직 무력화 변이(`stop_claiming` no-op 또는 호출 누락) 적용 시 1차 완료 후 루프가 미착수 2차 queued 행까지 claim하여 `assert queued_store["notebooklm"] == ["job-queued-2"]`에서 `assert [] == ['job-queued-2']`로 확실하게 실패(Red)함을 입증. 정상 복원 시 Green(`1 passed in 0.28s`).
- `local-api-retirement-regression.txt`: 121 passed in 47.65s (12 DB-free unit_tests files: test_local_api_retirement.py, test_tts_claim_stop_race.py, test_executor_integrated_drain.py, test_executor_safety.py, test_run_executor_drain.py, test_run_executor_services.py, test_executor_queued_consumers.py, test_creative_v2_asset_queue.py, test_creative_v2_asset_generation_e2e.py, test_desk_qwen_isolation.py, test_qwen_flash_monitor_jobs.py, test_run_executor_unit.py). 27,591줄의 loopback traceback 파일은 git rm 후 요약본으로 대체.
- DB-backed tests are UNVERIFIED after this change: tests/test_background_consumers.py, test_game_build_queue.py, test_tts_queue.py, test_tts_executor_isolation.py, test_desk_qwen_service.py, test_generation_bootstrap.py, test_background_consumer_migration.py, test_migration_single_head.py, test_generation_deployment.py.
- `test_generation_hardening.py` 기존 결함: FakeSlots.ensure_executor 부재 5건 중 4건은 fake 추가 시 해결되나 1건은 제품 코드 IdleRequired/StageError 거동 관련으로 제품 변경이 필요하여 지침대로 제품/테스트 미수정 유지.
- `scripts/pytest_slots.py` could not open `/tmp/ttk-pytest-slots/slot-0.lock` (EPERM), so pytest ran directly.
- Step2 heartbeat coordination note: executor `drain()` emits initial `row.heartbeat_at = utcnow()` and updates tick/commands, while background consumer pumps operate under the executor or local API without separate DB heartbeat stamps. Full ownership lease binding during long consumer drains is tracked under Step2 verification (검증자 판정: 결함 아님 확인).
- Not measured: real launchd, production, full suite, STT jobs shutdown (still `jobs.stop()`).

## Context Index Queue Cancellation Preservation (base 05ae55d)
Scope: `src/ttakkari/context/index_queue.py` manual index claim/cancellation lifecycle.

### Observed Defect
`claim()`이 `index_requested_at=None`으로 수동 색인 요청을 지운 상태에서 종료 상한 초과 등으로 실행 중 작업이 취소되면, `run_claimed()`가 `CancelledError`를 다시 던지고 `release()`는 `index_started_at`만 비워 수동 색인 요청이 재대기 표시도 실패 기록도 없이 영구 소실됨.

### Counterexamples & Red
- `unit_tests/test_context_index_queue.py`:
  Red `PYTHONPATH=src:. ../back/.venv/bin/pytest -q -p no:cacheprovider unit_tests/test_context_index_queue.py`: `1 failed, 3 passed in 0.23s`.
  `FAILED unit_tests/test_context_index_queue.py::test_claimed_manual_index_request_is_restored_to_queued_on_cancellation` - `assert None == datetime(...)` (취소 후 index_requested_at 소실 재현).
- DB-backed test `tests/test_background_consumers.py::test_manual_index_request_is_restored_to_queued_on_cancellation` 작성 완료. 샌드박스 loopback 소켓 연결 차단(`PermissionError: [Errno 1] Operation not permitted`)으로 미실행 표기.

### Correction
- `index_queue.claim`: 출처의 `index_requested_at` 및 `index_started_at`을 원자적으로 확인하여 수동 요청 시각(`req`)을 보관하고 claim 확정.
- `index_queue.run_claimed`: `except asyncio.CancelledError` 경로에서 수동 요청이었던 경우에만 `requeue_at = req`를 지정하여 `release()`에 전달하고 `CancelledError` 재전파. 정상 완료 및 일반 예외 경로 동작은 불변 유지.
- `index_queue.release`: `requested_at` 인자를 지원하여 취소 복원 시 `index_requested_at`을 원래 요청 시각으로 원자적 복원하고 `index_started_at`을 해제. 주기 색인(`index_all`) 등 수동 요청이 아닌 claim은 `requested_at=None`으로 유지되어 `index_requested_at`이 생성되지 않음.
- `index_queue.pump`: 선별 시점의 `index_requested_at`을 `run_claimed`에 연계 전달.

### Evidence & Mutation
- Green `unit_tests/test_context_index_queue.py`: `4 passed in 0.24s`.
- Mutation test: `requeue_at = req` 복원 라인 무력화 시 `test_claimed_manual_index_request_is_restored_to_queued_on_cancellation`에서 `assert None in (...)`으로 즉시 Red 실패 확인(`1 failed, 3 passed in 0.23s`). 정상 복구 시 Green(`4 passed in 0.21s`).
- 13개 DB-free 회귀 스위트: `125 passed in 48.47s` (test_local_api_retirement.py, test_tts_claim_stop_race.py, test_executor_integrated_drain.py, test_executor_safety.py, test_run_executor_drain.py, test_run_executor_services.py, test_executor_queued_consumers.py, test_creative_v2_asset_queue.py, test_creative_v2_asset_generation_e2e.py, test_desk_qwen_isolation.py, test_qwen_flash_monitor_jobs.py, test_run_executor_unit.py, test_context_index_queue.py).
- Ruff check 통과, git diff --check 통과.

## Context Index Manual Request Loss Counterexamples Correction (base 27a01fd)
Scope: `src/ttakkari/context/index_queue.py`, `src/ttakkari/context/indexer.py`, `src/ttakkari/api/assistant.py`.

### Observed Defects & Counterexamples
1. 주기 `indexer.index_all()`이 수동 요청이 걸린 source를 claim한 뒤 취소되면 수동 요청이 영구 소실됨 (27a01fd 복원은 background consumer 경로에만 적용됨).
2. claim의 SELECT와 UPDATE 사이에 새 수동 요청이 들어오면 claim이 `index_requested_at=None`으로 덮어 새 요청이 유실됨.
3. claim commit 직후, 실행 task 생성 전에 취소되면 `index_started_at`이 남아 행이 stuck 됨.

### Red Test Results
`PYTHONPATH=src:. ../back/.venv/bin/pytest -q -p no:cacheprovider unit_tests/test_context_index_queue.py`:
`3 failed, 4 passed in 0.26s`
- `FAILED test_index_all_restores_manual_index_request_on_cancellation` - `assert None in (...)` (index_all 취소 시 수동 요청 소실 재현).
- `FAILED test_claim_compare_and_set_guards_against_concurrent_request_race` - `assert True is False` (SELECT/UPDATE 사이 신규 요청 덮어쓰기 재현).
- `FAILED test_pump_claim_cancellation_before_task_spawn_releases_started_marker` - `assert datetime(...) is None` (claim 후 task 시작 전 취소 시 행 stuck 재현).

### Minimal Corrections
- `index_queue.claim`: UPDATE 조건에 관측된 `index_requested_at` 일치 조건(`req_cond`)을 compare-and-set 가드로 추가하여, 조회와 업데이트 사이에 새 수동 요청이 들어오면 claim이 안전하게 실패(`return False`)하고 새 요청을 보존.
- `index_queue.release`: `cancelled` 플래그를 지원하여 `cancelled=True`일 때만 원래 수동 요청 시각(`(requested_at or cached_req)`)을 복원하고, 정상 완료 및 일반 예외는 `index_requested_at=None` 유지. 주기 claim은 새 요청을 만들지 않음.
- `indexer.index_all`: `CancelledError` 발생 시 `cancelled=True`로 `index_queue.release(sid, cancelled=cancelled)`를 호출하여 수동 요청 복원 연계.
- `index_queue.pump` & `api.assistant._start_index`: claim 성공 후 실행 task 생성/등록 handoff를 `try/finally`로 보호하여, task 시작 전 취소 시 즉시 `release(sid, cancelled=True, requested_at=req_at)` 호출로 stuck 해소 및 요청 복원.

### Evidence & Mutation
- Green `unit_tests/test_context_index_queue.py`: `7 passed in 0.23s`.
- Mutation tests:
  1. `release` 복원 줄 무력화 (`restore_req = None`): 3 failed, 4 passed (`test_claimed_manual_index_request_is_restored_to_queued_on_cancellation`, `test_index_all_restores_manual_index_request_on_cancellation`, `test_pump_claim_cancellation_before_task_spawn_releases_started_marker` 실패 확인).
  2. `index_all` 취소 복원 무력화 (`cancelled=False`): 1 failed, 6 passed (`test_index_all_restores_manual_index_request_on_cancellation` 실패 확인).
  3. `claim` compare-and-set 무력화 (`req_cond` 제거): 1 failed, 6 passed (`test_claim_compare_and_set_guards_against_concurrent_request_race` 실패 확인).
  4. `pump` try/finally 해제 무력화 (release 제거): 1 failed, 6 passed (`test_pump_claim_cancellation_before_task_spawn_releases_started_marker` 실패 확인).
- 13개 DB-free 회귀: `128 passed in 48.34s`.
- Ruff check 통과, git diff --check 통과.
- PostgreSQL DB 직결 테스트: 샌드박스 loopback 차단(`PermissionError: [Errno 1]`)으로 작성 완료 후 미실행 표기.
