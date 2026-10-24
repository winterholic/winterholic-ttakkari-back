# Platform V2 리팩토링 재수립 계획: 실행부 격리 중심 전환

- 작성일: 2026-10-25
- 대상 저장소: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration`
- 기준 브랜치 및 커밋: `fix/platform-v2-verified-integration-20261024` (`bf2d566`)
- 비교 참고 브랜치: `fix/orchestrator-report-merge-queue-20261025` (`91fe130`)

## 0. 고정 원칙과 목표

### 0.1 핵심 목표 3가지
1. 기존에 되던 기능이 그대로 동작한다.
2. 이미 돌아가고 있는 작업이 배포나 재시작 때문에 중간에 죽지 않는다.
3. 특정 파트를 고쳤을 때 그 파트만 빠르게 배포할 수 있다.

### 0.2 아키텍처 고정 결정
1. 사용자는 1명이다. 다중 사용자 동시성이나 대규모 트래픽을 가정한 설계를 넣지 않는다.
2. MSA로 쪼개지 않는다. DB, 파일 저장소, 로컬 모델, GPU는 그대로 공유하되 각 파트의 **실행부만 격리**한다.
3. 공유 자원에서 실제로 부딪히는 지점만 큐나 짧은 트랜잭션으로 순서를 잡는다.
4. 세대 간 작업 소유권 이전, 다중 세대 펜싱(`fencing_token`), 복잡한 리스(`lease`) 조율을 더 늘리지 않는다.
5. **API 프로세스는 요청 접수와 조회만 맡는다.** 실행부는 API 배포와 별개로 작업을 끝까지 처리한 뒤 새 버전으로 교체된다.

---

## 1. 실행부 목록 (API 프로세스 내 장시간·백그라운드 실행 전수 조사)

현재 API 프로세스([`src/ttakkari/main.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/main.py#L199-L263), [`src/ttakkari/generation_service.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_service.py#L51-L343)) 안에서 기동되거나 요청 핸들러가 직접 스폰하는 장시간·백그라운드 실행은 총 10개 영역이다.

### 1.1 요약 표

| # | 실행부 영역 | 코드 근거 (`파일:라인`) | (a) 사용하는 공유 자원 | (b) 충돌 지점 | (c) 순서 제어 필요 여부 |
|---|---|---|---|---|---|
| 1 | 에이전트 Run 실행 및 오케스트레이터 보고 병합 | `src/ttakkari/runs/manager.py:189-206`, `:280-325`, `:345-366`, `:1522-1628`, `:1834-1862`, `:2339-2382`<br>`src/ttakkari/generation_service.py:68-75`, `:93`, `:307` | PostgreSQL(`runs`, `run_events`, `sessions`, `runtime_leases`, `runtime_commands`, `runtime_process_groups`, `engine_accounts`), 워크스페이스 파일트리, Run outbox(`data/outbox/<run_id>`), `home/deploy` 잠금, CLI 계정 프로필 디렉터리 | 1) 동일 일반/워커 `ChatSession` 내 동시 실행 시 대화 문맥 꼬임<br>2) 하위 워커 다수가 동시에 끝날 때 부모 세션의 `queued` report Run 병합 경쟁<br>3) API가 직접 자식 CLI를 들고 있으면 장기 Run 종료 전까지 구세대 API 슬롯 퇴역 불가 | **필요**: 일반/워커 세션당 Run 1개 순차 실행(`ChatSession` `FOR UPDATE`), 부모 report 병합 시 부모 `ChatSession` `FOR UPDATE` 직렬화 (`desk` 세션만 무상태 병렬 허용) |
| 2 | Desk Warm Pool 및 로컬 Qwen 관리형 서버 | `src/ttakkari/runs/desk_qwen_service.py:167-292`<br>`src/ttakkari/runs/desk_pool.py:53-156`<br>`src/ttakkari/runs/luna_benchmark.py:225-245`<br>`src/ttakkari/generation_service.py:88`, `:232`, `:325`<br>`src/ttakkari/main.py:213`, `:248` | 호스트 RAM/Swap(`llama-server` 약 7.2GiB RSS), CPU 스레드, `home/qwen/*.json` PID 파일, `home/desk` 디렉터리, CLI 프로필 | `desk_qwen_manage=True`일 때 API 세대 전환 중 신·구 슬롯이 각각 `llama-server`를 띄우면 메모리/Swap(95% 육박) 고갈 위험 | **필요**: `llama-server`는 호스트 단일 인스턴스 1개만 유지하고 `--parallel 1`로 순차 처리. `desk_pool`은 활성 실행부만 보유하고 종료 시 즉시 정리 |
| 3 | 로컬 TTS 합성 큐 | `src/ttakkari/tts/jobs.py:150-176`, `:207-230`, `:282-345`, `:520-560`<br>`src/ttakkari/tts/service.py:90-118`<br>`src/ttakkari/generation_service.py:229`, `:318`<br>`src/ttakkari/main.py:222`, `:238` | PostgreSQL(`tts_jobs`, `tts_cache`), 파일(`data_dir/tts-cache`, `home/runtime/work-tts.lock`), Supertonic 추론 자식 프로세스(CPU/RAM) | 1) 로컬 Whisper STT와 CPU/메모리 경합<br>2) Creative V2 자산 생성(`asset_generation_managed.py:478`)이 큐 밖에서 직접 `tts.service.budget(600)`을 호출해 `work-tts.lock` 경합<br>3) API 세대별 `owner_id` 귀속 시 드레인 지연 및 고아 잡 발생 | **필요**: 단일 워커가 `tts_jobs`(`status='queued'`)를 FIFO로 1건씩 직렬 소비하고, `work-tts.lock` 및 `defer_to_stt` 정책으로 STT와 실행 순서 조율 |
| 4 | 로컬 Whisper STT 전사 및 회의록 자동 분석 | `src/ttakkari/meetings/local_stt/jobs.py:137-216`, `:236-250`, `:253-311`<br>`src/ttakkari/generation_service.py:226`<br>`src/ttakkari/executor.py:108` | PostgreSQL(`local_stt_jobs`, `local_stt_segments`, `meetings`), 오디오 Artifact 파일, Whisper large-v3 모델, `home/runtime/work-stt.lock`, CPU/RAM | 1) 다중 전사 동시 실행 시 대형 모델 중복 로드로 메모리 부족 위험<br>2) 전사 완료 후 자동 분석 Run 생성과 수동 분석 요청 간 `Meeting` 행 경합<br>3) TTS와 CPU 경합 | **필요**: 단일 직렬 큐(`LocalSttJob.status == "running"` 존재 시 대기) + `Meeting` 행 `FOR UPDATE` 잠금으로 분석 트리거 직렬화 |
| 5 | 임베딩·문서 색인·미리보기 변환 | `src/ttakkari/meetings/vectors.py:36-37`, `:92-165`<br>`src/ttakkari/kb_retrieval.py:58-99`<br>`src/ttakkari/context/indexer.py:131-212`<br>`src/ttakkari/api/assistant.py:464-488`<br>`src/ttakkari/artifacts/preview.py:55-133`<br>`src/ttakkari/generation_service.py:140-179`, `:212-225` | PostgreSQL(`meeting_chunks`, `kb_passage_vectors`, `ctx_sources`, `ctx_documents`, `ctx_chunks`, `artifacts`), 로컬 E5 임베딩 모델, LibreOffice(`soffice`), `artifact_store/<id>/preview` | 1) `api/assistant.py:485`의 요청 시점 `create_task` 색인과 주기 `_retention`의 `index_all`이 동시에 같은 `CtxSource`를 수정할 때 충돌(`_indexing` set은 프로세스 로컬)<br>2) 회의록 임베딩과 KB 임베딩이 동시에 E5 워커를 띄울 때 CPU 경합 | **필요**: 백그라운드 색인·임베딩·미리보기 변환을 단일 실행부 루프로 일원화하여 순차 실행 |
| 6 | ComfyUI · Creative Studio (V1/V2) · ArtCraft · Host GPU | `src/ttakkari/api/creative_v2.py:397-425`<br>`src/ttakkari/creative/v2/asset_generation_managed.py:370-600`<br>`src/ttakkari/managed_comfy.py:111-250`<br>`src/ttakkari/creative/v2/run_extraction.py:59-106`<br>`src/ttakkari/creative/render.py:335-609`<br>`src/ttakkari/artcraft.py:419-463`<br>`src/ttakkari/host_gpu.py:343-449`, `:725` | 호스트 단일 Metal GPU 및 통합 메모리(`ResourceLeaseManager`: `data_dir/host-gpu/lock`, `data_dir/host-resource/lease.json`), TTS 예산, 파일 매니페스트(`production.json`, `<scene>-managed-comfy.json`, `project.json`, `renderer.lock`, `artcraft/state.json`), 포트 `8199` ComfyUI | 1) `_ASSET_GENERATION_TASKS`와 `managed_comfy` 스레드가 API 프로세스 메모리에서 30분에서 60분간 돌아 API 슬롯 종료 시 파이프라인 중단 및 GPU 리스 고아(`needs_reconcile`) 발생<br>2) Host GPU `llama-server`, ComfyUI Wan 2.2, ACE-Step 1.5 간 단일 GPU 경합 | **필요**: GPU는 `ResourceLeaseManager` 파일 잠금으로 상호배제 유지. Creative 자산 생성·렌더링·ArtCraft 실행은 API 스레드에서 빼내어 단일 실행부 워커가 순차 처리 |
| 7 | 게임 스튜디오 빌드 | `src/ttakkari/api/games.py:417-438`, `:543-546`<br>`src/ttakkari/games/builds.py:136-151`, `:189-250`<br>`src/ttakkari/runs/manager.py:1821` | PostgreSQL(`game_projects`, `game_builds`, `artifacts`), 게임 Git 워크스페이스(`ws.root_path`), 빌드 번들 저장소(`data_dir/game-builds/<game_id>/<build_id>`) | 1) 빌드 도중 워크스페이스 파일 변경 시 지문(`fp_before != fp_after`) 불일치 실패<br>2) API의 `create_task(_run_build)`가 API 종료로 끊기면 `building` 상태가 남아 타임아웃까지 후속 빌드가 차단됨 | **필요**: `GameProject` `FOR UPDATE`로 게임당 1개 빌드 직렬화 유지, 실행은 API `create_task` 대신 실행부 워커가 수행 |
| 8 | 수집기 플랫폼 및 KB 배달 | `src/ttakkari/collector/runner.py:173-192`<br>`src/ttakkari/kb_delivery.py:48-140`<br>`src/ttakkari/generation_service.py:187-210`, `:258-261`<br>`src/ttakkari/executor.py:109` | PostgreSQL(`collector_*` 16개 테이블, `kb_*` 테이블), 외부 대상 호스트(속도 제한 `collector_host_state`, 예산 `collector_budget_*`), `home/runtime/work-collector-*.lock` | 다수 워커가 동시에 돌면 호스트별 속도 제한 및 예산 차감 경합 발생 | **필요**: 단일 실행부 워커에서 `consume_once`와 `deliver_once`를 순차 배치로 실행 |
| 9 | 스케줄러 및 주기 유지보수 루프 | `src/ttakkari/schedules/service.py:128-180`, `:335-348`<br>`src/ttakkari/generation_service.py:94-99`, `:140-185`, `:248-280`<br>`src/ttakkari/executor.py:109` | PostgreSQL(`scheduled_actions`, 알림, 날씨, 계정 사용량 샘플, `system_resource_samples`, `ops_issues`), 외부 CLI 쿼터 프로브, `home/runtime/work-<name>.lock` | 여러 API 세대가 교대할 때 주기 루프가 겹쳐 실행되면 중복 알림·중복 프로브 발생 가능 | **필요**: API 프로세스에서는 주기 루프를 전부 끄고 단일 실행부 프로세스 1개에서만 실행 |
| 10 | 기타 요청 기동형 백그라운드 작업 (`NotebookLM`, `Reverse`, `TossInvest`, `Google Login`) | `src/ttakkari/notebooklm/service.py:523`, `:840`<br>`src/ttakkari/reverse.py:1371`, `:1407`, `:1772`<br>`src/ttakkari/stocks/tossinvest.py:358`, `:551`<br>`src/ttakkari/google_account/login.py:171`, `:323` | PostgreSQL(`notebooklm_jobs`, `reverse_jobs`, `tossinvest_sync_runs`, `google_accounts`), Chrome 프로필 디렉터리(`SingletonLock`), `reverse` 클론 작업 폴더 | 1) API 핸들러가 `asyncio.create_task`로 헤드리스 브라우저나 `git clone` 분석을 직접 돌려 API 재시작 시 작업 중단<br>2) 동일 구글/토스 Chrome 프로필 동시 접근 시 `SingletonLock` 충돌 | **필요**: `NotebookLMJob`과 `ReverseJob`은 DB `queued` 적재 후 단일 실행부 워커가 계정별·세마포어별로 순차 소비 |

### 1.2 세부 동작 및 코드 근거 보충

1. **에이전트 Run (`runs/manager.py`)**
   - [`RunManager.start()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/runs/manager.py#L189-L206)가 프로세스 내 `asyncio.create_task`로 `run-{run_id}`를 띄우고, [`_execute()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/runs/manager.py#L1522-L1628)가 임시 PostgreSQL 테스트 클러스터(`RunTestDatabase`)와 CLI 서브프로세스(`claude`, `codex`, `antigravity`, `qwen-local` 등)를 구동한다.
   - [`RunManager._enqueue_report()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/runs/manager.py#L2339-L2382)는 하위 워커 완료 시 부모 세션에 `[Worker Report]` Run을 생성하거나 기존 `queued` report Run에 병합한다.
2. **이미 분리 배선이 준비된 영역 (`executor.py`)**
   - [`src/ttakkari/executor.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/executor.py#L86-L133)의 [`start_background_services()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/executor.py#L106-L110)와 [`GenerationRuntime.coordinate()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_service.py#L304-L310)에는 이미 `execution_mode == "remote"`일 때 API 프로세스에서 `RunManager`, `preview`, `local_stt`, `maintenance_jobs`(`schedules`, `collector-consume`, `collector-control`, `creative-render`, `vectors`, `kb-vectors`, `retention`, `reminders`, `weather`, `usage`, `ops-logs` 등)를 끄고 독립 `executor` 프로세스에서 돌리는 구조가 들어가 있다.
   - 반면 **로컬 Qwen 서버(`desk_qwen_service`)**, **로컬 TTS 워커(`tts/jobs.py`)**, **Creative V2 자동 자산 생성(`api/creative_v2.py`)**, **게임 빌드(`api/games.py`)**, **NotebookLM/Reverse/수동 색인(`notebooklm`, `reverse`, `api/assistant.py`)**은 아직 API 프로세스에 묶여 있다.

---

## 2. FND-01 변경 분류 (`917ea4b..bf2d566`)

`FND-01`에서 구현된 7개 항목을 **(a) 독립 실행부에서도 필요한 것**과 **(b) API 세대 직접 실행 때문에 생긴 임시 보완이라 실행부 분리 후 제거할 것**으로 분류한다.

| # | FND-01 항목 | 관련 코드 (`파일:라인`) | 분류 | 판단 근거 및 후속 처리 |
|---|---|---|---|---|
| 1 | **TTS `chunks_json` 영속화** | `alembic/versions/0068_tts_payload_chunks.py:19-30`<br>`src/ttakkari/tts_models.py:26`<br>`src/ttakkari/tts/jobs.py:217`, `:291-293` | **(a) 독립 실행부에서도 필요 (유지)** | API 프로세스가 요청만 접수(`POST /api/tts/jobs`)하고 별도 실행부 프로세스가 합성을 수행하려면, 문장 분할된 텍스트 청크가 API 프로세스 메모리(`_pending_texts`)가 아니라 DB(`tts_jobs.chunks_json`)에 저장되어 있어야 한다. 실행부 분리(Step 4)의 필수 선행 스키마다. |
| 2 | **드레인 중 accepted work 보존** | `src/ttakkari/generation.py:107-123`, `:140-156`<br>`src/ttakkari/tts/jobs.py:151-152`, `:524-525`, `:553`<br>`src/ttakkari/tts/service.py:93-116` | **(b) 임시 보완 (실행부 분리 후 제거)** | API 슬롯이 직접 TTS를 돌리다가 `draining` 상태가 되었을 때, 이미 받은 `queued` TTS 잡을 스스로 실패시키지 않으려고 `RuntimeGeneration.work(..., allow_draining=True)` 등 세대 상태 분기를 추가한 것이다. TTS를 독립 실행부로 옮기면 API `draining`과 TTS 실행은 무관해지므로 제거한다. |
| 3 | **`can_retire` fail-closed** | `src/ttakkari/generation.py:124-137`, `:162-170`<br>(`_has_pending_tts_jobs`, `_accepted_work`) | **(b) 임시 보완 (실행부 분리 후 제거)** | 구세대 API 슬롯이 자신이 수락한 TTS(`TtsJob.owner_id == self.id`)를 끝내기 전에 `SIGTERM`으로 내려가는 것을 막기 위해 API 퇴역 조건(`can_retire`)에 TTS DB 조회를 넣은 것이다. 실행부가 분리되면 API 슬롯의 `can_retire()`는 진행 중인 HTTP 요청(`http_inflight == 0`)만 확인하면 된다. |
| 4 | **고아 running TTS 660초 reap** | `src/ttakkari/tts/jobs.py:42`, `:399-447`, `:461-473`, `:538` | **(a) 독립 실행부에서도 필요 (단순화 유지)** | 독립 실행부 프로세스라도 `SIGKILL`이나 전원 장애로 죽으면 DB에 `status='running'`인 TTS 잡이 남을 수 있다. 다만 다중 API 세대(`RuntimeGeneration`) 조인 검사는 걷어내고, 단일 실행부 기동 시 잔류 `running` 정리 및 합성 제한시간(`local_tts_timeout_seconds + 60s = 660s`) 초과 실패 처리만 남긴다. |
| 5 | **activate 전 구세대 queued TTS 30초 대기** | `src/ttakkari/generation_deployment.py:209-257`, `:1199-1200`<br>(`_wait_previous_tts_queue_empty`, `_hold_for_previous_tts`) | **(b) 임시 보완 (실행부 분리 후 제거)** | `0068` 이전 구세대 API가 `draining` 전환 즉시 `queued` TTS를 실패 처리하던 결함을 우회하려고 배포 컨트롤러가 `activate()` 직전 최대 30초 대기하는 임시 코드다. TTS 실행부가 API에서 분리되면 API 세대 교체 시 기다릴 이유가 전혀 없으므로 제거한다. |
| 6 | **`resume_interrupted` 무-replay** | `src/ttakkari/config.py:62` (`auto_resume_interrupted = False`)<br>`src/ttakkari/runs/manager.py:1967-1982` | **(a) 독립 실행부에서도 필요 (유지)** | 실행부가 재시작될 때 비정상 중단(`interrupted`)된 Run을 사용자 확인 없이 처음부터 자동 재실행(blind replay)하면 외부 부수효과가 중복 발생한다. 독립 실행부에서도 자동 재실행을 막는 안전 스위치로 계속 유지해야 한다. |
| 7 | **`recover_interrupted` 3상태 분기** | `src/ttakkari/runs/manager.py:297-358`<br>(`_inspect_run_process`: `alive` / `absent` / `unknown`) | **(a) 독립 실행부에서도 필요 (유지)** | 실행부 재기동 시 이전 실행부가 띄운 하위 CLI 프로세스가 살아있는지(`alive`), 확실히 없는지(`absent`), 권한·OS 오류로 알 수 없는지(`unknown`)를 구분해, 확실히 죽은 프로세스의 Run만 `interrupted`로 마감하고 살아있는 프로세스를 오폭하지 않는 기본 안전장치다. |

---

## 3. 기존 FND-02부터 FND-05 및 P4부터 P6 처리 판정

사용자의 고정 결정(단일 사용자, MSA 및 모듈 번들 핫스왑 배제, 세대 간 소유권·펜싱 복잡도 확장 금지, API는 접수·조회만 담당하고 실행부만 격리)에 따라 기존 계획을 판정한다.

| 기존 단계 | 원래 계획 내용 | 판정 | 판정 이유 및 변경 내용 |
|---|---|---|---|
| **FND-02** | Creative V2 `_ASSET_GENERATION_TASKS`를 API `RuntimeGeneration`의 `accepted_work`/`finalizers`에 등록 + 체크포인트 `os.fsync`/`sha256` + DB 태스크별 `fencing_token` 도입 | **축소** | API 세대의 드레인 카운터에 Creative 60분 작업을 등록하는 것은 API가 계속 무거운 실행을 직접 맡는다는 잘못된 전제다. 세대 드레인 연동과 다중 워커용 DB `fencing_token` 도입은 삭제하고, **체크포인트 파일 원자적 쓰기(`os.fsync`)만 유지**한 채 실행 자체를 독립 워커 큐로 이관(Step 5)한다. |
| **FND-03** | Desk Qwen `llama-server`를 API 세대별 자식 프로세스에서 분리하여 단일 인스턴스로 운용 | **유지** | API 세대마다 7.2GiB 규모의 `llama-server`를 중복 스폰할 가능성을 차단하고 API를 가볍게 유지하기 위해 그대로 필요하다(Step 3). |
| **FND-04** | ADR 0036 `RunExecutor` 활성화 + `0064`의 4개 PL/pgSQL 펜싱 트리거·`runtime_process_groups`·세대 간 동적 소유권 이전 실증 | **축소** | 독립 `executor` 프로세스가 Run·STT·주기 작업을 전담하고 API는 `execution_mode="remote"`로 접수·조회만 하는 구조는 핵심이므로 유지한다. 단, **세대 간 lease 탈취나 다중 세대 펜싱 프로토콜 확장은 중단**하고, 단일 `executor`가 진행 중 작업을 끝낸 뒤 재기동하는 단순한 드레인 교체(Step 2)로 축소한다. |
| **FND-05** | `EvidenceCacheKey v2`(소스 바이트 스냅샷 바인딩) + HMAC `ValidationReceipt v2` + 동적 import 클로저 스캐너 + `ChangeSet` CAS 파이프라인 강제 연동 | **축소** | 단일 사용자 저장소에서 소스 스냅샷 해시와 HMAC 영수증 체인을 구축하는 것은 실제 개발 속도 대비 관리 비용이 과도하다. 복잡한 영수증·클로저 인프라는 걷어내고, 테스트 픽스처(`tests/conftest.py`) 경량화와 변경 파트 대상 빠른 `pytest` 실행만 유지한다. |
| **P4** | Creative Studio를 첫 모듈로 삼아 `~/.ttakkari/deploy/module-releases/<module_id>/<sha>`, `active.json`, 동적 import 디스패처, `pinned_module_revision` 멀티버전 워커 구축 | **삭제** | 단일 백엔드 안에 버전별 모듈 릴리스 디렉터리와 동적 로더를 두는 것은 사실상 자체 MSA/플러그인 컨테이너를 만드는 과잉 설계다. 또한 `artcraft_tasks.session_id` FK, `ResourceLeaseManager`, `tts` 등 공유 자원 결합과도 맞지 않는다. 모듈 번들 핫스왑 대신 Creative 실행부를 API 밖의 워커로 분리(Step 5)한다. |
| **P5** | Game Studio, Collector, Meetings STT 등 나머지 도메인을 버전드 모듈 번들 아키텍처로 순차 분리 | **축소** | 도메인별 코드 번들 분리는 폐기한다. 대신 API 프로세스 안에 남아 있는 장시간 실행(`tts`, `creative_v2`, `games/builds`, `notebooklm`, `reverse`, `assistant` 색인)을 실행부 워커 소비 구조로 옮기는 작업(Step 4부터 Step 6)으로 전환한다. |
| **P6** | 9대 모듈 핫스왑 시나리오(`active.json` 3초 롤백 등) 실증 및 전후 벤치마크·거버넌스 문서화 | **축소** | 폐기된 모듈 번들 시나리오를 제거하고, 3대 핵심 목표(1. 기존 기능 동작, 2. API 배포 중 실행 작업 무중단, 3. 실행부 드레인 교체)를 검증하는 통합 테스트와 배포 운영 절차 확인으로 축소한다. |

---

## 4. 최소 전환 순서 (작업자 1명 × 1일 이내 단위, 총 6단계)

실행부를 하나씩 API 프로세스에서 떼어내는 순서다. 각 단계는 1명이 하루 안에 끝내고 단독 검증·배포할 수 있는 크기다.

### 단계별 의존성 및 병렬 진행 가능 구조

- **Step 1** (기준선 통합, 0.5일) 완료 후:
  - **Step 2** (`executor` 활성화 및 드레인 교체, 1일)
  - **Step 3** (로컬 Qwen 서버 단일화, 0.5일, Step 2와 병렬 진행 가능)
- **Step 2** 완료 후:
  - **Step 4** (로컬 TTS 실행부 이관 및 FND-01 임시 코드 제거, 1일, Step 3·Step 5·Step 6과 병렬 진행 가능)
  - **Step 5** (Creative V2 자산 생성 및 Game 빌드 실행부 이관, 1일, Step 3·Step 4·Step 6과 병렬 진행 가능)
  - **Step 6** (NotebookLM·Reverse·수동 색인 실행부 이관, 1일, Step 3·Step 4·Step 5와 병렬 진행 가능)

---

### Step 1: 오케스트레이터 Report 병합 경쟁 수정 통합 및 1차 배포 기준선 확립 (0.5일)

- **변경 범위(파일·모듈)**:
  - [`src/ttakkari/runs/manager.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/runs/manager.py#L2339-L2382): [`_enqueue_report()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/runs/manager.py#L2339)에서 부모 [`ChatSession`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/models.py#L43) 행을 `with_for_update()`로 잠그고, `manager.runtime is None` 조건 때문에 세대 런타임에서 `queued` report Run에 병합하지 못하던 버그(`fix/orchestrator-report-merge-queue-20261025`의 `91fe130`)를 현재 브랜치(`bf2d566`)에 통합한다.
  - [`tests/conftest.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/tests/conftest.py#L1-L72), `unit_tests/test_orchestrator_report_merge.py`: 단위 테스트 픽스처 분리 및 병합 경쟁 테스트 추가.
- **실행 중 작업 보호 방법**:
  - 하위 워커 완료 후 부모 세션에 보고를 적재할 때 짧은 DB 트랜잭션(`SELECT ... FOR UPDATE`)으로 순서만 맞추며, 실행 중인 워커 프로세스에는 영향을 주지 않는다.
  - `FND-01`의 TTS 드레인 유예와 `auto_resume_interrupted=False`가 전환기 배포 중 작업 손실과 중복 실행을 방어한다.
- **롤백 방법**:
  - `91fe130` 커밋 revert. (`0068_tts_payload_chunks` 마이그레이션은 nullable 컬럼 추가만 수행하므로 하위 호환된다.)
- **완료 기준과 검증 테스트**:
  - `uv run pytest -q unit_tests/test_orchestrator_report_merge.py unit_tests/test_fnd01_foundation.py tests/test_migration_single_head.py` 통과.
- **병렬 진행 가능 여부**:
  - 모든 후속 단계의 공통 기준선이므로 가장 먼저 수행한다.

---

### Step 2: 독립 실행기(`executor`) 전환 및 실행기 완주 후 교체(Drain & Restart) 구현 (1일)

- **변경 범위(파일·모듈)**:
  - [`src/ttakkari/executor.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/executor.py#L86-L157): [`serve()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/executor.py#L136) 종료 시 단순 `shutdown()` 호출 대신, 신규 작업 claim을 먼저 멈추고(`stop.set()`), 현재 실행 중인 `runs.manager._handles`, `local_stt`, `preview`, 주기 작업 1회차가 끝날 때까지 대기한 뒤 프로세스를 종료하는 드레인 루프 구현.
  - [`src/ttakkari/generation_service.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_service.py#L68-L100), [`src/ttakkari/generation_deployment.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_deployment.py#L1185-L1240), [`deploy/generation-contract.json`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/deploy/generation-contract.json): API 슬롯은 `execution_mode="remote"`로 기동하여 요청 접수·조회·SSE 중계만 수행하고, 에이전트 Run·STT·미리보기·스케줄러·수집기·렌더 스캔은 단일 `executor` 프로세스가 전담하도록 활성화.
- **실행 중 작업 보호 방법**:
  - **API 배포 시**: `executor` 프로세스는 재시작되지 않으므로 실행 중인 에이전트 Run, STT, 수집기 작업이 중단 없이 그대로 돈다.
  - **실행부(`executor`) 코드 변경 배포 시**: `executor`에 종료 신호(`SIGTERM`)를 보내면 새 작업 수락만 멈추고 진행 중인 작업을 끝까지 마친 뒤 프로세스가 내려가며, launchd가 즉시 새 리비전의 `executor`를 기동한다.
- **롤백 방법**:
  - `ttakkari executor --disable` (또는 `deploy/generation-contract.json`의 `run_executor.activation`을 `"disabled"`로 복원) 후 API를 `execution_mode="local"`로 전환.
- **완료 기준과 검증 테스트**:
  - API 세대 슬롯이 교체되는 동안 실행 중인 Run 서브프로세스 PID가 끊기지 않고 완료됨을 검증.
  - `executor` 프로세스에 종료 신호 전달 시 활성 Run 완료 후 정상 종료되는지 검증 (`unit_tests/test_run_executor_unit.py`, `tests/test_run_executor.py`).
- **병렬 진행 가능 여부**:
  - Step 3(로컬 Qwen 서버 분리)과 병렬 진행 가능.

---

### Step 3: 로컬 Qwen 서버(`desk_qwen_service`)를 API 세대별 자식에서 단일 실행부/외부 소유로 분리 (0.5일)

- **변경 범위(파일·모듈)**:
  - [`src/ttakkari/generation_service.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_service.py#L232-L233) (`start_background_services` 및 `shutdown`의 `qwen_service.start()` / `stop()`), [`src/ttakkari/main.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/main.py#L213), [`src/ttakkari/executor.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/executor.py#L106-L132), [`src/ttakkari/runs/desk_qwen_service.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/runs/desk_qwen_service.py#L167-L230).
  - API 프로세스(`execution_mode="remote"`)에서는 `qwen_service.start()`로 `llama-server` 자식을 직접 스폰하지 않고 상태 조회 및 HTTP 추론 요청만 보내며, 단일 `executor` 프로세스 또는 호스트 고정 `desk_qwen_base_url`(운영 환경 설정값은 배포 시 `확인 필요`)만 `llama-server` 1개를 관리하도록 변경한다.
- **실행 중 작업 보호 방법**:
  - API 슬롯이 새로 뜨거나 내려갈 때 `llama-server` 프로세스를 건드리지 않으므로, 진행 중인 Qwen 로컬 추론이 끊기지 않고 7.2GiB 모델 중복 로딩으로 인한 호스트 Swap 압박도 발생하지 않는다.
- **롤백 방법**:
  - `execution_mode == "local"`일 때 기존대로 API가 `qwen_service.start()`를 호출하는 분기로 즉시 복귀.
- **완료 기준과 검증 테스트**:
  - API 세대 교체 전후 `llama-server` PID가 1개로 유지되고 추가 스폰이 없음을 검증 (`tests/test_desk_qwen_service.py`).
- **병렬 진행 가능 여부**:
  - Step 2, Step 4, Step 5, Step 6과 병렬 진행 가능.

---

### Step 4: 로컬 TTS 워커를 실행부(`executor`)로 이관 및 FND-01 임시 세대 드레인 코드 제거 (1일)

- **변경 범위(파일·모듈)**:
  - [`src/ttakkari/tts/jobs.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/tts/jobs.py#L207-L246), [`src/ttakkari/generation_service.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_service.py#L227-L230), [`src/ttakkari/executor.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/executor.py#L106-L110):
    - `FND-01`에서 `tts_jobs.chunks_json`이 DB에 저장되었으므로 [`generation_service.py:227`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_service.py#L227) 주석("TTS payloads live in the accepting process")의 제약이 이미 해소되었다.
    - API가 `execution_mode="remote"`일 때는 `tts_jobs.start()` 워커 루프를 띄우지 않고 DB에 `TtsJob(status="queued", chunks_json=...)` 적재만 수행한다.
    - `executor.py`에서 단일 `tts_jobs.start()` 워커를 기동하고, API 세대 ID(`owner_id`) 구분 없이 `status='queued'`인 TTS 작업을 FIFO 순서로 소비한다.
  - 임시 코드 정리:
    - [`src/ttakkari/generation.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation.py#L107-L170)의 `_has_pending_tts_jobs()`, `enter_accepted_work()` 제거.
    - [`src/ttakkari/generation_deployment.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/generation_deployment.py#L209-L257)의 `_wait_previous_tts_queue_empty()`, `_hold_for_previous_tts()` 제거.
- **실행 중 작업 보호 방법**:
  - API 배포 시 TTS 워커(`executor`)는 멈추지 않고 합성을 계속한다.
  - `executor` 재기동 시 현재 합성 중인 1건(`run_claimed`)을 완료한 뒤 종료하며, 대기 중인 `queued` 잡은 `chunks_json`에서 원문을 읽어 새 `executor`가 이어서 처리한다.
- **롤백 방법**:
  - `execution_mode == "local"`일 때 API 프로세스에서 `tts_jobs.start()`를 기동하는 분기 유지.
- **완료 기준과 검증 테스트**:
  - `remote` 모드 API에서 `POST /api/tts/jobs` 호출 직후 API 슬롯의 `can_retire()`가 즉시 `True`가 되고, 별도 `executor` 루프가 `chunks_json`으로 오디오 합성을 완료하는지 검증 (`tests/test_tts_queue.py`, `tests/test_tts_api.py`).
- **병렬 진행 가능 여부**:
  - Step 2 이후 Step 3, Step 5, Step 6과 병렬 진행 가능.

---

### Step 5: Creative V2 자동 자산 생성 및 Game 빌드를 API 인메모리 태스크에서 실행부 큐로 이관 (1일)

- **변경 범위(파일·모듈)**:
  - **Creative V2**:
    - [`src/ttakkari/api/creative_v2.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/api/creative_v2.py#L397-L425): `POST /plans/{pid}/production/generate-assets` 핸들러가 API 프로세스 안에서 `_ASSET_GENERATION_TASKS[pid] = asyncio.create_task(...)`를 직접 띄우는 대신, `production.json`의 `asset_generation.status`를 `"queued"`로 기록하고 즉시 응답한다.
    - [`src/ttakkari/creative/v2/asset_generation_managed.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/creative/v2/asset_generation_managed.py#L345-L363): [`_write_checkpoint()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/creative/v2/asset_generation_managed.py#L345)에 파일 디스크립터 `os.fsync()`를 추가하여 체크포인트 파일 손상을 방지하고, 실행부 워커가 `queued` 상태의 플랜을 스캔해 순차 실행하도록 연결한다.
  - **Game Studio 빌드**:
    - [`src/ttakkari/api/games.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/api/games.py#L417-L438) (`create_game_build`, `rollback_game_build`): `prepare_build()`로 DB에 `GameBuild(status="building")`을 커밋한 뒤 API에서 `asyncio.create_task(_run_build(plan))`를 띄우지 않고, 실행부(`executor`)의 빌드 소비 루프가 미완료 빌드를 감지해 [`execute_build()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/games/builds.py#L189-L250)를 실행하도록 변경한다.
- **실행 중 작업 보호 방법**:
  - ComfyUI 비디오 생성(최대 60분), ACE-Step 음악 생성(최대 30분), Supertonic 보이스 합성, Vite 게임 빌드가 모두 실행부 프로세스에서 돌기 때문에 API를 재배포해도 중단되지 않는다.
  - GPU는 기존의 파일 기반 [`ResourceLeaseManager`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/host_resource.py#L121-L260)로 순서가 제어되고, 게임 빌드는 [`GameProject`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/games/builds.py#L137) `FOR UPDATE` 잠금으로 프로젝트별 직렬화가 유지된다.
- **롤백 방법**:
  - `execution_mode == "local"`일 때 기존 API 내 `asyncio.create_task` 실행 경로로 복귀.
- **완료 기준과 검증 테스트**:
  - 자산 생성 및 게임 빌드 요청 직후 API를 재시작해도 실행부 워커가 작업을 끝까지 완주하여 `succeeded`로 마감하는지 검증 (`unit_tests/test_creative_v2_lease_handoff.py`, `tests/test_creative_studio.py`).
- **병렬 진행 가능 여부**:
  - Step 2 이후 Step 3, Step 4, Step 6과 병렬 진행 가능.

---

### Step 6: 잔여 요청 기동형 백그라운드 태스크(`NotebookLM`, `Reverse`, `assistant` 수동 색인)의 실행부 이관 (1일)

- **변경 범위(파일·모듈)**:
  - [`src/ttakkari/notebooklm/service.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/notebooklm/service.py#L523) (`add_source` L523, `start_query` L840): API 핸들러는 `NotebookLMJob(status="queued")` 행만 커밋하고, `self._tasks` 생성은 실행부 워커 루프에서 소비하도록 이동.
  - [`src/ttakkari/reverse.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/reverse.py#L1371) (`start_repository_analysis` L1371, `retry_job` L1407): API는 `ReverseJob(status="queued")`만 기록하고, 실행부가 [`resume_waiting_reverse_jobs()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/reverse.py#L1760-L1775) 패턴으로 큐를 소비하도록 일원화.
  - [`src/ttakkari/api/assistant.py`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/api/assistant.py#L464-L488) (`_start_index`): API 프로세스 내 `_tasks` 대신 DB `CtxSource` 상태 표시 후 실행부의 색인 루프가 [`context.indexer.index_source()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/context/indexer.py#L131)를 순차 실행하도록 변경.
- **실행 중 작업 보호 방법**:
  - 헤드리스 브라우저 조작(`NotebookLM`), 외부 저장소 클론 및 분석(`Reverse`), 문서 색인이 모두 단일 실행부에서 돌아 API 배포와 분리되며, Chrome 프로필 잠금(`SingletonLock`)이나 `CtxDocument` 색인 트랜잭션의 프로세스 간 경합이 사라진다.
- **롤백 방법**:
  - `execution_mode == "local"`일 때 API 내부 `create_task` 즉시 실행 경로로 복귀.
- **완료 기준과 검증 테스트**:
  - API가 `queued` 응답만 반환한 뒤 실행부 소비자가 NotebookLM 잡, Reverse 잡, Source 색인을 완료 처리하는지 단위 테스트 검증.
  - 이 단계까지 마치면 API 프로세스에는 실시간 스트리밍 중계(`remote_desktop` WebSocket, SSE 구독) 외에 장시간 백그라운드 실행 태스크가 0개가 된다.
- **병렬 진행 가능 여부**:
  - Step 2 이후 Step 3, Step 4, Step 5와 병렬 진행 가능.

---

## 5. 첫 배포 경로 권고

### 5.1 권고 결론
**현재 `FND-01`(`bf2d566`)과 오케스트레이터 report 병합 수정(`fix/orchestrator-report-merge-queue-20261025`, `91fe130`)을 먼저 병합하여 1차 배포(Step 1)한 뒤, 그 위에서 실행부 분리(Step 2부터 Step 6)로 진행할 것을 권고한다.**

### 5.2 근거 4가지
1. **실행부 분리와 무관한 런타임 경쟁 조건 및 배포 게이트 결함의 즉시 해소**
   - `91fe130`이 고친 [`_enqueue_report()`](file:///Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-integration/src/ttakkari/runs/manager.py#L2339-L2382) 버그(`manager.runtime is None` 조건 때문에 세대 런타임에서 `queued` report Run 병합이 작동하지 않던 문제와 부모 `ChatSession` `FOR UPDATE` 잠금 누락)는 실행부를 분리하더라도 동일하게 필요한 DB 동시성 수정이다.
   - 또한 `bf2d566`에는 기존 `origin/main` 배포를 막던 프롬프트 버전 정합성(`LOG-195`)과 `recover_interrupted` 3상태 안전 분기, `auto_resume_interrupted=False`가 이미 포함되어 있어 지금 당장 운영 안정성을 높인다.
2. **TTS 실행부 분리(Step 4)를 위한 무중단 Expand 스키마(`0068`) 선행 반영**
   - Step 4에서 API가 TTS 텍스트를 DB에 적재하고 독립 실행부가 이를 읽으려면 `0068_tts_payload_chunks`(`tts_jobs.chunks_json`) 마이그레이션이 DB에 먼저 적용되어 있어야 한다. `FND-01`을 1차로 배포해 두면 Step 4에서는 DB 마이그레이션 없이 코드 스위치만으로 TTS 워커를 분리할 수 있다.
3. **실행부 분리 완료 전 전환기 배포의 작업 보호 안전망 확보**
   - Step 2부터 Step 4를 적용하는 배포 과정 자체에서도 기존 API 세대의 드레인이 일어난다. 이미 작성·검증이 끝난 `FND-01`의 TTS 드레인 보호와 구세대 큐 대기를 1차로 올려두면, 전환기 배포 도중 진행 중인 TTS나 Run이 유실되지 않는다(Step 4 완료 시점에 임시 드레인 코드는 제거).
4. **변경 단위 최소화와 명확한 롤백 기준선 확보**
   - 이미 단위·마이그레이션 테스트가 통과된 `bf2d566` + `91fe130`을 먼저 배포해 깨끗한 기준선을 고정한 다음, Step 2(`executor` 활성화 및 드레인 교체)를 별도 배포로 올리는 편이 문제 발생 시 원인을 좁히고 롤백하기에 훨씬 유리하다.

---

## 6. 코드에서 확인하지 못한 가정 (`확인 필요` 목록)

1. **실시간 운영 슬롯 및 외부 감독 프로세스 리비전 (`확인 필요`)**:
   - 현재 순간 호스트에서 실제 트래픽을 받고 있는 세대 슬롯 번호와 `~/.ttakkari/runtime/generations/supervisor.json`의 실시간 리비전은 보호 경로(`~/.ttakkari`) 밖에 있어 이 문서 작성 시점에 직접 파일을 읽지 않았다. Step 1 배포 요청 시점에 `backend_deploy_status`로 `확인 필요`.
2. **로컬 Qwen 서버(`llama-server`)의 상주 관리 주체 설정값 (`확인 필요`)**:
   - Step 3에서 `llama-server` 단일 인스턴스를 `executor` 프로세스의 관리형 자식으로 띄울지, 아니면 호스트 고정 URL(`desk_qwen_manage=False`, `desk_qwen_base_url` 지정)로 띄울지는 운영 환경 설정(`.env`)의 `TTAKKARI_DESK_QWEN_MANAGE` 현재 값 `확인 필요`. 어느 쪽이든 API 프로세스에서 `qwen_service.start()`를 빼는 변경 범위는 동일하다.
