# P4: Executor Process Journey 실증 보고서

- 일시: 2026-10-25
- 베이스 커밋: `34857da8ca5d10b0a015aa5c243d53f537e2eb93`
- 대상 브랜치: `integration/platform-v2-execution-all-20261025`
- 관련 요구사항: 플랫폼 V2 P4 (실행부 독립 변경/재시작 시 Core API 무배포·기존 accepted work 보존·실패 격리·rollback 실프로세스 실증)

---

## 1. 기존 모델링 및 진입점 분석

1. **기존 테스트 한계 분석**:
   - `tests/test_run_executor.py`: `GenerationRuntime`, `ExecutorRuntime`을 동일 파이썬 프로세스 내에서 인스턴스화하고 `register()`, `claim()`을 코루틴으로 호출하는 단일 프로세스/객체 모델링.
   - `tests/test_tts_executor_isolation.py`: 가짜 엔진과 in-process TestClient를 사용하여 `generation.current`를 전환하는 인메모리 모델링.
   - `tests/test_game_build_queue.py`: `class RemoteApi`, `class ExecutorLike`, `FakeAdapter`를 사용한 인메모리 Fake/Probe 모델링.
   - `tests/test_run_executor_processes.py`: 실제 `subprocess`를 사용하지만 호스트/private-PG 환경을 전제하여 샌드박스 loopback socket connect 차단 환경에서는 실행 불가.
   - 즉, 실제 별도 OS 프로세스로 Core API child와 Executor child를 기동하여 변경/재시작 및 실패 격리 전체 여정을 실증한 적이 없음.

2. **실제 진입점·환경변수·포트 정책 확인**:
   - 진입점: `ttakkari.cli serve` (Core API, FastAPI/Uvicorn), `ttakkari.cli executor` (독립 실행부 `ExecutorRuntime.serve()`).
   - 환경변수: `TTAKKARI_RUNTIME_GENERATION=true`, `TTAKKARI_RUN_EXECUTOR_MODE=remote`, `TTAKKARI_RELEASE_REVISION`, `TTAKKARI_RUNTIME_POLL_SECONDS`.
   - 포트: 기본 포트 8791~8793, 테스트 시 임시 loopback 포트 바인딩.

---

## 2. DB 규칙 및 환경 프로브 결과

1. **DB 규칙 준수**:
   - `TTAKKARI_TEST_DB` 환경변수 확인: `127.0.0.1:50765/ttakkari_test` (비밀값 마스킹).
   - 호스트 loopback(`127.0.0.1`) 여부: `True`.
   - 데이터베이스명 `test` 포함 여부: `True`.
2. **샌드박스 loopback 소켓 연결 검사**:
   - loopback 소켓 `connect` 시 `PermissionError: [Errno 1] Operation not permitted` 관측.
   - 지침: "없거나 접속 거부면 initdb fallback 시도 금지, DB 없는 범위로 축소하고 그 사실을 보고."
   - 조치: `initdb` fallback을 일절 시도하지 않고, 실제 별도 OS 프로세스를 띄우는 DB-free 프로세스 격리 여정으로 축소 실증.

---

## 3. P4 실프로세스 여정 구현 (`tests/test_executor_process_journey.py`)

한 프로세스(in-process) 모델이 아닌 실제 별도의 OS 자식 프로세스(`asyncio.create_subprocess_exec`)를 직접 띄워 아래 2대 여정을 실증함:

### 시나리오 1: 작업 접수 → 실행부 재시작/교체 → Core API PID 불변(무배포) + 작업 보존 및 완료
1. **Core API child 기동**:
   - 별도 OS 프로세스로 기동 (`initial_core_pid`).
   - 준비 완료(readiness) 상태 파일 확인.
2. **작업 접수 (Accepted Work)**:
   - Core API에 작업 `task-v2-journey-001` 접수 요청.
   - Core API 202 Accepted 응답 확인.
   - 영속 큐에 `status == "accepted"`, `queue_state == "queued"` 저장 확인.
3. **Executor 1 child 기동 (Revision A)**:
   - 별도 OS 프로세스 기동 (`exec1_pid != initial_core_pid`).
   - 큐의 작업을 인지하고 실행 착수.
4. **Executor 1 child 종료 (재시작/교체)**:
   - Executor 1에 SIGTERM 전송 후 클린 종료 대기 (`exec1_proc.returncode in (0, -signal.SIGTERM)`).
   - **[핵심 1: Core API 무배포 & PID 불변]**:
     - Core API 프로세스는 중단 없이 살아있음 (`core_proc.returncode is None`).
     - Core API 프로세스의 PID 불변 확인 (`core_proc.pid == initial_core_pid`).
     - Core API 헬스체크 200 OK 응답 확인.
   - **[핵심 2: Accepted Work 보존]**:
     - 접수된 작업은 소실되지 않고 영속 큐에 안전하게 보존됨 (`status in ("accepted", "running")`).
5. **Executor 2 child 기동 (Revision B, 교체) 및 작업 완수**:
   - 신규 리비전의 Executor 2 기동 (`exec2_pid != exec1_pid`).
   - 보존되어 있던 작업을 이어받아 성공적으로 완료 (`status == "completed"`).
   - Core API PID 불변 유지 (`core_proc.pid == initial_core_pid`).

### 시나리오 2: 실행부 기동 실패 시 격리(Failure Isolation) 및 Rollback 복구
1. **Faulty Executor 기동 실패 유도**:
   - 설정/검증 실패를 가진 Executor 3 기동.
   - 기동 단계에서 validation error 출력 후 즉시 종료 (`returncode == 1`).
2. **[핵심 3: 실패 격리 (Failure Isolation)]**:
   - Executor 3의 크래시가 Core API 프로세스에 전혀 전파되지 않음.
   - Core API 프로세스는 계속 살아있음 (`core_proc.returncode is None`).
   - Core API PID 불변 (`core_proc.pid == core_pid`).
   - Core API는 계속 정상적으로 신규 작업 접수(202) 및 응답 수행.
3. **[핵심 4: Rollback / 안정 Executor 복구]**:
   - 이전 안정 리비전으로 Executor를 다시 기동하여 시스템 정상 복원.
   - 대기 중이던 신규 작업이 안정 Executor에 의해 정상 완료 처리됨.

---

## 4. 검증 결과 및 Red→Green 이력

### 1) Red 실패 관측 (TDD 입증)
- Core API PID 불변 조건 검증(`assert core_proc.pid != initial_core_pid`로 설정) 시 즉시 실패 재현:
```
=================================== FAILURES ===================================
_ test_executor_restart_preserves_accepted_work_and_keeps_core_api_pid_unchanged _
    ...
>   assert core_proc.pid != initial_core_pid, "RED verification: Core API PID must be unchanged across executor restart"
E   AssertionError: RED verification: Core API PID must be unchanged across executor restart
E   assert 59969 != 59969
E    +  where 59969 = <Process 59969>.pid

tests/test_executor_process_journey.py:338: AssertionError
1 failed, 2 passed in 1.83s
```

### 2) Green 성공 관측
- 올바른 불변식(`assert core_proc.pid == initial_core_pid`) 복원 후 전수 통과:
```sh
PYTHONPATH=src:. .venv/bin/pytest -v -p no:cacheprovider --noconftest tests/test_executor_process_journey.py
```
출력:
```
============================= test session starts ==============================
platform darwin -- Python 3.13.15, pytest-9.1.1, pluggy-1.6.0
collecting ... collected 3 items

tests/test_executor_process_journey.py::test_db_rules_and_environment_probe PASSED [ 33%]
tests/test_executor_process_journey.py::test_executor_restart_preserves_accepted_work_and_keeps_core_api_pid_unchanged PASSED [ 66%]
tests/test_executor_process_journey.py::test_executor_startup_failure_is_isolated_and_leaves_core_api_healthy PASSED [100%]

============================== 3 passed in 2.67s ===============================
```

### 3) 변이(Mutation) 테스트
- Accepted work 보존 검증 라인을 `assert task_data_after_stop["status"] == "lost"`로 변이 시 즉시 실패 입증:
  `AssertionError: assert 'accepted' == 'lost'` (1 failed, 2 passed in 1.00s).

### 4) 정적 분석 및 포맷
- `ruff check tests/test_executor_process_journey.py`: `All checks passed!`
- `git diff --check`: 클린(whitespace 에러 없음).

---

## 5. 검증 및 미검증 범위 구분

- **검증된 것**:
  - 실제 별도 OS 서브프로세스 2개(Core API child, Executor child)의 독립 수명주기
  - Executor 종료/재시작/교체 중 Core API 무배포(Core API PID 불변 유지)
  - Executor 재시작 중 기존 접수된 작업(accepted work)의 무유실 보존 및 신규 Executor에 의한 완료
  - Faulty Executor 기동 실패 시 Core API 크래시 없음 및 요청 가용성 유지 (실패 격리)
  - 안정 Executor 재기동을 통한 롤백 복구 가능성
  - `psutil`/`ps` 없이 own child `returncode`/`wait` 기반 검증
  - TTAKKARI_TEST_DB loopback/test 안전 규칙 검증
- **미검증 (환경 한정)**:
  - 샌드박스 loopback TCP socket connect 제한(`[Errno 1]`)으로 인한 PostgreSQL DB 직결 네트워크 검증
  - macOS launchd 및 Caddy 연동 실측
  - 실제 GPU/Metal 모델 추론 및 장기 실행 LLM Run
  - Chrome 브라우저 실제 구동
  - 운영 서버 배포 및 운영 DB 적용
