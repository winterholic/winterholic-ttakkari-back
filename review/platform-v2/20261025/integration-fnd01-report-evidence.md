# FND-01 + report 병합 교정 통합 후보 증거

- 통합 브랜치: `integration/platform-v2-fnd01-report-20261025`
- 원 후보: FND-01 `bf2d566243feab5845fe384d44c9579e61ebdec4`, report `a371677b7ea273bff07c0242ad8ac80d73928c09`(둘 다 `git ls-remote` 확인). 두 후보 커밋 ancestry를 `--no-ff` 병합으로 보존.
- `origin/main`이 작업 중 `fb74602` → `17212d9` → `e4c810d`로 움직여 마지막에 `e4c810d`를 병합했다.

## FND 독립검증 86d63945 FAIL 교정

### 1) 회귀 5건 (모두 테스트만 수정, 제품 코드는 새 계약 유지)
기준 `917ea4b`에서는 통과, FND 후보에서 실패하던 것이다. ALIVE kill과 무인자 자동 replay로 되돌리지 않았다.
- `tests/test_api_runs.py::test_recover_interrupted_marks_running_runs`: 이미 종료된(ABSENT) agent-like 프로세스의 Run만 interrupted로 마감.
- `…::test_recover_does_not_kill_unrelated_process_with_reused_pid`: 살아 있는 PID의 Run은 `recover_interrupted()==0`, status `running`, 프로세스 생존.
- `…::test_only_server_shutdown_interruption_is_queued_for_resume`, `…::test_restart_resume_uses_saved_engine_session_and_progress`: `resume_interrupted()`가 기본 0이고 Run이 interrupted로 남는 것을 먼저 assert, 그다음 `force=True`로만 재큐잉.
- `unit_tests/test_creative_v2_asset_generation_e2e.py::_LoopBoundRuntime.work`: `allow_draining` 키워드를 받도록 바꾸고 서버 루프 검증은 그대로 유지.
- ALIVE/ABSENT/UNKNOWN 분기는 `unit_tests/test_fnd01_foundation.py`의 결정적 fixture가 계속 검증한다(호스트 프로세스 열거에 의존하지 않음).

### 2) 첫 혼합 세대 전환의 TTS 경로
verifier가 지적한 순서: 구세대 POST가 active 확인 → DB 조회 대기 → 전환 대기가 queued 0 관측 → activate → 구 POST 커밋 → 구 guard가 draining으로 queued를 실패 처리.

- 재현(측정, 모델): `tests/test_tts_first_transition.py::test_race_loses_accepted_job_when_only_queue_is_checked_before_activate`. 격리 PostgreSQL에서 917ea4b `tts/jobs.py`의 accept/`_guard_loop` 알고리즘을 옮겨 적은 구세대 모델로 돌렸다. 결과 `failed / generation_draining`, 원문은 DB에 있으나 작업은 실패. 실제 구세대 바이너리를 띄운 측정이 아니다(코드 추론 + 모델 재현).
- 결함 원인: 기존 30초 대기는 큐 0건 관측만으로 통과했고 timeout/오류는 경고만 남기고 activate 했다.
- 닫는 방법(구세대 코드 변경 없음): `GenerationController.drain_previous_tts`가 activate 전에
  1. 프록시에서 새 `POST /api/tts/jobs`를 `503 Retry-After: 5`(`generation_not_active`)로 거절한다. 원문은 어디에도 남지 않는다. 같은 업스트림을 유지하고 영속하지 않는다.
  2. 구세대 `/api/ready`의 `http_inflight`(917에도 존재)가 0이 될 때까지 기다린 뒤,
  3. 구세대 queued TTS가 0건임을 확인해야만 activate 한다.
  구세대는 active인 동안 접수된 job을 끝까지 처리하므로 원문과 완주가 보존된다. 증명하지 못하면(120초 초과, 조회 오류, 구세대 확인 불가) activate 하지 않고 `tts-drain` 단계 실패로 롤백하며 접수 경로를 다시 연다. 경고만 남기고 진행하지 않는다. 성공 경로에서는 `switch(candidate)`가 열린 구성을 실어 접수를 다시 연다.
- 새 접수는 전환 구간 동안 명시적 재시도 거절로 정의했다. 성공 응답 뒤 유실되는 경로는 위 순서에서 생기지 않는다.
- 변이 확인: `http_inflight` 대기를 제거하면 `test_drain_waits_for_inflight_post_…`와 `…[inflight]`가 실패한다(변경 전 코드로 복원 후 9 passed).
- 실제 Caddy 측정(격리 포트 28787/20291, 업스트림은 로컬 http.server): 열림 `POST /api/tts/jobs`=200 → 닫힘 503, `Retry-After: 5`, 본문 `{"detail":{"code":"generation_not_active",…}}`, `GET /jobs/x`·`POST /speak`·`/api/ready`는 영향 없음(200/200/404) → 재개방 200. 운영 프록시(8787/20191)는 건드리지 않았다.
- 제거: `_wait_previous_tts_queue_empty`, `_hold_for_previous_tts`와 `job["warnings"]` 기록. 이 대기는 구세대 race를 닫지 못했다.

#### 남는 한계 (정확히)
- 프록시를 이미 통과했지만 구세대 미들웨어에 닿지 않은 요청(로컬 루프백 수 ms)은 첫 관측 전 `poll`(0.5s) 대기로만 줄인다. 증명하지는 못한다.
- 닫는 구성은 영속하지 않아, 닫힌 동안 프록시가 재시작되면 접수가 열린 상태로 돌아간다(그 구간 한정).
- 프록시를 거치지 않고 구세대 포트로 직접 POST하는 클라이언트는 막지 못한다(알려진 호출자 없음, 미확인).
- 컨트롤러가 닫은 뒤 강제 종료되면 같은 job의 재처리/롤백이 `switch`로 열 때까지 TTS POST가 503이다.
- 실제 운영 중인 구세대 리비전은 확인하지 못했다(작업 지시문의 917ea4b는 미검증 전제).
- 구세대가 이 후보보다 새로운 코드(FND 포함)이면 이 단계는 불필요하지만 해롭지 않다(최대 약 0.5초 대기와 짧은 503).

### 3) main 병합 중 발견한 잠금 순서 충돌
main(`bff631b`, 큐 취소와 claim 직렬화)은 `claim`에서 `Run FOR UPDATE`를 먼저, report 교정(a371677)은 `ChatSession → Run` 순서를 쓴다. 텍스트 충돌은 없었지만 그대로면 `claim`(Run→Session)과 report enqueue(Session→Run)가 교착할 수 있다. `generation.py` claim을 `ChatSession → Run`으로 통일했다. Run은 잠금 없이 읽어 세션만 찾고, 세션 잠금 뒤에 `FOR UPDATE + populate_existing`로 다시 읽어 커밋된 취소와 병합을 반영한다. main의 `test_claim_waits_for_pending_run_cancellation`과 a371 실DB 경쟁 2건이 함께 통과한다.

## 실행 결과 (`PYTHONPATH=src:.` + `../back/.venv/bin/pytest`, uv 미사용)
- 최종 통합 헤드 기준 관련 묶음(TTS, generation, deployment, deploy_gate, bootstrap, processes, orchestrator, planning, route inventory, run_executor, api_runs, failed_runs, migration single head, report 실DB race, TTS 첫 전환 + `unit_tests` 전체): `2071 passed, 1 skipped, 5014 subtests passed in 356.75s`
- 전체 `tests/` 중 위 묶음에 없는 파일은 이번에 돌리지 않았다. 배포 컨트롤러의 전체 게이트가 돌린다.
- `alembic heads`: `0068_tts_payload_chunks` 단일. ruff: 변경 .py 전체 통과.
- 환경 메모: `unit_tests/test_generation_hardening.py::test_db_create_schema_record_failures_carry_their_stage`는 단독 실행 시 `origin/main`에서도 실패한다(asyncpg `password authentication failed for user "winterholic"`, DB 자격증명 의존). 전체 묶음 안에서는 통과했다. 환경 의존으로 분류했고 수정하지 않았다.
- 실패하던 `test_platform_v2_inventory`(`manage_work` unmatched)는 `e4c810d`에서 사라졌다. 이번에 직접 고치지 않았다.

## 미검증 / 범위 밖
- 실제 구세대 바이너리로 띄운 혼합 세대 전환 측정(미실행, 모델과 코드 추론만).
- 운영 배포, main push, `request_backend_deploy`: 수행하지 않았다.
- 독립 실행부 분리(Step2)·Qwen(Step3)은 중복 구현하지 않았다.
