# 따까리 플랫폼 V2 감사 보고서 (Worker A: Runtime / Delivery / Architecture)

- 작성 시각: `2026-10-24T21:40:00+09:00`
- 담당: Worker A (Runtime, Delivery, Architecture 판단)
- 감사 대상 정본 SHA (`origin/main`): `c8f10156845d007ec6496a65d6d8417be19260bd`
- 운영 활성(Live) SHA: `9f82c9c16cd604e81ddf43180b0be2e895ec85cb`
- 배포 감독(Supervisor) 고정 SHA: `e817fc19471bdc48054158cada4a99466f7eaa6f`
- 공유 작업 트리(`/Users/winterholic/development/projects/winterholic-ttakkari/back`) 로컬 HEAD: `607f8bda0df7dbdd458e4f0e54d703ce9edd3def` (`origin/main` 대비 111커밋 뒤처짐, 미커밋 변경 43건 존재)
- 격리 작업 트리: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a` (`audit/platform-v2-a-runtime-delivery-20261024`)
- 기계 판독 증거: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-runtime-delivery-evidence.json`
- 재현 추출기 및 테스트:
  - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/A-extract-runtime-delivery-evidence.py`
  - `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-a/review/platform-v2/20261024/test_a_runtime_delivery_evidence.py`

---

## 1. P0 운영·복구·성능 기준선 실측 결과

운영 상태는 보호 경로(`~/.ttakkari`) 직접 스캔 없이 공식 MCP 도구(`backend_deploy_status`, `ops_list_deploys`, `ops_deploy_log`, `ops_list_issues`, `ops_get_issue`, `execution_capabilities`, `server_resources`, `read_session`)만으로 수집하였다.

### 1.1 활성 릴리스·세대 슬롯·프로세스 상태 (`live` / `measured`)

| 항목 | 실측값 | 근거 도구 및 경로 | 상태 분류 |
| :--- | :--- | :--- | :--- |
| 운영 활성 리비전 | `9f82c9c16cd604e81ddf43180b0be2e895ec85cb` | `backend_deploy_status(4a6e09a8-c49f-4c2f-9a6c-c0d83fe5824a)` | `live` / `measured` |
| 운영 릴리스 경로 | `/Users/winterholic/.ttakkari/deploy/releases/9f82c9c16cd604e81ddf43180b0be2e895ec85cb` | `backend_deploy_status(4a6e09a8)`, `execution_capabilities(host)` | `live` / `measured` |
| 활성 세대 및 슬롯 | `5ef462e5-d7f7-40e7-bf27-1d4bf19e1d91` (Slot `1`, Port `8792`) | `ops_deploy_log(4a6e09a8)` | `live` / `measured` |
| 직전 은퇴 세대 및 슬롯 | `e1eb4ad0-5ce7-415f-b91e-9c0b0eab2ba3` (`4f341c54`, Slot `0`, Port `8791`, `2026-10-23T10:43:06Z` 은퇴) | `backend_deploy_status(4a6e09a8)` | `live` / `measured` |
| 배포 감독(Supervisor) 리비전 | `e817fc19471bdc48054158cada4a99466f7eaa6f` (`/Users/winterholic/.ttakkari/deploy/releases/e817fc19471bdc48054158cada4a99466f7eaa6f/src/ttakkari/deployment.py`) | `ops_get_issue(LOG-68)` 스택트레이스, `docs/work-history/2026-10-23.md` | `live` (`supervisor.json` 원문은 보호 경로라 `확인 필요`) |
| 호스트 주요 프로세스 | `python3.13` (`pid=44104`, `kind=backend`, RSS `374 MiB`), `llama-server` (`pid=44129`, RSS `7.20 GiB`) | `server_resources` (`2026-10-24T12:20:12Z`) | `measured` |
| 호스트 자원 부하 | Apple M5 Max (CPU 18코어 `17.6%`, GPU 40코어 `0.0%`, RAM 64GiB 중 `42.1%`, Swap `31.7/33.3 GiB` = `95.3%`, Disk `77.3%`) | `server_resources` (`2026-10-24T12:20:12Z`) | `measured` |
| 엔진별 실행 능력 | `host`(10-24 09:14), `codex`(10-23 14:48), `antigravity`(10-21 18:29), `muse`(10-21 17:55): `blocked=[]`; `claude`, `qwen-local`: 미측정 | `execution_capabilities` | `measured` (`claude`/`qwen-local`은 `확인 필요`) |
| UI 워커 부작용 대조 | 세션 `6e33860e-3773-48bf-8df5-a3dc2c330a3e`는 `front` 저장소만 수정해 `dbe0c9f042b006e2a627b8e59c295ad14b7a2caf` 푸시 완료. `back` 수정 및 배포 예약 없음 | `read_session(6e33860e-3773-48bf-8df5-a3dc2c330a3e)` | `measured` |

> **중요 발견 (공유 작업 트리 `607f8bda`와 `origin/main` `c8f10156`의 111커밋 Baseline Drift 및 Worker B·C 검증 대기 격리)**:
> 1. `/Users/winterholic/development/projects/winterholic-ttakkari/back` 공유 작업 트리의 로컬 `HEAD`는 `607f8bda0df7dbdd458e4f0e54d703ce9edd3def`에 머물러 있으며, 실제 `origin/main`(`c8f10156845d007ec6496a65d6d8417be19260bd`)은 그보다 **111커밋 앞서 있다** (`253 files changed, 68,266 insertions(+), 1,568 deletions(-)`).
> 2. Worker B(`c1e9cd73-83ef-4240-99c8-809e500bd903`, `back-platform-v2-b` HEAD `86373cedda20e298ad2b82d857352f845ae70980`)는 실제 실행 모델이 Flash였고 `607f8bda` 기준으로 추출하여 `25모듈 / 497 route / 102 tool / 88 table / 62 migration / 212 config / 321 .py / unmatched 0`, `Creative = artcraft_tasks 1 / FK 0`, `collector = 2 tables`, `cycle = 34`를 보고하였다.
> 3. Worker C(`9a3215de-01d4-4efd-b3ff-9e282de9b766`, 공유 `/back` untracked 산출물) 역시 실제 실행 모델이 Flash였고, 요약 `38 routes` vs 스캐너 `36 routes`, 10분류 주장 대비 `runtime/ownership` 누락(dict 9종·`257 files` vs 요약 합 `262`), `main missing security test` / `deploy security duplicate 31`, C 측 `front 5.48s` / `back 1045.97s` 원시로그 대조 등 내부 불일치·미검증 항목이 존재한다.
> 4. 루트 오케스트레이터가 **독립 검증 워커 `Bv = 1d994e50-c900-4f90-9975-1ff082522a35` (`auto/verification`, GPT-6 Luna 예정)** 및 **`Cv = fed2c630-a867-42f1-9574-4a92905de864` (`auto/verification`)**를 배정했으므로, 검증 완료 전까지 B·C의 위 결론은 모두 **`확인 필요`(`verification_pending`)**로 격리하며 확정 모듈 경계나 구현 근거로 채택하지 않는다.
> 5. 실제 `origin/main`(`c8f10156`) 정본은 `607f8bda` 대비 **`src/ttakkari` 파이썬 파일 `+64`개(`321` → `385`개, `71,985` → `94,439`줄), 마이그레이션 `+5`개(`62` → `67`개, `0063`~`0067`), 신규 DB 테이블 `+21`개(`collector_*` 14개 + `kb_*` 3개 + `run_executor_*` 4개)**가 추가되어 있다 (`A-runtime-delivery-evidence.json`의 `baseline_drift_607f8bda_to_c8f10156` 참조). 본 Worker A 감사는 `origin/main`(`c8f10156`) 정본 및 `ops_deploy_log(4a6e09a8)` 원시 로그를 기준으로 수행하였다.

### 1.2 배포 소요 시간 및 실패 이력 통계 (`measured`)

1. **관측 기간 및 분모**:
   - `ops_list_issues(source="deploy", status="all")` 전수 조회 결과: `2026-10-16T08:11:13Z`부터 `2026-10-23T17:19:03Z`까지 총 **34개 배포 실패 이슈 그룹, 누적 56회 실패 이벤트**가 기록되었다.
   - 단계별 분포:
     - `deploy:pytest`: **33개 이슈 그룹 (54회 실패, 전체 배포 실패의 96.4%)**
     - `deploy:archive`: **1개 이슈 그룹 (2회 실패, `LOG-74` `git archive` 실패)**
   - `ops_list_deploys` 상태별 최근 이력: `succeeded` 50건(조회 상한), `failed` 50건(조회 상한), `superseded` 40건, 현재 `draining` 0건, `waiting_slot` 0건.
2. **최근 성공 배포(`4a6e09a8-c49f-4c2f-9a6c-c0d83fe5824a`, SHA `9f82c9c1`) 단계별 실측 시간**:
   - 예약 생성(`created_at`): `2026-10-23T10:16:49.556741+00:00`
   - 최종 은퇴 완료(`updated_at`): `2026-10-23T10:43:06.032849+00:00`
   - **총 벽시계 소요 시간**: **1,576.48초 (26분 16초)**
   - 내부 구간(`ops_deploy_log(4a6e09a8)`):
     - `git archive` + `uv sync --frozen`: `10:16:50Z`부터 `10:17:38Z`까지 약 **48초** (`Installed 215 packages in 25.63s`)
     - `pytest -q --timeout=120 -x` 전체 실행: **`1,045.97초` (17분 25.97초)** (`4473 passed, 8 skipped, 2 xfailed, 5020 subtests passed`, DB selective reset `2,822`회 / `106`개 테이블)
     - 이전 성공 배포 `34ee75c6`(`b2faeb5d`) 역시 `pytest` 단독 **`1,020.57초` (17분 0.57초)** 소요 (`4455 passed`).
3. **현재 `origin/main` 미반영 커밋 4건과 직전 전역 차단 사례 2건**:
   - 운영 활성 리비전(`9f82c9c1`) 이후 `origin/main`에는 4개 커밋(`d78f304`, `827ae35`, `063002a`, `c8f1015`)이 푸시되었으나, 아래 두 번의 배포가 연속 실패하여 운영은 여전히 `9f82c9c1`에 머물러 있다.
   - **차단 사례 1 (`794447f3-8635-443d-b4c5-de975b0e91fb`, SHA `063002a1`, `LOG-194`)**:
     - 변경 내용: Creative Studio 한국어 자막 줄바꿈 보존 및 장면 프레이밍 수정.
     - 결과: `pytest`를 **`985.34초` (16분 25초)** 동안 실행(`3,327 passed`)한 뒤 `unit_tests/test_creative_v2_lease_handoff.py::test_scene_generation_requests_bounded_lease_wait_from_controller` 1건(`assert 349 == 324`, 프롬프트 길이 하드코딩 단정 불일치)으로 배포 전체 실패.
   - **차단 사례 2 (`e15536bc-c7f9-4e1a-89cb-95e2f91192e5`, SHA `c8f10156`, `LOG-195`)**:
     - 변경 내용: Desk 세션 재사용 시 프로젝트·작업 연속성 검사 추가(`PROMPT_VERSION = "harness-v3-topic-boundary"`).
     - 결과: `pytest`를 **`172.23초` (2분 52초)** 실행(`1,112 passed`)한 뒤 `tests/test_desk_qwen_service.py::test_desk_request_goes_to_the_managed_server_of_this_process` 1건(`assert 'harness-v3-topic-boundary' == 'harness-v2'`)으로 배포 전체 실패.

### 1.3 백업 및 복구 리허설 기준선 (`implemented` / 일부 `확인 필요`)

- **구현된 코드 및 절차 (`implemented`)**:
  - DB 백업: `scripts/backup_db.sh` (`asyncpg` `repeatable_read` 읽기 전용 트랜잭션에서 `pg_export_snapshot()`과 테이블별 `SELECT count(*)`를 구하고, 동일 스냅샷으로 `pg_dump -Fc --snapshot` 실행 후 선택적 `openssl enc -aes-256-cbc -pbkdf2 -iter 200000` 암호화 및 `~/.ttakkari-backups/latest.json` 원자적 갱신).
  - DB 복원 리허설: `scripts/restore_rehearsal.sh` (대상 DB명에 `test`·`restore`·`rehearsal` 필수 강제, localhost/loopback PostgreSQL만 허용, `sha256` 검증 후 임시 DB에 `pg_restore` 실행 및 manifest의 모든 테이블 행 수 전수 대조 후 임시 DB 삭제).
  - 데이터 스냅샷: `scripts/snapshot_data.sh` (`*.sock`, `*.pid` 제외 `tar -czf` 및 파일별 `sha256` manifest 생성, `--verify` 모드로 격리 디렉터리 압축 해제 및 해시 전수 대조).
  - 상태 조회: `src/ttakkari/backup_status.py` 및 `GET /api/system/backup-status` (`src/ttakkari/api/system.py:128`).
- **운영 실제 실행 여부 (`확인 필요`)**:
  - 기본 백업 경로 `~/.ttakkari-backups/latest.json` 및 `~/.ttakkari` 하위는 에이전트 보호 영역 밖이며, MCP 도구 중에 `/api/system/backup-status`를 호출하는 도구가 없다. 따라서 현재 호스트에서 마지막 백업·복원 리허설이 언제 성공했는지는 `확인 필요`(`unknown`)로 분류한다.

---

## 2. 런타임·배달 하위 시스템 코드/콜패스 대조 및 상태 분류

각 하위 시스템에 대해 `계획(planned)`, `코드 구현(implemented)`, `테스트(tested)`, `설정 기본값(feature-off)`, `운영 활성(live)`, `실측(measured)`, `미검증(unknown)`을 엄격히 분리하여 대조하였다.

### 2.1 하위 시스템 상태 요약표

| 하위 시스템 | 핵심 코드 및 마이그레이션 경로 | 코드/테스트 상태 | 설정·계약 기본값 | 운영(Live) 상태 |
| :--- | :--- | :--- | :--- | :--- |
| **ADR 0036 독립 RunExecutor** | `src/ttakkari/executor.py`, `alembic/versions/0064_run_executor.py` | `implemented` / `tested` (`unit_tests/test_executor_migration_contract.py`) | `feature-off` (`deploy/generation-contract.json` `run_executor.activation="disabled"`, `Settings.run_executor_mode="local"`) | `feature-off` (`9f82c9c1`에 코드·스키마 포함되었으나 API 세대가 `local` 모드로 직접 실행 중) |
| **ADR 0036 PG 프로세스 그룹 원장 및 펜싱 Reconcile** | `src/ttakkari/executor_reconcile.py`, `src/ttakkari/models.py` (`RunExecutionLease`, `RunProcessGroup`) | `implemented` / `tested` (`unit_tests/test_executor_reconcile.py`) | `feature-off` (`run_executor` 비활성으로 미호출) | `feature-off` (단, `Run.pid` / `Run.process_started_at` 기반 `reconcile_runs`는 `live`) |
| **ADR 0066 ChangeSet / Worktree / MergeQueue** | `src/ttakkari/delivery/{worktree,provenance,diff_guard,repair_policy,validation,merge_queue,cli}.py`, `0062_delivery_change_sets.py` | `implemented` (부분) / `tested` (`tests/delivery/test_*.py`) | `feature-off` (`Settings.delivery_pipeline_enabled=False`) | `feature-off` (수동 CLI만 존재, 상주 통합기 데몬 및 MCP `submit_change` 미노출) |
| **세대 배포 컨트롤러 및 3슬롯 (`8791/8792/8793`)** | `src/ttakkari/generation.py`, `generation_service.py`, `generation_deployment.py`, `deployment.py`, `deploy_gate.py` | `implemented` / `tested` (`tests/test_generation_deployment.py`) | 운영 `deploy_strategy="generations"`, `runtime_generation=True` | `live` / `measured` (Slot 1 `8792` 활성, 감독 프로세스는 `e817fc19`에서 실행 중) |
| **API 세대 주기 작업 (`maintenance_jobs`)** | `src/ttakkari/generation_service.py:248-279` (총 15개 주기 루프) | `implemented` / `tested` | `execution_mode=="local"`일 때 API 세대가 실행 | `live` (현재 `active` API 세대가 15개 루프를 직접 실행) |
| **로컬 TTS 큐 (`tts_jobs`)** | `src/ttakkari/tts/jobs.py`, `src/ttakkari/tts/service.py` | `implemented` (결함 내포) / `tested` | `local_tts_enabled=True` | `live` (원문 프로세스 로컬 메모리 보관 + `draining` 시 `queued` 강제 실패 결함 활성) |
| **Desk Qwen 관리형 서버** | `src/ttakkari/runs/desk_qwen_service.py`, `src/ttakkari/generation_service.py:232` | `implemented` / `tested` (`tests/test_desk_qwen_service.py`) | `desk_qwen_enabled=True`, `desk_qwen_manage=True`, `desk_qwen_base_url=""` | `live` / `measured` (API 세대마다 독립 `llama-server` 자식 기동, 현재 `pid=44129` RSS `7.20 GiB`) |
| **호스트 GPU 배타 리스 (`ResourceLeaseManager`)** | `src/ttakkari/host_resource.py`, `host_gpu.py`, `managed_comfy.py` | `implemented` / `tested` (`unit_tests/test_creative_v2_lease_handoff.py`) | `data_dir/host-gpu/lock` + `data_dir/host-resource/lease.json` | `live` (`9f82c9c1` 반영 완료) |

### 2.2 ADR 0036 독립 Executor·리스·펜싱·프로세스 그룹 원장 정밀 대조

1. **인계 문서와 실제 정본(`origin/main`)의 차이 규명**:
   - `2026-10-21` 작성된 `~/.claude/handoffs/tasks/ttakkari-delivery-executor.md`에는 `run-executor` 브랜치가 `main`에 미병합된 것으로 기록되어 있으나, 실제 Git 이력을 대조한 결과 커밋 `a3b49f6` (`Merge run-executor (d4aa622) into main; renumber executor migration to 0064`)를 통해 **이미 `origin/main` 및 운영 활성 릴리스(`9f82c9c1`)에 병합 완료**되었다.
2. **구현된 아키텍처 및 콜패스**:
   - **스키마(`0064_run_executor.py`)**:
     - `runtime_generations.execution_mode` (`'local'` 기본값)
     - `run_executor_states` (`executor_id`, `revision`, `pid`, `process_started_at`, `state`, `lease_token`, `lease_epoch`, `heartbeat_at`, `lease_expires_at`, `draining_since`)
     - `run_execution_commands` (`command_id`, `run_id`, `session_id`, `action` in `start|cancel|context_reset`, `status` in `pending|claimed|completed|failed`, `idempotency_key` unique)
     - `run_execution_leases` (`run_id` PK, `executor_id`, `lease_token`, `fencing_token` bigint, `lease_expires_at`)
     - `run_process_groups` (`pgid` PK, `run_id` unique, `executor_id`, `fencing_token`, `leader_pid`, `process_started_at`, `state` in `running|stopping|terminated|orphan_fenced`)
   - **펜싱 토큰(Fencing Token) 불변 조건**:
     - `RunManager.claim_execution_lease()` (`src/ttakkari/runs/manager.py:1303-1356`)는 `run_execution_leases` 행을 생성·갱신할 때마다 `fencing_token = coalesce(max_fencing, 0) + 1`로 단조 증가시키고 `Run.execution_fencing_token`에 기록한다.
     - `RunManager._flush_events()` (`src/ttakkari/runs/manager.py:725-777`)는 `RunEvent`를 DB에 기록하기 직전 `SELECT execution_fencing_token FROM runs WHERE id = :id FOR KEY SHARE`를 조회하여, 메모리의 `expected_fence`와 불일치하면 즉시 `StaleExecutionFenceError`를 발생시키고 `session.rollback()`으로 늦은(stale) 실행자의 이벤트·상태 오염을 차단한다. (`FOR KEY SHARE`를 사용하여 `choose_profile`과의 외래키 데드락을 방지한 커밋 `e08ec2f` 반영 확인).
   - **임의 복구 없는 펜싱 고아 회수(`executor_reconcile.py`)**:
     - `reconcile_fenced_orphans()` (`src/ttakkari/executor_reconcile.py:86-226`)는 각 `run_process_groups` 행에 대해 `SET LOCAL lock_timeout = '200ms'`, `SET LOCAL statement_timeout = '1000ms'`를 걸고 `FOR UPDATE NOWAIT`로 잠근다.
     - 리스가 살아있고(`lease_expires_at > now`) 활성 executor와 `lease_token`·`fencing_token`이 일치하면 절대 건드리지 않는다(`skipped_live_lease`).
     - 리스가 만료·부재하거나 펜싱 토큰이 구세대인 경우에도 **자동 재실행(replay/resume)을 절대 수행하지 않으며**, 오직 커널 프로세스 그룹 리더의 `(leader_pid, process_started_at)`이 원장과 정확히 일치할 때만(`_verified_group_alive`) `SIGTERM` 후 유예 시간 뒤 `SIGKILL`로 고아 프로세스 그룹을 종료하거나, 신원 불일치 시 신호 전송 없이 `orphan_fenced`로 격리하고 `Run.status = failed`로 마감한다.
3. **왜 현재 운영에서 꺼져 있는가 (`feature-off`)**:
   - `deploy/generation-contract.json` (`L9-15`) 및 `deploy/run-executor-manifest.json`에서 `"activation": "disabled"`로 고정되어 있고, `src/ttakkari/config.py:109`에서 `run_executor_mode: Literal["local", "remote"] = "local"`이 기본값이다.
   - 또한 `src/ttakkari/generation_deployment.py:390-394`의 `_ensure_executor_if_enabled()`는 `contract.get("run_executor", {}).get("activation") == "enabled"`일 때만 `ensure_executor()`를 호출한다.

### 2.3 잔여 TTS Process-Local Payload 및 API 세대별 Qwen Ownership 전수 조사 (`코드 증명 완료`)

#### (A) 잔여 TTS Process-Local Payload 및 `draining` 시 정상 작업 강제 실패 결함

1. **원문 청크의 프로세스 로컬 메모리 귀속 (`src/ttakkari/tts/jobs.py:42`, `L300-346`)**:
   - `_pending_texts: dict[uuid.UUID, list[str]] = {}` 전역 딕셔너리가 API 프로세스 메모리에만 존재한다.
   - `accept(db, text=..., voice=..., speed=...)` (`jobs.py:300-346`)는 `tts_jobs` 테이블에는 `text_sha256`, `text_chars`, `chunk_count`, `owner_generation_id`만 저장하고, 실제 합성할 문자열 청크 배열 `plan.chunks`는 `_pending_texts[job.id] = list(plan.chunks)`로 **요청을 받은 API 프로세스 메모리**에만 넣은 뒤 `202 Accepted`를 반환한다.
   - 이 때문에 `generation_service.py:227-229`에도 명시되어 있듯, TTS 소비자 워커를 독립 `run-executor`로 옮기면 원문 텍스트가 없어 합성이 불가능하다.
2. **세대 전환(`draining`) 시 발생하는 이중 결함 (`src/ttakkari/tts/jobs.py:153-154`, `178-193`, `service.py:130`)**:
   - **결함 1 (`generation.work("tts")`의 `draining` 거부)**:
     - `tts/service.py:130`의 `budget()` 컨텍스트 매니저는 `async with generation.work("tts") as active:`를 호출한다.
     - 그런데 `src/ttakkari/generation.py:130`의 `GenerationRuntime.enter_work()`는 `if self.state != "active": return False`로 구현되어 있다!
     - 즉, API 슬롯이 `active`일 때 이미 `202 Accepted`로 접수되어 `_pending_texts`에 들어 있는 `queued` 작업이라도, 워커 루프가 차례가 되어 실행하려 할 때 해당 API 슬롯이 `draining`으로 바뀌어 있으면 `enter_work("tts")`가 `False`를 반환해 합성을 시작조차 못 한다.
   - **결함 2 (`_guard_loop`의 `_fail_queued_draining` 강제 실패)**:
     - 더 심각하게, `tts/jobs.py:153-154`의 `_guard_loop()`는 5초마다 `if runtime is not None and runtime.state == "draining": await _fail_queued_draining(runtime.id)`를 실행한다.
     - `_fail_queued_draining()` (`jobs.py:178-193`)은 해당 세대(`owner_generation_id == generation_id`)의 모든 `queued` TTS 작업을 찾아 `_pending_texts.pop(jid, None)`으로 원문을 버리고 **`status="failed"`, `error_code="generation_draining"`, `error_message="서버가 새 버전으로 전환되어 대기 중인 음성 생성을 마쳤습니다. 다시 시도해 주세요."`로 강제 실패**시킨다!
     - 이는 사용자에게 이미 `202 Accepted`로 수락한 비동기 작업을 배포 전환 때문에 임의로 실패시키는 동작으로, 플랫폼 V2 불변 조건("기존 접수·실행 중 작업 보존 또는 안전한 드레인")에 정면 위배된다.

#### (B) API 세대별 Desk Qwen (`llama-server`) 중복 소유 구조

1. **기동 콜패스 (`src/ttakkari/generation_service.py:232`, `src/ttakkari/runs/desk_qwen_service.py:124-213`)**:
   - `generation_service.py:306`의 `lifespan()`은 API 슬롯이 **`standby` 상태로 처음 뜨는 시점**에 무조건 `await start_api_services(runtime.execution_mode)`를 호출하고, 여기서 `await qwen_service.start()` (`generation_service.py:232`)를 호출한다.
   - `DeskQwenService.start()` (`desk_qwen_service.py:124-213`)는 `desk_qwen_enabled=True`(기본값), `desk_qwen_manage=True`(기본값), `desk_qwen_base_url=""`(기본값)일 때 가용 메모리가 `6.0 GiB` 이상이면 빈 loopback 포트를 잡아 **`llama-server` 자식 프로세스(`Qwen3.5-4B-Q4_K_M.gguf`, CPU 6스레드, ctx 8192)를 직접 스폰하고 최대 120초 동안 준비 및 워밍업 추론을 수행**한다.
2. **운영 및 배포 시 문제점**:
   - **문제 1 (세대 기동 지연 및 메모리 중복 점유)**: 현재 운영 중인 `llama-server`(`pid=44129`) 하나가 **RSS `7.20 GiB`**를 점유하고 있으며 호스트 Swap 사용률이 **`95.3%` (`31.7 / 33.3 GiB`)**에 달한다. 세대 배포가 일어날 때마다 새 `standby` 슬롯이 또 하나의 `llama-server`를 띄우므로, 전환 구간(`active` + `draining` + `standby`)에는 동일 모델 프로세스가 2개에서 최대 3개까지 중복 기동되거나 가용 메모리 부족(`desk_qwen_min_free_memory_gb`)으로 새 세대에서 Qwen 기동이 건너뛰어진다.
   - **문제 2 (코드에는 이미 외부 공유 URL 모드가 준비되어 있으나 미사용)**: `DeskQwenService.start()` (`desk_qwen_service.py:127-135`)는 `desk_qwen_base_url`이 설정되어 있으면 자식 프로세스를 띄우지 않고 외부 서버 `/health`만 확인하도록 이미 구현되어 있다. 따라서 Qwen 서버를 세대 밖 단일 호스트/Executor 관리 서비스로 분리하고 API 세대에는 `desk_qwen_base_url`만 주입하면, 세대 전환 시 `llama-server` 재기동 시간(최대 90초)과 7.2GiB 중복 메모리 점유를 즉시 제거할 수 있다.

#### (C) Creative Studio V2 자동 자산 생성의 미추적 태스크 결함 (`추가 발견`)

- `src/ttakkari/api/creative_v2.py:418`의 `POST /api/creative/v2/plans/{pid}/production/generate-assets`는 `_ASSET_GENERATION_TASKS[key] = asyncio.create_task(_run_asset_generation(...))`로 백그라운드 태스크를 띄운다.
- 그러나 이 태스크는 `generation.work(...)`를 획득하지 않으며, `src/ttakkari/generation_service.py:235-245`의 `ready(runtime)`가 검사하는 `pending` 집합(`_background`, `indexing`, `usage_store._tasks`, `_pending.values()`)에도 포함되지 않는다.
- 결과적으로 ComfyUI 장면 생성이나 ACE-Step 음악 생성이 수 분간 돌고 있는 도중에 세대 배포가 발생하면:
  1. 컨트롤러는 해당 API 슬롯의 `active_work == 0`, `finalizers == 0`을 보고 즉시 `SIGTERM`으로 프로세스를 종료시켜 실행 중이던 자산 생성을 증발시키거나,
  2. 설령 다른 작업 덕에 슬롯이 `draining` 상태로 살아있더라도, ComfyUI 직후 `asset_generation_managed.py:478`에서 `tts.service.budget(600)`을 호출할 때 `generation.work("tts")`가 `draining` 상태를 거부하여 수 분간 생성한 ComfyUI 결과물 뒤 음성 단계에서 `Supertonic 실행 예산을 얻지 못했습니다: draining` 오류로 전체 작업이 실패한다.

---

## 3. 임의 복구 없는 Recovery / Drain / Rollback 및 Side-Effect Idempotency 조건

사용자 불변 조건("실행 중 Run 임의 중단 금지", "소유권 미증명 작업 자동 재실행/replay 금지", "exactly-once 가정 금지")을 코드와 운영 경계에서 만족하기 위한 정확한 조건은 다음과 같다.

1. **소유권 증명 없는 자동 재실행(Replay/Resume) 절대 금지**:
   - API 세대 프로세스나 Executor가 재기동할 때 DB에 `status = 'running'`으로 남아 있는 `Run`, `Creative` 자산 생성, `Collector` 작업, `ComfyUI` 작업을 만났을 때:
     - **원칙**: 커널 프로세스 신원 `(pid, process_started_at)` 또는 `(pgid, leader_pid, process_started_at)`과 유효한 `fencing_token` / `ResourceLease`가 살아있는지 먼저 대조한다.
     - **살아있는 소유자가 증명된 경우**: 상태를 건드리지 않고 기존 소유자가 완료하거나 드레인하도록 보존한다(`RunManager.reconcile_runs`의 `pid_alive(run.pid, run.process_started_at)` 경로 및 `executor_reconcile.py`의 `skipped_live_lease` 경로).
     - **소유자 부재(`ABSENT`)가 증명된 경우**: 자동으로 처음부터 다시 돌리지(replay) 않고, `Run.status = 'failed'` (또는 `needs_reconcile` / `interrupted`)와 명확한 실패 사유(`서버 재시작으로 실행이 중단되었습니다`)를 기록한 뒤 펜스를 올려 마감한다. 재개 여부는 오케스트레이터나 사용자가 멱등 키·체크포인트를 확인해 명시적으로 결정한다.
2. **안전한 드레인(Drain)의 2계층 분리 (` Admission Fence ` vs ` Accepted Work Drain `)**:
   - 현재 `GenerationRuntime.enter_work()` (`generation.py:130`)는 **신규 HTTP/SSE 요청 수락 차단**과 **이미 수락된 내부 큐 작업의 실행**을 구분하지 않고 `state != "active"`면 모두 차단하고 있다.
   - **필수 수정 계약**:
     - `enter_admission(kind)`: `state == "active"`일 때만 `True` (신규 HTTP 변이 요청, 신규 작업 수락 게이트).
     - `enter_accepted_work(kind, *, accepted_in_generation_id)`: 이미 `active` 상태에서 `202 Accepted`로 수락되어 해당 세대가 배타 소유한 작업(예: 이미 접수된 `queued` TTS 작업, 실행 중인 Creative V2 자산 생성 파이프라인의 하위 단계)은 세대가 `draining`으로 전환된 뒤에도 실행을 마칠 수 있도록 허용하고, 그 작업 수가 0이 될 때까지 `can_retire()`를 `False`로 유지한다.
3. **Side-Effect Idempotency 및 보상(Compensation) 조건**:
   - **명령 큐**: `run_execution_commands.idempotency_key` (`0064_run_executor.py`) 유니크 제약으로 동일 명령(`start`/`cancel`/`context_reset`)의 중복 삽입을 차단한다.
   - **이벤트 기록**: `(run_id, fencing_token, seq)` 단조 증가 및 `FOR KEY SHARE` 펜스 검사(`manager.py:741`)로 구세대 워커의 중복 이벤트 쓰기를 거부한다.
   - **외부/파일 부수효과**:
     - 파일/미디어 산출물(`Creative Studio`, `TTS cache`, `Collector blob`, `Backup manifest`)은 모두 임시 파일(`*.tmp` / `*.partial.$$`)에 쓰고 `fsync` 후 내용 해시(`sha256`) 검증을 거쳐 `os.replace`로 원자적 반영한다.
     - `ManagedAssetGenerator._run_comfy` (`asset_generation_managed.py:379-404`)처럼 입력 `(plan_id, revision, scene_id, prompt_sha256, seed)` 결정적 `identity` 체크포인트를 남겨, 이미 `succeeded`이고 산출물 `sha256`이 일치하는 단계는 재실행하지 않고 건너뛴다.

---

## 4. 배포 전역 차단(Global Blocking) 구체 경로와 격리 설계

### 4.1 현재 배포 파이프라인이 전역 차단을 일으키는 5단계 메커니즘 (코드 증명)

1. **단일 직렬 컨트롤러 잠금 (`src/ttakkari/deployment.py:424-465`, `generation_deployment.py:265`)**:
   - `Controller.serve()`는 `~/.ttakkari/deploy/controller.lock`을 배타 잠금(`flock`)하고 단일 루프로 돌며, 매 틱마다 `pending_jobs()`에서 아직 전환되지 않은 예약 중 **단 1건(`foreground`)만** 선택해 `backend-restart.lock` 아래에서 실행한다.
2. **누적 SHA 결합 (`Candidate Coupling`, `src/ttakkari/deployment.py:217-269`)**:
   - 모든 모듈이 단일 `main` 브랜치 커밋 SHA로만 배포된다.
   - `JobStore.request()`는 새 예약 SHA가 기존 `requested`/`waiting_idle` 예약 SHA의 후손이고 `equivalent_contracts()`일 때 이전 예약을 `superseded`로 묶어 최신 SHA 하나를 검증한다.
   - 결과적으로 커밋 A(예: Creative 자막 수정 `063002a`)에 단위 테스트 결함 1건이 들어가면, 그 뒤에 커밋 B(Desk 수정 `c8f1015`)나 커밋 C(긴급 버그 수정)를 푸시하더라도 누적 트리 전체에 커밋 A의 결함이 그대로 남는다.
3. **무차별 전체 테스트 게이트 (`src/ttakkari/generation_deployment.py:130-191`)**:
   - `validate_release()`는 변경된 파일이나 모듈과 무관하게 매 배포마다:
     1. `git archive <sha>`로 릴리스 추출 (`~5초`)
     2. `uv sync --frozen`으로 가상환경 구성 (`~40초`)
     3. `pytest -q --timeout=120 -x`로 **전체 테스트 스위트(4,473개 테스트, 5,020개 서브테스트, 약 17분 25초)**를 처음부터 실행한다.
     4. 실패 시 `stepwise_retry_args()`(`deploy_gate.py:92-123`)가 `--stepwise --stepwise-skip`으로 1회 재시도하지만, 결정적 실패(`single_failure` 1건이라도 동일하게 재현)면 즉시 `RuntimeError("Release validation failed")`로 중단한다.
4. **ADR 0066 `delivery/validation.py`의 동일한 한계 (`src/ttakkari/delivery/validation.py:136-184`)**:
   - `project_stage_validator()` 역시 Stage 1에서 `pytest -m contract`만 돌린 뒤 Stage 2(`full`)에서 `pytest -q --timeout=120` 전체 스위트를 그대로 실행하며, 배포 컨트롤러(`generation_deployment.py`)와 증거를 공유하지 않아 병합 시 17분 + 배포 시 17분 이중 실행 구조로 되어 있다.
5. **복구 대기 시 전역 중단 (`Fail-Closed Recovery Gate`, `src/ttakkari/deployment.py:492-495`)**:
   - `any(j["status"] in {"recovery_pending", "rolling_back"} for j in jobs)`이면 모든 후속 후보 전환을 멈춘다. 공유 런타임 상태가 불명확할 때 멈추는 이 fail-closed 안전장치 자체는 올바르며 유지해야 하지만, **모듈 내부 검증 실패가 아예 공유 런타임 전환 시도나 다른 모듈의 변경과 뒤섞이지 않도록 앞단에서 분리**해야 한다.

### 4.2 독립 모듈 실패 격리 및 공유 상태 Fail-Closed 유지 설계

1. **ChangeSet 단계에서의 모듈별 사전 격리 (Pre-Merge Isolation)**:
   - 변경이 `main`에 합쳐지기 전에 ChangeSet 단위로 검증한다. 특정 도메인 모듈(예: `creative`)의 ChangeSet이 검증에 실패하면 해당 ChangeSet만 `failed`/`quarantined` 상태로 격리되고 `main`에 병합되지 않으므로, 다른 모듈(예: `desk`, `collector`, `games`)의 ChangeSet 병합과 배포를 절대 막지 않는다.
2. **검증 증거(Validation Receipt) 재사용을 통한 배포 게이트 단축**:
   - ChangeSet 통합기(또는 사전 검증 게이트)가 후보 트리 SHA에 대해 유효한 `EvidenceCacheKey`와 서명된 검증 영수증(`ValidationReceipt`)을 생성하면, 배포 컨트롤러(`generation_deployment.py`)는 동일한 `EvidenceCacheKey`가 확인될 때 17분짜리 전체 `pytest`를 재실행하지 않고:
     1. 빠른 계약 묶음(`pytest -m contract`, 목표 15초 이내) +
     2. 실제 슬롯 기동 후 **비캐시 실시간 검진(`/api/health`, `/api/ready`, `/api/version`, 모듈 라우트 스모크)**만 수행하고 즉시 전환한다.
3. **공유 상태 불명 시 Fail-Closed 유지**:
   - `alembic/` 마이그레이션 변경, `deploy/generation-contract.json` 변경, `models.py`/`config.py`/`security/`/`generation*`/`executor*` 등 공유 코어 변경, 또는 분류 불능(`unknown`) 경로 변경은 **절대 모듈 스코프로 축소하지 않고 광역 게이트(Broad Gate)로 승격**한다.
   - 슬롯 전환 중 이전 슬롯 신호 실패나 리스 불일치로 `recovery_pending`이 발생하면 기존과 동일하게 fail-closed로 전환을 멈춘다.

---

## 5. Evidence Cache Key 명세 및 실시간 Readiness 재검증 분리

### 5.1 결정적 Evidence Cache Key (`platform-v2-evidence-key-v1`)

테스트 결과 캐시는 거짓 양성(stale pass)을 절대 허용하지 않도록 아래 8개 구성 요소의 정규화된 JSON에 대한 `SHA-256` 해시로 계산한다 (`A-extract-runtime-delivery-evidence.py:compute_evidence_cache_key_preview` 참조):

```json
{
  "schema_version": "platform-v2-evidence-key-v1",
  "module_id": "creative",
  "changed_files": ["src/ttakkari/creative/render.py"],
  "dependency_closure_hashes": {
    "src/ttakkari/config.py": "<sha256>",
    "src/ttakkari/host_resource.py": "<sha256>",
    "src/ttakkari/managed_comfy.py": "<sha256>",
    "src/ttakkari/managed_comfy_receipt.py": "<sha256>",
    "src/ttakkari/models.py": "<sha256>"
  },
  "uv_lock_sha256": "<sha256 of uv.lock>",
  "migration_head": "0067_collector_outbox_retry",
  "generation_contract_sha256": "<sha256 of deploy/generation-contract.json>",
  "suite_selector": "pytest -m contract + tests/test_creative*.py + unit_tests/test_creative*.py",
  "python_version": "3.13"
}
```

- **무효화(Invalidation) 규칙**:
  1. 대상 모듈 소스 파일 또는 대상 모듈 테스트 파일 중 1바이트라도 바뀌면 무효화.
  2. 대상 모듈이 import하는 상위 의존성 클로저(`dependency_closure_hashes`)의 파일 해시가 바뀌면 무효화.
  3. `uv.lock`, `pyproject.toml`, Alembic `migration_head`, `deploy/generation-contract.json`, `deploy/run-executor-manifest.json` 해시가 바뀌면 전체 모듈 캐시 즉시 무효화.
  4. 캐시 유효 기간(TTL): 동일 키라도 최대 `24시간` 초과 시 재실행.

### 5.2 캐시 가능 검증(Cached Validation)과 비캐시 실시간 검진(Live Cutover Probe)의 엄격한 분리

| 구분 | 대상 항목 | 캐시 허용 여부 | 실행 시점 |
| :--- | :--- | :--- | :--- |
| **정적·단위·모듈 통합 검증** | `ruff check`, `pytest -m contract`, 모듈 전용 `unit_tests/` 및 `tests/test_<module>*.py` | **허용 (`EvidenceCacheKey` 일치 시 재사용)** | ChangeSet 검증 시 1회 실행 후 배포 게이트에서 영수증 검증 |
| **광역 보안·마이그레이션 게이트** | `tests/test_security_gate.py`, 마이그레이션 체인 검증 | 코어/보안/스키마 무변경 시에만 캐시 허용 (변경 시 무조건 재실행) | ChangeSet 검증 및 광역 게이트 트리거 시 |
| **비캐시 실시간 검진 (Live Probes)** | 후보 슬롯 프로세스 기동, PostgreSQL 연결(`GET /api/health`), `RuntimeGeneration` 리스너(`GET /api/ready`), 리비전 일치(`GET /api/version`), 외부 프록시 전환 후 라우트 응답, `Executor` 하트비트 | **절대 캐시 금지 (매 배포·전환마다 100% 실시간 실행)** | 배포 컨트롤러가 후보 슬롯/워커를 띄우고 트래픽을 전환하는 순간 |

### 5.3 변경 영향 분석(Change Impact Analysis) 및 광역 게이트(Broad Gate) 승격 규칙

`origin/main`(`c8f10156`)의 `src/ttakkari` 385개 파이썬 파일(94,439줄, 60개 최상위 모듈 단위)에 대해 정적 AST 역의존성(`fan_in`)을 실측한 결과:

1. **고결합 코어 단위 (`fan_in > 5`, 수정 시 무조건 Broad Gate 승격)**:
   - `config` (`fan_in = 34`), `models` (`fan_in = 32`), `db` (`fan_in = 22`), `runs` (`fan_in = 14`), `security` (`fan_in = 8`), `audit` (`fan_in = 8`), `generation` (`fan_in = 7`), `context` (`fan_in = 7`), `artifacts` (`fan_in = 6`), `notifications` (`fan_in = 6`), `orchestrator` (`fan_in = 6`).
2. **4대 공유 계약 및 스키마 경계 (수정 시 무조건 Broad Gate 승격)**:
   - `alembic/versions/*` 및 `alembic/env.py`
   - `deploy/generation-contract.json`, `deploy/run-executor-manifest.json`
   - `pyproject.toml`, `uv.lock`
   - `src/ttakkari/main.py`, `src/ttakkari/deployment.py`, `src/ttakkari/generation_deployment.py`, `src/ttakkari/generation_service.py`, `src/ttakkari/executor.py`, `src/ttakkari/executor_reconcile.py`
3. **저결합 도메인 모듈 (`fan_in <= 5`, 모듈 스코프 검증 가능)**:
   - `creative` (`fan_in = 4`: `api`, `generation_service`, `main`, `orchestrator`에서만 참조; 다른 도메인 모듈의 역참조 0건)
   - `games` (`fan_in = 4`: `api`, `cli`, `main`, `runs`)
   - `notebooklm` (`fan_in = 4`: `api`, `generation_service`, `main`, `orchestrator`)
   - `design_system` (`fan_in = 2`: `api`, `orchestrator`)
   - `reverse` (`fan_in = 2`: `api`, `orchestrator`)
   - `tossinvest` (`fan_in = 2`: `api`, `orchestrator`)
4. **테스트 실행 인프라 병목 핵심 발견 (`tests/conftest.py`의 `autouse=True` 세션 픽스처)**:
   - `tests/conftest.py:189`의 `_schema_and_app` 픽스처가 `scope="session", autouse=True`로 선언되어 있어, `tests/test_contract_marker.py`나 `tests/test_migration_single_head.py`처럼 **DB가 전혀 필요 없는 순수 정적 계약 테스트만 실행해도 무조건 PostgreSQL 연결 및 전체 스키마 초기화를 수행**한다.
   - 본 감사에서 `--noconftest`로 실행한 결과, `test_contract_marker.py` + `test_migration_single_head.py` + `unit_tests/test_executor_*.py` + `test_creative_v2_lease_handoff.py` 총 **39개 테스트가 DB 연결 없이 `5.00초` 만에 통과**하였다.
   - 따라서 P3 공용 기반 구현에서 `contract` 및 순수 정적 검사를 DB `autouse` 픽스처 영향권 밖으로 분리하는 것만으로도 계약 게이트 실행 시간을 수십 초에서 **5초 이내**로 즉시 단축할 수 있다.

---

## 6. 최소 Foundation Change 및 아키텍처 대안 요약

모듈 경계 최종 확정은 Worker B(백엔드/데이터) 및 Worker C(프런트/테스트) 감사 결과와 통합하여 결정하되, 런타임·배달 소스에서 증명된 제약과 이를 해결하는 **최소 공용 기반 변경(Minimum Foundation Change)** 및 **3가지 아키텍처 대안**은 `docs/design/platform-v2/architecture-options.md`에 상세 기술하였다. 핵심 요지는 다음과 같다.

### 6.1 소스에서 증명된 5대 필수 공용 기반 변경 (P3 구현 대상)

1. **TTS 원문 DB 영속화 및 Draining 수락 작업 보호 (`src/ttakkari/tts/jobs.py`, `generation.py`)**:
   - `tts_jobs` 테이블(또는 JSONB 컬럼 `payload_chunks`)에 원문 청크를 영속화하여 프로세스 로컬 `_pending_texts` 의존을 제거한다.
   - `_guard_loop`의 `_fail_queued_draining`을 폐기하고, 이미 `202 Accepted`로 수락된 작업은 `draining` 상태에서도 완료될 때까지 실행을 허용(`enter_accepted_work`)하거나 독립 실행기로 인계한다.
2. **API 세대 백그라운드 태스크 드레인 추적 (`src/ttakkari/api/creative_v2.py`, `generation_service.py`)**:
   - `_ASSET_GENERATION_TASKS` 등 API 요청이 스폰한 비동기 작업을 `generation.work` / `ready()`의 `finalizers`에 등록하거나 독립 워커 큐로 이관하여 세대 은퇴 시 작업 증발을 막는다.
3. **Desk Qwen 서버의 세대 외부 단일 소유권 분리 (`src/ttakkari/runs/desk_qwen_service.py`, `config.py`)**:
   - API 세대마다 `llama-server`(RSS 7.2GiB)를 자식 프로세스로 띄우는 구조를 멈추고, 호스트/Executor 단일 관리 프로세스(`desk_qwen_base_url` 연결)로 분리하여 3슬롯 전환 시 메모리 폭발(현재 Swap 95.3%)과 90초 기동 지연을 제거한다.
4. **ADR 0036 `RunExecutor` 단계적 활성화 (`deploy/generation-contract.json`, `deploy/run-executor-manifest.json`)**:
   - 이미 구현·병합된 `0064_run_executor`와 `executor.py`를 활성화하여, AI CLI Run 프로세스 그룹(`run_process_groups`), 주기 유지보수 작업(`maintenance_jobs`), 미디어/문서 워커(`preview`, `stt`, `tts`, `creative-render`)의 생명주기를 API HTTP 세대(8791/8792/8793) 재기동과 완전히 분리한다.
5. **ChangeSet 영향 기반 선택 검증 + EvidenceCacheKey 배포 게이트 연동 (`src/ttakkari/delivery/validation.py`, `generation_deployment.py`, `tests/conftest.py`)**:
   - 정적 계약 테스트의 불필요한 DB 세션 픽스처 결합을 분리하고, 저결합 도메인 모듈 변경 시 `contract` + 해당 모듈 스위트만 실행해 `ValidationReceipt`(`EvidenceCacheKey` 포함)를 발행하며, 배포 게이트가 이를 검증해 17분 25초짜리 전체 `pytest` 재실행을 생략(공유 코어/스키마/보안/미분류 변경 시에만 Fail-Closed 광역 게이트 실행)하도록 연결한다.

### 6.2 3가지 아키텍처 대안 비교 요약 (상세는 `docs/design/platform-v2/architecture-options.md`)

- **대안 1 (보수적 모놀리스 유지 + 선택적 테스트 게이트 + ADR 0036 Executor 활성화)**:
  - 프로세스 추가 없이 `Core API (3슬롯)` + `RunExecutor (단일 상주)` 2종 프로세스 구조만 사용.
  - 장점: 운영 복잡도 최소, 메모리 추가 부담 없음.
  - 한계: 도메인 모듈 코드만 고쳐도 결국 `Core API` 세대 슬롯(8791/8792/8793)을 교체해야 하므로, 사용자 핵심 목표인 **"모듈 단독 릴리스 시 Core API 무재배포(`unnecessary Core deploy = 0`)"와 "9대 실증 시나리오 #4(Core 재배포 0회)"를 만족하지 못한다.**
- **대안 2 (권장: Core Control Plane + ADR 0036 RunExecutor + 버전드 모듈 플러그인/워커 번들 핫스왑)**:
  - 4개 논리 평면(`Control`, `Execution`, `Domain`, `Delivery`)을 유지하되, 물리 프로세스를 4개로 쪼개지 않고:
    1. `Core API (8791/8792/8793)`는 인증·세션·라우팅·공용 레지스트리 및 얇은 모듈 프록시/디스패처만 담당하고,
    2. `RunExecutor`가 장기 실행 Run과 공용 큐를 전담하며,
    3. 독립 전환 대상 도메인 모듈(1차: `Creative Studio`)은 **불변 모듈 릴리스 아카이브(`~/.ttakkari/deploy/module-releases/<module>/<sha>/`)**로 빌드·검증되어, 모듈 전용 워커/서브서비스(또는 버전드 어댑터 프로세스) 단위로 독립 교체·드레인·롤백된다.
  - 장점: **모듈 단독 수정 시 Core API(8791/8792/8793) 재배포 0회 달성**, 모듈 검증 시간 17분 25초 → 1~2분 이내 단축, 모듈 장애·롤백이 Core API 및 다른 모듈과 완전 격리, 호스트 메모리 절약(신규 브로커/인프라 0개, 기존 PostgreSQL/파일 잠금 재사용).
- **대안 3 (도메인별 상주 마이크로서비스 + 각각 멀티슬롯 포트 할당)**:
  - 각 도메인(`Creative`, `Collector`, `Games`, `KB` 등)을 모두 독립 FastAPI 상주 데몬과 슬롯 포트로 분리.
  - 기각 사유: 현재 Mac Studio 호스트 Swap 사용률이 `95.3%`(`31.7/33.3 GiB`)인 상태에서 상주 파이썬/모델 프로세스를 다수로 늘리면 즉각적인 메모리 고갈을 유발하며, "모든 모듈에 3슬롯을 복제하지 말 것"이라는 불변 조건에 위배된다.
