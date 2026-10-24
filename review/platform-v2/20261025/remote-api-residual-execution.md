# 플랫폼 V2 원격 API 잔여 실행 경로 처리 보고서

**작성일**: 2026-10-25  
**대상 브랜치**: `integration/platform-v2-execution-all-20261025`  
**대상 위치**: `/Users/winterholic/development/projects/winterholic-ttakkari/back-platform-v2-execution-all-20261025`  
**근거 문서**: `review/platform-v2/20261025/full-gate-8503c4f.md` § 원격 API에 남은 실행 경로

---

## 1. 개요 및 분석

플랫폼 V2 통합 후보에서 remote 모드 API 프로세스에 남아 있던 2개의 백그라운드 실행 경로에 대해 배포·drain·재시작 시 실제 동작을 코드로 분석하고, 적합한 처리 전략을 선택 및 구현했다.

### 1.1 대상 1: ArtCraft (`/remote-check`, `/smoke`, `/apps/{app}/gui`)
- **코드 경로**:
  - `src/ttakkari/api/artcraft.py:49-60, 69-78`: 각 엔드포인트에서 `artcraft.start_*` 호출 후 202 Accepted 반환.
  - `src/ttakkari/artcraft.py:461`: `_start()` 내부에서 메모리 `JOBS`에 작업 등록 후 `asyncio.get_running_loop().create_task(run())`으로 백그라운드 태스크 실행.
- **배포·drain·재시작 시 관측된 문제**:
  - **작업 유실 및 사용자 증상**: 작업 상태와 태스크 핸들이 프로세스 메모리 `JOBS` dict에만 유지된다. API 배포로 세대가 전환되거나 재시작되면 `JOBS` 기록이 증발하여 클라이언트가 작업 ID로 상태 조회(`GET /api/artcraft/jobs/{job_id}`) 시 404 `unknown_job`("작업 기록이 없습니다. 서버가 다시 시작되면 기록이 지워집니다.") 오류를 수신한다.
  - **drain 중 신규 요청 수락**: drain 중인 구세대가 신규 `/smoke` 등의 요청을 계속 수락하여 곧 종료될 프로세스에서 작업을 시작했다가 즉시 중단되는 현상 발생.
  - **외부 프로세스 고아화 위험**: `_exec`의 `asyncio.create_subprocess_exec`는 `CancelledError` 시 `killpg`를 호출하지만, `start_gui`로 시작된 GUI 앱이나 화면 조작 명령은 중간 취소 시 제어 포트만 열린 채 화면 창이 남거나 불완전한 상태로 고아화될 수 있다.

### 1.2 대상 2: Google 계정 로그인 (`/api/google-accounts/{account_id}/login`)
- **코드 경로**:
  - `src/ttakkari/api/google_accounts.py:72-75`: `login_start()` 호출 후 202 Accepted 반환.
  - `src/ttakkari/google_account/login.py:323`: `LoginManager.start()`에서 실제 Chrome 창을 띄우고 `asyncio.create_task(self._run(...))` 실행.
- **배포·drain·재시작 시 관측된 문제**:
  - **사용자 조작 단절**: 사용자가 Mac 화면에 뜬 Chrome 창에서 직접 계정 정보와 2차 인증을 입력하는 대화형 흐름이다. 입력 도중 API 프로세스가 종료되면 태스크 취소로 Chrome 창이 갑자기 닫힌다.
  - **외부 프로세스 고아화**: 급작스러운 프로세스 종료 시 Chrome 서브프로세스가 사용자 프로필 락을 쥔 채 잔존하여, 이후 로그인 시도 시 `profile_chrome_pids` 검사에서 409 Busy("이전 Google 로그인 창이 남아 있습니다.")를 유발한다.
  - **세션 불일치**: 구세대에서 로그인 창이 열린 상태로 신세대로 트래픽이 절체되면, 사용자가 UI에서 '로그인 완료'(`POST /login/complete`)를 눌러도 신세대의 메모리에는 로그인 상태가 없어 409 Busy("진행 중인 로그인 창이 없습니다.") 오류가 발생한다.

---

## 2. 전략 선택 및 결정 근거

| 대상 | 선택 전략 | 결정 근거 |
|---|---|---|
| **ArtCraft** | **(b) drain 거절 + 대기** | 1. 작업 성격이 진단·제어성 단기 작업(`remote-check`: 25s, `gui`: 15~60s, `smoke`: 120~300s 타임아웃)이다.<br>2. DB 테이블이 없고 파일/메모리 기반으로 동작하므로, 실행부 이관(a)을 도입하려면 DB 마이그레이션(테이블 신설), executor consumer 루프 작성 등 150줄을 크게 초과하고 새 lease/fence 추가 금지 원칙과 충돌한다.<br>3. 따라서 drain 중 새 요청은 503으로 거절하고, 진행 중인 작업은 상한 내 완료를 대기하는 (b)가 가장 안전하고 적절하다. |
| **Google 로그인** | **(b) drain 거절 + 대기** | 1. 사용자가 로컬 화면에서 직접 Chrome 브라우저를 조작하는 대화형 흐름이므로 백그라운드 실행부 이관(a)이 원천적으로 불가능하다.<br>2. drain 중 신규 로그인 요청은 503(+Retry-After)으로 명시 거절하여 새 세대에서 시작하도록 유도해야 한다.<br>3. 이미 사용자가 조작 중인 로그인 태스크는 `drain_ready`를 막아 배포 시 즉시 종료되지 않도록 보호하고, 상한 내에서 자연 완료되거나 안전하게 취소/Chrome 정리를 수행해야 한다. 단, 진행 중 작업 정리를 위한 `complete`와 `cancel` 요청은 drain 중에도 허용한다. |

---

## 3. 구현 내용 요약

1. **ArtCraft (`src/ttakkari/artcraft.py`, `src/ttakkari/api/artcraft.py`)**:
   - `artcraft.inflight() -> int`: 메모리 `JOBS` 중 활성(`j.active == True`) 작업 수 반환.
   - `artcraft.drain(timeout=60.0) -> bool`: 진행 중 작업 자연 완료 대기 및 상한 초과 시 잔여 태스크 취소.
   - `api/artcraft.py`: `_assert_admitting()` 헬퍼를 추가하여 `/remote-check`, `/smoke`, `/apps/{app}/gui` 신규 시작 요청 시 `runtime.enter_admission("artcraft")`가 False이면 `503 Service Unavailable` + `Retry-After: 5` 반환.
2. **Google 계정 로그인 (`src/ttakkari/google_account/login.py`, `src/ttakkari/api/google_accounts.py`)**:
   - `LoginManager.inflight() -> int`: 완료되지 않은 로그인 태스크 수 반환.
   - `LoginManager.drain(timeout=20.0) -> bool`: 진행 중 로그인 태스크 자연 완료 대기 및 상한 초과 시 `shutdown()` 호출.
   - `LoginManager.shutdown()`: 등록된 cancel 이벤트 set과 더불어 활성 태스크에 `cancel()`을 호출하여 확실하게 종료 유도.
   - `api/google_accounts.py`: `login_start()`에 drain 검사 추가 (`503` + `Retry-After: 5`). `login_complete`, `login_cancel`은 예외 없이 정상 통과.
3. **수명주기 통합 (`src/ttakkari/generation_service.py`)**:
   - `ready(runtime)`: `api_residual = artcraft.inflight() + login_manager.inflight()`를 집계하고, `drain_ready` 조건에 `not api_residual`을 추가하여 잔여 작업이 끝날 때까지 퇴역/종료 차단.
   - `finish_claimed_work(seconds)`: local 모드 종료 대기에 `artcraft.drain` 및 `login_manager.drain` 포함.
   - `lifespan(app)`: remote 모드 API 프로세스 종료 시에도 `finally` 블록에서 `artcraft.drain` 및 `login_manager.drain`을 상한 내에서 안전하게 수행.

---

## 4. 검증 결과

### 4.1 TDD Red -> Green
- **신규 테스트 파일**: `unit_tests/test_remote_api_residual_execution.py` (10개 테스트)
  1. `test_artcraft_remote_check_rejected_with_503_during_drain`
  2. `test_artcraft_smoke_rejected_with_503_during_drain`
  3. `test_artcraft_gui_rejected_with_503_during_drain`
  4. `test_google_login_start_rejected_with_503_during_drain`
  5. `test_google_login_complete_and_cancel_not_503_during_drain`
  6. `test_normal_requests_allowed_in_active_mode_and_no_runtime`
  7. `test_ready_drain_ready_is_blocked_by_inflight_artcraft_job`
  8. `test_ready_drain_ready_is_blocked_by_inflight_google_login`
  9. `test_artcraft_drain_waits_for_completion_and_respects_timeout`
  10. `test_google_login_drain_waits_for_completion_and_respects_timeout`
- **Red 단계**: 기능 구현 전 8 failed, 2 passed 확인.
- **Green 단계**: 최소 구현 후 10 passed in 0.56s 확인.

### 4.2 변이(Mutation) 검증
1. **ArtCraft 거절 줄 제거**: `api/artcraft.py`의 `remote_check` 내 `await _assert_admitting()` 주석 처리 시 `test_artcraft_remote_check_rejected_with_503_during_drain` 실패 (202 != 503).
2. **Google 로그인 거절 줄 제거**: `api/google_accounts.py`의 `login_start` 내 503 거절 블록 주석 처리 시 `test_google_login_start_rejected_with_503_during_drain` 실패 (202 != 503).
3. **`drain_ready`의 `api_residual` 검사 제거**: `generation_service.py`의 `drain_ready`에서 `or api_residual` 제거 시 `test_ready_drain_ready_is_blocked_by_inflight_artcraft_job` 및 `test_ready_drain_ready_is_blocked_by_inflight_google_login` 실패 (True is not False).

### 4.3 회귀 테스트 수치
- `unit_tests/test_remote_api_residual_execution.py`: 10 passed in 0.56s
- `unit_tests/test_local_api_retirement.py`: 14 passed in 2.15s
- `unit_tests/test_executor_queued_consumers.py`: 4 passed in 1.18s
- `unit_tests/` 전체: 1824 passed, 1 skipped, 6 warnings, 5014 subtests passed in 302.10s (실패 30건은 worktree `.venv` 부재에 따른 backup 스크립트 실행 오류 및 외부 호스트 리소스/소켓 의존 테스트로 본 변경과 무관).
- 코드 스타일: `ruff check` 통과 (All checks passed!), `git diff --check` 통과.

---

## 5. 미검증 항목 및 한계

1. **실제 GUI 화면/Chrome 프로세스 실행**: Mac 로컬 GUI 화면 상에서 실제 Chrome 창이 열리고 닫히는 화면 동작 및 `bin/artcraft` 바이너리 실행은 샌드박스 환경 특성상 단위 테스트 mock으로 검증되었으며, 실제 GUI 화면 실측은 미검증이다.
2. **PostgreSQL 소켓 통합 테스트**: `tests/test_artcraft.py` 및 `tests/test_google_account.py`는 샌드박스 내부에서 PG DB 소켓 접근이 차단되어(`PermissionError: [Errno 1] Operation not permitted`) 실행하지 않았으며, DB 의존이 없는 `unit_tests/` 전체 및 격리된 단위 테스트로 검증을 대체했다.

---

## 6. 독립 검증자 결함 2건 TDD 추가 교정 (2026-10-25 06:15)

### 6.1 결함 분석
1. **결함 1: `drain_ready` 블로킹 및 상한 취소 부재**
   - ArtCraft(최대 300초 smoke)와 Google 로그인(창 대기 기본 600초, 설정 최대 1800초) 작업 진행 중 세대가 draining 상태가 되면 `api_residual > 0`이 되어 `generation_service.py`의 `drain_ready`가 `False`를 유지한다.
   - 배포 제어기(`generation_deployment.py:1052`)는 `drain_ready == True`일 때만 이전 세대를 정지(`slots.stop`)하므로, 상한 취소인 `artcraft.drain(30s)`와 `login_manager.drain(20s)`가 위치한 FastAPI `lifespan` 종료 블록에 진입하지 못하고 배포 퇴역이 수 분~수십 분간 블로킹되는 결함이 존재했다.
2. **결함 2: remote lifespan 종료 결과 및 예외 검사 누락**
   - remote lifespan 종료가 `gather(..., return_exceptions=True)`로 받은 결과를 검사하지 않아 drain `False`(상한 초과)나 예외 발생 시에도 경고 로그를 남기지 않았다 (local 모드는 `finish_claimed_work`에서 경고 로그 기록).

### 6.2 설계 선택 및 근거: 별도 감시 Task (drain loop in `coordinate`)
- **선택 방식**: `drain_ready` 평가 경로(GET /api/ready)에 상한 취소를 직접 두지 않고, **`coordinate` 루프의 draining 상태 전이 이벤트에서 별도 감시 태스크(`drain_residual_jobs`)를 구동하는 방식**을 선택했다.
- **선택 근거**:
  1. **부작용 없는 조회 보장 (CQS 원칙)**: `/api/ready`는 Liveness/Readiness 프로브 및 배포 제어기가 주기적으로 폴링하는 HTTP GET 엔드포인트다. 상태 조회를 호출한 행위 자체가 백그라운드 프로세스를 강제 종료(kill)하는 부작용을 일으키는 것은 책임 분리 원칙에 위배된다.
  2. **기존 라이프사이클(`coordinate`)과의 일관성**: 기존 플랫폼 V2에서 draining 상태 전이(`state == "draining"`) 시 리소스 회수 및 새 claim 차단(`desk_pool.shutdown()`, `queued_work.begin_drain()`, `background_consumers.pause_claiming()`)은 모두 `coordinate` 루프가 총괄하고 있다. 잔여 작업의 drain 감시 및 상한 관리도 `coordinate`의 상태 전이 이벤트에 연결하는 것이 아키텍처적으로 가장 일관된다.
  3. **폴링 독립적 적시 타이머**: 배포 제어기의 외부 HTTP 폴링 간격에 의존하지 않고, 프로세스 내부 이벤트 루프에서 상한 시간(`ARTCRAFT_DRAIN_TIMEOUT=30.0s`, `GOOGLE_LOGIN_DRAIN_TIMEOUT=20.0s`) 도래 시 즉시 안전한 취소 및 외부 Chrome 리소스 회수를 수행한다.
  4. **롤백 안전성**: 세대가 다시 `active`로 롤백되는 경우, `coordinate`에서 감시 태스크를 즉시 취소하여 안전하게 작업을 유지한다.

### 6.3 구현 요약
1. `src/ttakkari/generation_service.py`:
   - 모듈 상수 `ARTCRAFT_DRAIN_TIMEOUT = 30.0`, `GOOGLE_LOGIN_DRAIN_TIMEOUT = 20.0` 정의.
   - `drain_residual_jobs(artcraft_timeout, login_timeout)`: ArtCraft와 Google 로그인 작업을 각각 상한 안에서 대기하고, 초과 시 안전하게 취소(`drain(timeout=...)`)하며 명시적 경고 로그 기록.
   - `coordinate(runtime, manager)`: `state == "draining"` 진입 시 `residual_drain_task = asyncio.create_task(drain_residual_jobs(...))` 구동, `state == "active"`로 롤백 시 및 `finally` 블록에서 감시 태스크 취소.
   - `lifespan(app)`: remote 모드 API 종료 시 `drain_results = await asyncio.gather(...)`의 결과와 예외를 전수 검사하여, 실패 또는 예외 시 `log.warning("remote shutdown cut residual work: %s", sorted(failed))` 경고 로그 기록.

### 6.4 TDD Red -> Green 및 변이 검증
- **신규 테스트 5건 추가** (`unit_tests/test_remote_api_residual_execution.py`, 총 15개 테스트):
  - `test_draining_artcraft_job_cancelled_and_drain_ready_after_timeout`: 상한 초과 취소, 경고 로그, drain_ready True 검증.
  - `test_draining_google_login_cancelled_and_stops_chrome_after_timeout`: 상한 초과 취소, Chrome 프로세스 종료 경로(`proc.terminate`), cancelled 상태, 경고 로그, drain_ready True 검증.
  - `test_draining_jobs_finish_naturally_within_timeout`: 상한 내 자연 완료 유지 검증.
  - `test_coordinate_monitors_residual_drain_and_cancels_on_rollback`: coordinate 감시 태스크 구동 및 rollback 취소 검증.
  - `test_remote_lifespan_logs_warning_on_residual_drain_failure_or_exception`: remote lifespan 종료 결과 및 예외 경고 로그 검증.
- **Red 단계**: 기능 구현 전 신규 5건 실패 확인 (`5 failed, 10 passed in 0.63s`).
- **Green 단계**: 최소 교정 구현 후 15건 전수 통과 (`15 passed in 0.69s`).
- **변이(Mutation) 검증**:
  1. `coordinate` 내 감시 태스크 생성 줄 주석 처리 시: `test_coordinate_monitors_residual_drain_and_cancels_on_rollback` 실패 (TimeoutError).
  2. `drain_residual_jobs` 내 `artcraft.drain` 상한 취소 무력화 시: `test_draining_artcraft_job_cancelled_and_drain_ready_after_timeout` 실패 (assert True is False).
  3. remote lifespan 결과 검사 제거 시: `test_remote_lifespan_logs_warning_on_residual_drain_failure_or_exception` 실패 (assert False).

### 6.5 회귀 테스트 결과
- `unit_tests/test_remote_api_residual_execution.py`: 15 passed in 1.08s
- `unit_tests/test_local_api_retirement.py`: 14 passed in 2.18s
- `unit_tests/test_executor_queued_consumers.py`: 4 passed in 1.18s
- `unit_tests/test_generation_hardening.py`: 32 passed in 9.57s
- `unit_tests/test_fnd01_foundation.py`: 9 passed in 0.49s
- `unit_tests/` 전체: 1832 passed in 302.47s (실패 27건은 기분류된 worktree `.venv` 부재 등 환경 의존 항목)
- 코드 품질: `ruff check` 통과 (All checks passed!), `git diff --check` 통과.
