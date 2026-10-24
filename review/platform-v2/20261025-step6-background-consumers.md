# Platform V2 Step 6: NotebookLM·Reverse·수동 색인 실행부 이관

- 작성일: 2026-10-25
- 기준: `cf7fc031fe4fcd81e3e6794eda0095ceb00f6cbb` (`bf2d566` + 계획 문서)
- 브랜치: `feat/platform-v2-background-isolation-20261025`
- 범위: 계획 Step 6의 세 도메인만. TossInvest·Google 로그인·Host GPU 등은 건드리지 않았고 아래 6절에 호출 위치만 남긴다.

## 1. 계약

1. 원격 모드(`generation.current.role == "api"` 이고 `execution_mode == "remote"`)의 API는 입력을 DB에 내구적으로 기록하고 즉시 응답한다. 브라우저·Google·clone·색인을 직접 오래 소유하는 `asyncio.create_task` 는 만들지 않는다.
2. 실행부(`executor` 프로세스)의 소비자가 `queued` 행을 조건부 UPDATE 한 번으로 `running` 으로 가져가 끝까지 돌린다. 한 번 `running` 이 된 행은 다시 가져가지 않는다.
3. 종료 신호가 오면 `stop_claiming()` 으로 새 claim 을 멈추고 `drain()` 이 진행 중 작업의 완주를 기다린다. 남은 `queued` 는 그대로 보존된다. 기다리는 한도(`DRAIN_SECONDS`, 1800초)를 넘기면 각 도메인의 기존 취소 경로로 정리한다.
4. API 재배포·재시작은 실행부가 소유한 작업에 영향이 없다. 반대로 실행부 교체는 위 3번 드레인을 따른다.
5. 로컬 모드(롤백 경로)는 기존대로 접수한 프로세스가 바로 돌리고, 같은 소비자도 함께 떠서 원격 시절에 남은 `queued` 를 이어받는다.

## 2. 도메인별 동작

| 도메인 | API(원격 모드)가 남기는 것 | 소비자 | 중복 부수효과 방지 |
|---|---|---|---|
| NotebookLM 질의·소스 추가 | `notebooklm_jobs` 행(`queued`) + 입력 원본 `spec`(질문·텍스트·업로드 경로). 응답에는 `spec` 을 싣지 않고 끝나면 비운다. 소스 중복 원장(`notebooklm_sources`) 예약은 접수 단계에서 그대로 한다. | `NotebookLMService.pump_queued()` | claim 은 `status='queued' AND cancel_requested=false` 조건부 UPDATE + `FOR UPDATE SKIP LOCKED`. 계정별 동시 실행은 `notebooklm_concurrency` 상한 안에서만 가져온다. 계정 상태 검사·429 쿨다운·blocked 는 기존 `_call` 정책이 그대로 적용된다. |
| Reverse (`git_url`/`local_path`) | `reverse_jobs` 행(`queued`) | `reverse.pump_queued()` | 같은 방식 claim. 동시 실행은 `MAX_CONCURRENT`(2). `reconstruction` 종류는 Run 실행부 경로라 제외. 대상은 claim 때 `normalize_target` 을 다시 통과한다. |
| 수동 색인 (`/api/assistant/sources`, `/reindex`) | `ctx_sources.index_requested_at` | `index_queue.pump()` | 출처 단위 claim(`index_started_at`). 한 번에 한 출처, 주기 색인(`index_all`)도 같은 claim 으로 겹치지 않는다. |

취소: 아무도 가져가지 않은 NotebookLM job 은 API 가 그 자리에서 취소하고 예약·업로드 파일을 정리한다. 이미 실행부가 가져간 job 은 `cancel_requested` 만 남기고, 실행부의 5초 하트비트가 보고 스스로 멈춘다(기존 메커니즘).

## 3. 복구 규칙 (맹목 재실행 없음)

- `running` 행: 실행부가 죽었다면 NotebookLM 은 하트비트가 낡으면 `unknown`, Reverse 는 `updated_at` 이 `STALE_MINUTES` 를 넘기면 `failed`, 색인은 claim 이 2시간 넘으면 다른 실행이 가져갈 수 있다(색인은 sha 기반 멱등). 어느 쪽도 외부 부수효과가 있는 작업을 자동으로 다시 보내지 않는다.
- `queued` 행: 실패로 만들지 않는다. 예전에는 Reverse `queued` 가 30분 지나면 읽을 때 `failed` 로 바뀌었는데, 이제 `running` 만 낡음을 본다.
- `spec` 이 없는 이전 세대의 NotebookLM `queued` 행은 기존처럼 `failed(시작 전 중단)` 로 정리한다. 입력 원본이 없어 이어받을 수 없다.
- 업로드 임시 폴더 24시간 정리는 그대로다. 24시간 넘게 `queued` 로 남은 업로드 job 은 실행 때 파일이 없어 실패한다.

## 4. 통합 hook (다른 파트와 겹치는 파일)

| 파일 | 추가한 것 |
|---|---|
| `src/ttakkari/executor.py` `start_background_services()` | `await background_consumers.start()` 한 줄 |
| `src/ttakkari/executor.py` `serve()` 의 `finally` 맨 앞 | `await background_consumers.stop()` 한 줄. Step 2 의 드레인 루프와 합칠 때는 종료 신호 시점에 `background_consumers.consumers.stop_claiming()` 을 부르고, 다른 드레인과 같은 지점에서 `await background_consumers.consumers.drain()` 을 기다리게 바꾸면 된다. 소비자가 도는 동안에도 실행부 하트비트가 갱신되도록 Step 2 가 순서를 정해야 한다(`확인 필요`). |
| `src/ttakkari/generation_service.py` `start_api_services()` / `lifespan()` | 로컬 모드에서만 `start()`, 종료 때 `stop(drain_seconds=0)`. 원격 모드 API 는 소비자를 띄우지 않는다. |
| `src/ttakkari/main.py` legacy `lifespan` | 로컬 경로와 같은 `start()`/`stop(drain_seconds=0)` |

`deferred()`(원격 모드 API 판정)는 `ttakkari/background_consumers.py` 에 있다. 다른 Step 이 같은 판정을 쓰려면 이 함수를 재사용하면 된다.

## 5. 스키마와 롤백

- 마이그레이션 `0069_background_consumer_queue`: `notebooklm_jobs.spec`(JSONB), `ctx_sources.index_requested_at`, `ctx_sources.index_started_at`. 모두 nullable 추가라 이전 세대 API 와 공존한다. `deploy/generation-contract.json` 의 `schema_revision` 과 `online_migration_from` 을 함께 갱신했다. 다른 Step 이 같은 번호의 마이그레이션을 추가하면 단일 head 검사(`tests/test_migration_single_head.py`)가 깨지므로 통합 때 번호만 맞추면 된다.
- 롤백: 실행부를 끄고 API 를 `execution_mode="local"` 로 돌린다. 로컬 모드의 소비자가 `queued` 를 이어받고, `running` 은 위 3절 규칙으로 정리된다. 코드 자체의 롤백은 이 브랜치 커밋 revert 이며, 추가된 컬럼은 남겨 둬도 이전 코드가 무시한다.

## 6. 이 Step 범위 밖의 API 내 장기 실행 (전수 완료로 오판하지 말 것)

`grep create_task|ensure_future|threading.Thread` 로 확인한, 이번 3개 도메인이 아닌 호출 위치다.

| 위치 | 내용 | 담당 |
|---|---|---|
| `tts/jobs.py:313,559` | TTS 합성 큐 | Step 4 |
| `runs/desk_qwen_service.py:176,179` | Desk Qwen 관리형 서버 | Step 3 |
| `runs/desk_pool.py:115,140,155` | Desk warm pool | Step 3 연관 |
| `api/creative_v2.py:418` | Creative V2 자산 생성 | Step 5 |
| `api/games.py:435,544` | 게임 빌드 | Step 5 |
| `artcraft.py:461` | ArtCraft 작업 task | Step 5 연관, 미배정 |
| `meetings/vectors.py:163` | 회의록 벡터 색인 요청 task | 미배정 |
| `runs/luna_benchmark.py:225` | Luna 벤치마크 | 미배정 |
| `stocks/tossinvest.py:358,374` | TossInvest 연결·동기화 | 미배정 |
| `google_account/login.py:323` | Google 로그인 창 | 미배정 |
| `host_gpu.py:437,729`, `managed_comfy.py:211,229,338` | Host GPU·Comfy 스레드 | Step 5 연관, 미배정 |
| `context/library.py:111` | 라이브러리 ingest 가 요청 안에서 `index_source` 를 직접 await (분리된 task 는 아님). 소비자·주기 색인과 같은 출처를 동시에 건드릴 수 있어 별도 정리가 필요하다 | 미배정 |

## 7. 알려진 제한

- `GET /api/notebooklm/auth/status` 의 `last_error`(마지막 실패 상세)와 `metrics` 는 프로세스 메모리 값이다. 원격 모드에서는 실행부에서 난 오류가 API 응답에 보이지 않는다. job 의 `error`/`error_code` 와 계정의 `last_error_code` 는 DB 에 남는다.
- 소비자는 실행부 안에서 `runtime_poll_seconds`(최대 2초) 간격으로 DB 를 본다. 접수 후 시작까지 최대 2초 지연이 있다.
- `start_repository_analysis` 는 현재 API 라우트에서 호출되지 않는다(테스트·구 기록용). 라이브 경로는 `retry_job` 이다.
