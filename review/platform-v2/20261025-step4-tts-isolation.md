# Step 4: 로컬 TTS 실행부 이관 (구현 인수 문서)

- 작성일: 2026-10-25
- 브랜치: `feat/platform-v2-tts-isolation-20261025` (기준 `cf7fc03`, 코드 기준 `bf2d566`)
- 범위: TTS 합성 consumer 를 remote API 에서 executor 로 이관. DB, 캐시 파일, 모델은 그대로 공유한다.

## 1. 동작 계약

| 항목 | remote API | executor | local API (롤백) |
|---|---|---|---|
| `POST /api/tts/jobs` | `queued` + `chunks_json` 을 `QUEUE_OWNER` 소유로 커밋하고 `pg_notify("tts")` | 해당 없음 | 기존과 같음(세대 소유, 메모리 원문 캐시, guard) |
| 합성 consumer | 시작하지 않음. `start()`, `kick()` 모두 no-op | `start_background_services()` 에서 `tts_jobs.start()` | 기존과 같음. active 일 때만 `QUEUE_OWNER` queued 도 FIFO 로 claim |
| claim | 없음 | `QUEUE_OWNER` queued 를 `created_at` 순으로 1건. claim 시 `owner_id` 를 executor id 로 변경 | 소유 queued + (active 일 때) `QUEUE_OWNER` queued |
| 교체 | `can_retire()` 가 TTS DB 조회 없이 `http_inflight`, `_work` 만 본다 | `drain()`: 새 claim 중지, 진행 중 1건 완주, queued 는 DB 에 보존 | 기존 `_has_pending_tts_jobs` 유지 |

핵심 선택 하나: remote API 가 접수한 job 의 `owner_id` 는 세대 id 가 아니라 고정 UUID(`jobs.QUEUE_OWNER`, uuid5)다. 스키마 변경과 lease 는 없다.
- `runtime_generations` 에 행이 없는 값이라 기존 사망 판정(`owner_dead`, `reap_dead_owner`)과 `_reap_stale_unknown_running` 에 걸리지 않는다. API 가 교체되어 offline 이 되어도 queued 가 `owner_gone` 으로 마감되지 않는다.
- claim 한 consumer 가 자기 id 로 owner 를 바꾸므로 running 행의 소유자 증거(flock/offline)는 기존 규칙 그대로 쓴다. 증거 없이 시간만으로 running 을 마감하지 않는다(소유자 행이 살아 있으면 어떤 나이여도 유지).
- 새 consumer 는 지금처럼 `NOT EXISTS running` + advisory lock 으로 한 번에 1건만 잡는다. executor 와 롤백한 local API 가 동시에 떠 있어도 이중 합성은 없다.

그대로 재사용한 것: `work-tts.lock`(`service.budget`), `defer_to_stt`, 청크 캐시 경로와 키, `_watch_cancel` 기반 DELETE 취소, queued TTL(`input_ttl_expired`, `QUEUE_OWNER` 에도 적용).

## 2. 변경 파일

- `src/ttakkari/tts/jobs.py`: `QUEUE_OWNER`, `remote_api()`, `accept()` remote 분기, `_claim(adopt_queue)`, `drain()`, `wake()`, 소유자 정리에서 `QUEUE_OWNER` 제외.
- `src/ttakkari/generation_service.py`: `start_api_services` 에서 `tts_jobs.start()` 를 local 분기로 이동, `lifespan` 종료의 `tts_jobs.stop()` 도 local 만.
- `src/ttakkari/executor.py`: `start_background_services` 에 `tts_jobs.start()`, `serve()` finally 에 `tts_jobs.drain()`.
- `src/ttakkari/generation.py`: NOTIFY payload `"tts"` 를 받으면 `tts_jobs.wake()`, remote API 의 `can_retire()` 는 TTS DB 조회 생략.
- 테스트: `tests/test_tts_executor_isolation.py`(신규 15건). 계약이 바뀐 기존 단언은 기대값만 갱신했고 삭제나 skip 은 없다.
  - `unit_tests/test_run_executor_services.py`: remote API 호출 목록 `["tts", "qwen"]` → `["qwen"]`(테스트 이름 변경), executor 호출 목록에 `"tts"` 추가.
  - `tests/test_desk_qwen_service.py`: remote 에서 `tts` 가 시작되지 않음을 단언.
  - `unit_tests/test_creative_v2_asset_generation_e2e.py`: `_LoopBoundRuntime.work()` 대역에 `allow_draining` 인자 추가. bf2d566 에서도 실패하던 기존 불일치다.

## 3. Step 2 와 함께 살릴 통합 주의점

1. `executor.py` 충돌 지점은 3곳이다: `start_background_services` 의 `tts_jobs.start()`, `serve()` 의 `tts_started` 플래그, finally 의 `tts_jobs.drain()`. Step 2 가 `serve()` 에 drain 루프를 넣으면 `tts_jobs.drain()` 은 `stop.is_set()` 직후(Run 완주 대기보다 앞)에 호출해야 한다. 그래야 Run 이 끝나길 기다리는 동안 TTS 가 새 job 을 계속 claim 하지 않는다. `drain()` 은 진행 중 1건이 끝나야 반환하며, 그 시간 상한은 합성 자체 타임아웃(`local_tts_timeout_seconds`, 기본 600초)이다. launchd 종료 유예가 이보다 짧으면 `확인 필요`.
2. `generation_service.start_api_services` 는 Step 3(Qwen)도 건드린다. 이 브랜치의 변경은 `tts_jobs.start()` 한 줄을 `if execution_mode == "local":` 안으로 옮긴 것뿐이다. 충돌하면 "remote 에서는 qwen 만 시작한다"를 유지하면 된다. `unit_tests/test_run_executor_services.py` 와 `tests/test_desk_qwen_service.py` 의 호출 목록 단언도 같은 방향으로 갱신해야 한다.
3. `generation.listener` 는 payload `"tts"` 를 처리하도록 3줄을 추가했다. Step 2 가 `received` 콜백을 바꾸면 이 분기를 유지해야 한다. 빠지면 executor 가 NOTIFY 대신 `runtime_poll_seconds`(기본 5초) 폴링으로만 job 을 집어 지연이 최대 5초 늘어나지만, 유실은 없다.
4. `ExecutorRuntime.register()` 의 `validate_handoff`(remote API 필수)는 건드리지 않았다. TTS 이관은 새 lease, fence, 소유권 이전을 추가하지 않는다.

## 4. 전환·롤백 호환과 이관 후 단순화 범위

현재 코드가 유지하는 안전장치(실제 구세대 → 신세대 전환과 롤백이 라이브에서 검증되기 전에는 삭제하지 않는다):

- `GenerationRuntime._has_pending_tts_jobs`, `_accepted_work`, `enter_accepted_work`, `work(allow_draining)`, `service.budget(allow_draining)`: local 모드와 롤백이 쓴다. remote API 는 `can_retire()` 에서 TTS 조회를 건너뛴다.
- `generation_deployment._wait_previous_tts_queue_empty`, `_hold_for_previous_tts`(30초 대기): 구버전 API 가 draining 진입 즉시 자기 queued 를 `generation_draining` 으로 실패시키는 결함을 우회한다. 구버전 바이너리는 새 코드로 바뀌지 않으므로 새 POST 수정만으로 첫 전환의 구세대 race 가 해결되었다고 선언할 수 없다. 컨트롤러가 새 코드로 도는 첫 전환에서는 이 대기가 계속 필요하다. 구세대 소유 job 이 없으면 대기는 즉시 끝나므로 비용도 없다.
- `_reap_stale_unknown_running`(소유자 행이 아예 없는 running 의 660초 마감): 이번에는 변경하지 않았다.

라이브에서 아래가 확인된 뒤 제거할 수 있는 후보(root 판단):
1. 전 슬롯이 이 브랜치 이후 코드이고 롤백 경로 시험을 마친 뒤 `_wait_previous_tts_queue_empty`, `_hold_for_previous_tts` 와 `generation_deployment.py` 의 호출 1곳.
2. local 모드를 완전히 폐기하기로 결정한 뒤 `_has_pending_tts_jobs`, `_accepted_work`, `enter_accepted_work`, `guard_scope`/`_guard_loop`, `allow_draining` 인자.
순서가 바뀌면 안 되는 이유: 롤백(local 복귀) 때 local API 가 이 코드들에 의존한다.

## 5. 미해결·미검증

- 라이브(executor 실제 기동, launchd 교체, 실제 Supertonic 모델, 구세대 → 신세대 전환) 미검증. 테스트는 가짜 합성 엔진과 임시 PostgreSQL 뿐이다.
- `POST /api/tts/speak`(단문 동기 합성)는 remote API 에서도 요청 범위로 API 프로세스가 자식 합성을 띄운다. HTTP in-flight 와 `work-tts.lock` 이 퇴역을 막으므로 요청 수명 안에서는 보호되지만, "API 합성 start 0" 은 `/jobs` 경로에만 해당한다. `/speak` 를 큐로 보내려면 별도 결정이 필요하다.
- executor 가 합성 중 SIGKILL 되면 running 행은 다음 executor 가 소유자 offline 증거를 확인한 뒤 `owner_gone` 으로 마감한다. 재실행(requeue)은 하지 않았다(합성이 프로세스를 죽이는 poison job 의 무한 반복을 막을 시도 횟수 필드가 없다).
- queued TTL(`local_tts_queued_ttl_seconds`, 기본 600초)이 `QUEUE_OWNER` 에도 적용된다. executor 가 10분 넘게 내려가 있으면 queued 는 `input_ttl_expired` 로 마감된다. 일반 교체(초 단위)에서는 해당 없음.
- 종료된 job 의 `chunks_json` 원문은 지우지 않는다(기존 FND-01 동작 유지).
