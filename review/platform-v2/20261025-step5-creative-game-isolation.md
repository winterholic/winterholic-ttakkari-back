# Step 5 구현 기록: Creative V2 자산 생성과 게임 빌드의 실행부 이관

- 작성일: 2026-10-25
- 기준: `cf7fc03` (소스는 `bf2d566`), 브랜치 `feat/platform-v2-creative-game-isolation-20261025`
- 범위: 계획서 Step 5. 다른 단계(executor 드레인, Qwen, TTS, 수집기)의 프로토콜은 바꾸지 않았다.

## 1. 무엇이 바뀌었나

원격 모드(`run_executor_mode=remote`)의 API는 요청을 내구 저장소에 남기고 조회만 돌려준다. 장시간 실행은 실행부가 한다.
`queued_work.delegated()`가 참인 경우는 `generation.current`가 `role=api`, `execution_mode=remote`일 때뿐이다(`runs.manager.remote`와 같은 기준).
로컬 모드와 non-generation 모드는 기존 `asyncio.create_task` 경로를 그대로 쓴다.

### Creative V2 (`POST /api/creative/v2/plans/{pid}/production/generate-assets`)

- API: `asset_generation.enqueue_assets()`가 `production.json`의 `asset_generations`에 `status="queued"` 기록(`requested_at` 포함)을 남기고 `{plan_id, plan_revision, status}`를 돌려준다. 응답 모양과 HTTP 상태(202)는 그대로다.
  - 같은 키가 이미 `queued`면 새 기록을 만들지 않는다. 이미 `succeeded`면 기존 계약대로 `queued`로 답하고 아무것도 쓰지 않는다.
  - `running` 기록은 `generation.lock`(flock)을 살아 있는 실행자가 잡고 있을 때만 `running`으로 답한다. 잠금이 풀린 고아 `running`은 사용자가 다시 요청할 때만 같은 기록(id, progress 보존)으로 `queued`에 올린다.
- 실행부: `asset_worker.consume_once()`가 `queued` 기록만 요청 순서로 하나씩 `generate_assets()`에 넘긴다. 이전 프로세스가 남긴 `running`은 절대 자동 재실행하지 않는다(제출된 Comfy task, 잡힌 GPU 같은 부수효과를 사용자 재요청이 정리한다).
  - 시작 전 검증에서 막힌 요청(승인 revision이 바뀜 등)은 `failed`로 닫아 맨 앞 요청이 큐를 막지 않게 한다. 살아 있는 다른 실행자가 같은 키를 돌리면(`AssetGenerationBusy`) `queued`로 보존하고 다음 요청을 본다.
  - API 접수 뒤 실행부가 새 엔진 버전으로 교체돼 생성 키가 달라져도, 같은 revision의 대기 요청을 현재 엔진이 이어받는다.
- `_write_checkpoint()`: 임시 파일에 쓰고 `flush` + `os.fsync` 후 `replace`, 이어서 디렉터리 `fsync`. 실패하면 임시 파일을 지우고 이전 checkpoint를 보존한다.

### Game Studio 빌드 (`POST /api/games/{id}/builds`, `POST /api/games/{id}/rollback`의 복구 빌드)

- 모델: `game_builds.queue_state`(`queued`/`claimed`/NULL), `game_builds.claimed_at` 두 nullable 열(마이그레이션 `0069_game_build_executor_handoff`, expand-only). `status`는 계속 `building`이라 `BuildOut`(`building|succeeded|failed`)과 프론트 계약은 그대로다.
- API: `prepare_build(..., queued=True)`로 `building` + `queue_state=queued` 행만 만들고 task를 띄우지 않는다. 게임 행 `FOR UPDATE`로 게임당 진행 빌드 1개를 지키는 규칙은 그대로라, 대기 중인 게임에 다시 접수하면 `409 build_in_progress`다.
- 실행부: `consume_queued_build()`가 가장 오래된 `queued` 행을 조건부 `UPDATE ... WHERE queue_state='queued'`로 맡고(`claimed`, `claimed_at`), 기존 `execute_build()`를 그대로 실행한다. `queue_state`가 NULL인 `building` 행(로컬 API 빌드, Run 자동 빌드, 이 표식 이전 행)은 후보가 아니다.
  - stale 판정은 맡은 시각(`claimed_at`, 없으면 `created_at`)이 기준이다. `queued` 행은 오래 기다려도 stale이 아니다(실행부가 멈춰도 접수 순서와 내용이 보존된다).
  - 맡았지만 실행할 수 없는 행(저장소·workspace 소실)은 `failed`로 닫는다.

### 공통 (`ttakkari/queued_work.py`, 약 50줄)

큐 인프라가 아니라 드레인 스위치다. `begin_drain()`(새 claim 중단), `run_tracked()`(소비 루프가 취소돼도 맡은 일은 끝까지 shield), `wait_idle()`(맡은 일 대기). 내구 큐는 각 도메인의 기록(`production.json`, `game_builds`)이다.

## 2. 통합 hook (다른 단계와 만나는 곳)

| 파일 | 변경 | 통합 시 확인 |
|---|---|---|
| `generation_service.maintenance_jobs()` | `("creative-assets", ...)`, `("game-builds", ...)` 주기 작업 2개 추가. 작업 한 번에 대기분을 끝까지 소비한다 | 실행부(`executor.start_background_services`)와 로컬 모드 API가 같은 목록을 쓴다. 두 소비자는 `generation.work(name)` 잠금으로 프로세스 간 직렬화된다 |
| `executor.serve()` finally | 종료 때 `queued_work.begin_drain()` 후 `await queued_work.wait_idle()`을 주기 작업 취소 앞에 둔다 | Step 2의 드레인 루프로 이 블록이 옮겨져도 두 호출은 "주기 작업 task를 취소하기 전"에 있어야 한다 |
| `deploy/generation-contract.json` | `schema_revision`을 0069로, `online_migration_from`에 0068 추가 | Step 3/4/6이 마이그레이션을 추가하면 head와 이 값을 root가 한 번에 맞춘다 |
| `scripts/platform_v2_inventory.py` | 분류 규칙 2줄(`game_build` 마이그레이션, `queued_work` 소스). 감사 manifest는 재생성하지 않았다 | 기준 SHA가 바뀌므로 manifest 재생성은 통합 시점에 한다 |

## 3. 롤백과 모드 전환

- 코드 롤백: 커밋 revert. 0069의 두 열은 nullable이라 코드가 옛 버전이어도 무해하다(옛 코드는 열을 읽지 않는다).
- 원격 → 로컬 전환: 로컬 모드 API도 같은 주기 작업을 돌리므로 대기 중이던 요청을 그대로 소비한다. **non-generation 모드(`runtime_generation=false`)로 바로 내리면 소비자가 없다.** 이 경우 먼저 로컬 generation 모드로 대기분을 비운 뒤 내린다.
- 대기 중 빌드를 버리려면 해당 행을 `failed`로 닫는 방법뿐이다(취소 API는 원래 없다). 이번 변경은 취소 기능을 바꾸지 않았다.

## 4. 검증

- 신규 테스트: `unit_tests/test_creative_v2_asset_queue.py`(20), `unit_tests/test_executor_queued_consumers.py`(4), `tests/test_game_build_queue.py`(15), `tests/test_game_build_handoff_migration.py`(1).
- 프로세스 경계: Creative와 게임 모두 API가 접수한 뒤 **새 Python 프로세스**가 소비해 `succeeded`로 마감하는 테스트가 있다.
- 기존 실패 1건 교정: `unit_tests/test_creative_v2_asset_generation_e2e.py`의 `_LoopBoundRuntime.work()` fake가 FND-01이 추가한 `allow_draining` 인자를 받지 않아 실패하던 것을 fake 시그니처로 맞췄다(제품 코드 결함 아님).
- 변경 뮤테이션 확인: stale 판정에서 `queued` 예외를 빼면 중복 접수 테스트가, 맡기 조건을 풀면 기존 행 보호 테스트가, 소비자가 `running`을 후보로 삼으면 고아 보존 테스트가 실패한다.

## 5. 미검증 / 범위 밖

- 실제 Comfy, ACE-Step, Supertonic, docker 격리 빌드 러너, 운영 실행부는 실행하지 않았다(가짜 생성기, `FakeAdapter`).
- 실행부 프로세스를 SIGTERM으로 내리는 실제 launchd 흐름은 확인하지 않았다. `executor.serve()`는 모킹한 런타임으로 SIGTERM 수신 시 대기 동작만 확인했다.
- 취소 기능과 API/Qwen/TTS 드레인은 다루지 않았다.
