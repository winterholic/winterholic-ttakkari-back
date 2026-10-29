# 배포 감독 Drain Recovery 하드닝 및 격리 검증 기록 (2026-10-30)

## 1. 대상 및 배경

- 작업 저장소: `/Users/winterholic/development/ttakkari-deploy-recovery-20261030`
- 브랜치: `fix/deploy-controller-recovery-20261030`
- 기반 커밋: `395738b1952784aa66b2ca470c5b520ed983605b`
- 교정 대상: 독립 검증 보고서(`deploy-recovery-independent-395738b-20261030.md`)의 3개 FAIL 및 실행 접점·부트스트랩 공백

## 2. 교정 내역

### 2.1 FAIL #1 교정 — OS 잠금·PID `unknown` 상태 분리 및 Fail-Closed

- `src/ttakkari/deployment.py`
  - `_probe_lock(path)` 도입: `FileNotFoundError`만 `("free", None)`, `BlockingIOError`만 `("held", None)`, 디렉터리·파일 `PermissionError` 및 기타 `OSError`/비정규 파일은 `("unknown", <ErrorType>)`으로 분리했다.
  - `_lock_held(path) -> bool | None`: `"held" -> True`, `"free" -> False`, `"unknown" -> None`을 반환하도록 변경해 기존 `path.exists()` 및 `except OSError: return False`의 fail-open 경로를 제거했다.
- `src/ttakkari/generation_deployment.py` 및 `src/ttakkari/operations/inline.py`
  - `_probe_pid(pid, started_at)` 및 `_pid_alive(pid, started_at) -> bool | None` 도입: `psutil.NoSuchProcess`/`errno.ESRCH` 또는 `process_started_at` 불일치(PID 재사용)만 `dead`(`False`)로 판정하고, `psutil.AccessDenied`, `PermissionError`, `psutil.ZombieProcess`(서브클래스 순서 우선 처리), 무효·미기록 PID는 모두 `unknown`(`None`)으로 분리했다.
  - `recover_drain` 및 `_drain_recovery_db_state(..., mark_offline=True)`는 `previous_lock_held is None` 또는 `previous_pid_alive is None`이면 dry-run과 apply 모두 즉시 `RuntimeError`로 거절한다.

### 2.2 FAIL #2 및 Crash Consistency 교정 — Await 뒤 Canonical `FOR UPDATE` Row-Lock 순서와 2단계 `recovery_intent`

- **기존 Lease Writer들과의 잠금 순서 대조**:
  - `generation.activate`, `GenerationRuntime.register`/`claim`/`release`/`record_process_group`, PostgreSQL 트리거(`alembic/versions/0064_run_executor.py`의 `executor_api`, `executor_lease`, `executor_command`), `executor_reconcile._apply_locked`는 모두 1순위로 `RuntimeControl(id=1)` `FOR UPDATE`를 획득한다.
  - 빈 `RuntimeLease` 집합에 대한 단순 트랜잭션 내 `SELECT count(*)` 재검사는 동시 `claim()` INSERT(phantom)를 막지 못하지만, `RuntimeControl(id=1)` `FOR UPDATE`를 먼저 잡으면 앱의 `_control(db)`와 PostgreSQL `executor_lease` 트리거 양쪽 모두에서 신규 lease INSERT와 `epoch`/`active_generation` 변경이 트랜잭션 종료 시점까지 직렬화 차단된다.
- **적용한 잠금 순서 (Apply Commit 단계)**:
  1. `RestartGate("backend-restart.lock")`
  2. DB 세션: `SET LOCAL lock_timeout = '2000ms'`, `SET LOCAL statement_timeout = '15000ms'`
  3. `RuntimeControl(id=1)` `FOR UPDATE`
  4. `RuntimeGeneration(active_id)` `FOR UPDATE`
  5. `RuntimeLease(generation_id == previous_id)` `FOR UPDATE`
  6. `RuntimeGeneration(previous_id)` `FOR UPDATE`
  7. `RuntimeProcessGroup(generation_id == previous_id)` `FOR UPDATE`
  8. 잠금 아래 OS 잠금(`_probe_lock`) 및 OS PID(`_probe_pid`) 즉시 재측정 + 초기 관측(`initial_state`) 대비 전 필드 무변화(no drift) 검증
  9. `JobStore.locked()` (`requests.lock` — 내부 DB 호출이 없어 역방향 교착 없음) CAS 파일 갱신
- **DB `offline`과 파일 `succeeded` 간 Crash Consistency 상태 전이**:
  - `resume_draining`: 모든 HTTP `slots.snapshot` `await` 완료 후 위 5개 테이블 `FOR UPDATE` 잠금 안에서 `previous_state == "draining"`, `previous_lock_held is True`, `previous_pid_alive is True`, `recovery_intent is None`, 무변화(no drift)를 재확인하고 **DB row-lock을 해제하기 전**에 `JobStore.locked()` CAS로 `status="draining"` 및 `recovery_receipt`를 기록한다.
  - `retire_verified_dead`:
    1. 모든 HTTP `await` 완료 후 `JobStore.locked()` CAS 아래 `status="recovery_pending"`을 유지한 채 `recovery_intent`(`token`, `action="retire_verified_dead"`, `stage="marking_db_offline"`)를 먼저 `fsync` 기록한다.
    2. 위 5단계 `FOR UPDATE` 잠금 아래 모든 fence, `claim_owner != previous_id`, `leases == 0`, `process_groups == 0`, `lock_held is False`, `pid_alive is False`, 무변화(no drift), `recovery_intent.token` 일치를 검증한 뒤에만 `prev_row.state = "offline"`을 커밋한다. 잠금 타임아웃(`55P03`/`57014`)이나 조건 불일치 시 명시적으로 `db.rollback()`하고 `recovery_intent`를 제거한 뒤 실패한다.
    3. 최신 반환 상태를 폐기하지 않고 재확인한 뒤 `JobStore.locked()` CAS로 `recovery_intent` 제거 + `status="succeeded"` + `retired_generation` + `recovery_receipt`를 기록한다.
    4. 2단계 DB `offline` 커밋 직후 3단계 파일 쓰기 전에 `OSError`/크래시가 나면 파일은 `status="recovery_pending"` + `recovery_intent`로 남아 감독의 `cutover_blocked=True`를 유지한다. 이 중간 상태에서 위조/일시 슬롯 응답으로 `resume_draining` 전이를 시도해도 거절되며, 파일 장애 해소 뒤 재호출(re-entry) 시 `previous_state == "offline"`에서 모든 fence와 부재 조건을 다시 잠금 검증해 `status="succeeded"`로 수렴한다.
  - 완료 후 재호출 idempotency는 `expected_epoch`, `previous_generation`, `previous_slot`, `active_generation`, `active_revision` 5개 필드가 모두 `recovery_receipt`와 일치할 때만 성립한다.

### 2.3 FAIL #3 교정 — Diagnostics 원문 오류 비노출 및 구조적 Allowlist

- `src/ttakkari/deployment.py`
  - `_job_summary`에서 raw `"error"` 필드를 완전히 제거했다.
  - 원문을 한 글자도 복사하지 않는 고정 패턴 분류 코드(`error_code`), boolean `has_error`, `recovery_intent_pending`, 그리고 allowlist 검증을 거친 `error_stage`·SHA(40자 hex)·UUID·ISO 시각·상태값만 반환하도록 제한했다.
  - `supervisor_diagnostics`에서도 `pinned_revision`, `runtime_revision`, `last_upgrade.revision`은 40자리 hex SHA일 때만, `last_upgrade.status`는 고정 allowlist 상태일 때만 노출하도록 제한했다.
- `unit_tests/test_collection_runtime_report.py`
  - 기존 `395738b`에 하드코딩되어 있던 로컬 worktree 절대 경로 단언(`Path("/Users/winterholic/development/ttakkari-deploy-recovery-20261030")`)을 `Path(__file__).resolve().parents[1] / "src"` 기준으로 교정해 `git archive` 격리 export 및 배포 릴리스 테스트 게이트(`releases/<SHA>`)에서도 동일하게 동작하도록 수정했다.

### 2.4 Active MCP Catalog 대조, 부트스트랩 공백, 그리고 배포 활성화 이후 Fenced API 대안

- **Active MCP Catalog vs 제품 소스 대조**:
  - 현재 운영 중인 구버전 활성 API(`48eac0c6`, active `bb450175`, epoch `123`)가 에이전트 Run에 노출한 MCP catalog에는 고정 9종 `host_operation_start`, `request_backend_deploy`, `backend_deploy_status`, `ops_*`만 존재한다.
  - 본 브랜치의 `runtime_report` 확장(`supervisor`, `deploy_queue`, `slots`) 및 신규 fenced API(`GET /api/agent/deploy/diagnostics`, `POST /api/agent/deploy/{job_id}/recover-drain`)는 새 커밋이 배포·컷오버되기 전까지 활성 서버에 반영되지 않는다.
- **최초 1회 부트스트랩 공백 명시**:
  - `abcb3f7d`가 `recovery_pending` 상태로 후속 배포 전환을 차단하고 있는 동안에는 새 커밋이 스스로 먼저 배포될 수 없으며, `TTAKKARI_RUN_ID`가 설정된 에이전트 Run 내부에서는 `recover-drain --apply`가 거절된다(Run guard 유지).
  - 따라서 최초 1회 `abcb3f7d` 해소는 현재 지원 MCP에서 실행할 수 없으며, 반드시 **따까리 Run 밖의 사용자 호스트 터미널**에서 검증된 소스의 `python -m ttakkari.deployment diagnose` 및 `recover-drain ... --apply`로 수행해야 한다.
- **배포 활성화 이후 최소 지원 Fenced API (`src/ttakkari/api/deployments.py`)**:
  - `GET /api/agent/deploy/diagnostics` 및 `POST /api/agent/deploy/{job_id}/recover-drain` 추가(`core.generation_deploy` 분류 정합 확인).
  - 임의 shell·경로 입력을 받지 않는 `RecoverDrainIn`(`extra="forbid"`), 개인 오케스트레이터 세션 전용(`pool="personal"`, `kind="orchestrator"`), `idempotency_key` 바인딩(`recover-drain:{job_id}:{expected_previous_generation}:{expected_epoch}`), 현재 API 서버 프로세스 자신의 활성 소유권 검증(`generation.current.id == expected_active_generation`, `not draining`, `RuntimeControl.epoch == expected_epoch`), `AuditLog`(`deployment.recover_drain`) 기록을 강제한다.
  - 본 엔드포인트는 독립 재검증 완료 및 정식 배포 활성화 전까지 사용하지 않는다.

## 3. 변경 파일 목록

- `src/ttakkari/deployment.py`
- `src/ttakkari/generation_deployment.py`
- `src/ttakkari/operations/inline.py`
- `src/ttakkari/api/deployments.py`
- `unit_tests/test_generation_hardening.py`
- `unit_tests/test_collection_runtime_report.py`
- `docs/design/generation-deployment.md`
- `docs/verification/deploy-recovery-hardening-20261030.md`

## 4. 실행한 격리 검증 증거

1. **단위 및 격리 반례 스위트 (신규 7개 반례 + 기존 하드닝/런타임 리포트 전체)**:
   - 명령:
     `PYTHONPATH=/Users/winterholic/development/ttakkari-deploy-recovery-20261030/src /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/pytest /Users/winterholic/development/ttakkari-deploy-recovery-20261030/unit_tests/test_generation_hardening.py /Users/winterholic/development/ttakkari-deploy-recovery-20261030/unit_tests/test_collection_runtime_report.py -q`
   - 결과: `49 passed in 5.27s`
2. **독립 검증자 원본 반례 스크립트 읽기 전용 대조**:
   - 명령:
     `PYTHONPATH=/Users/winterholic/development/ttakkari-deploy-recovery-20261030/src /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/pytest /private/tmp/deploy-recovery-395738-import.hij9iE/independent_counterexamples_395738_test.py --tb=short -q`
   - 결과: 기존 취약점 존재를 단언하던 6개 케이스 모두 차단되어 반박됨(`_lock_held(PermissionError) -> None`, `_drain_recovery_db_state(AccessDenied) -> previous_pid_alive=None`, `queue_diagnostics` raw `error` `None`, `mark_offline` 및 `slots.snapshot` await 이후 epoch/lease drift `RuntimeError` 거절), 비-drain 거부 단언 1개 `PASSED`.
3. **Platform V2 백엔드 인벤토리 계약 스캐너 회귀 검사**:
   - 명령:
     `PYTHONPATH=/Users/winterholic/development/ttakkari-deploy-recovery-20261030:/Users/winterholic/development/ttakkari-deploy-recovery-20261030/src /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/pytest /Users/winterholic/development/ttakkari-deploy-recovery-20261030/unit_tests/test_platform_v2_inventory.py -q`
   - 결과: `57 passed in 63.35s`
4. **린트 검사 (`ruff check`)**:
   - 명령:
     `cd /Users/winterholic/development/ttakkari-deploy-recovery-20261030 && /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/ruff check --no-cache src/ttakkari/deployment.py src/ttakkari/generation_deployment.py src/ttakkari/operations/inline.py src/ttakkari/api/deployments.py unit_tests/test_generation_hardening.py unit_tests/test_collection_runtime_report.py`
   - 결과: `All checks passed!`

## 5. 미실행 및 미검증 범위

- **실제 PostgreSQL 트랜잭션 및 Row-Lock 실경합 통합 테스트**: 미실행. 중첩 워커 샌드박스에서 `tests/test_generation_deployment.py`의 `tests/conftest.py:204 (_schema_and_app)` 실행 시 로컬 PostgreSQL loopback TCP `sock.connect(('127.0.0.1', ...))`가 `PermissionError: [Errno 1] Operation not permitted`로 거부됨(샌드박스 환경 제약이며 호스트 장애 아님). SQLAlchemy `Select._for_update_arg` 및 잠금 획득 순서·타임아웃 롤백은 fake session 어댑터로만 검증했으므로 실제 PostgreSQL 동시 트랜잭션 경합은 미검증 상태다.
- **운영 Supervisor/DB/슬롯 실측 및 실제 Host CLI 실행**: 미실행. 운영 보호 경로(`~/.ttakkari/deploy`, 운영 DB, `launchctl`) 불변 제약 및 에이전트 Run 내부(`TTAKKARI_RUN_ID`) 제한에 따라 운영 상태에 대한 `diagnose` / `recover-drain --apply`는 실행하지 않았다.
- **`abcb3f7d` 실제 운영 복구 및 후속 누적 배포**: 미실행. 독립 재검증자(`b99551fe-e4a2-498e-b4d5-e26421e91e04`)의 재검증 통과, `fcb55b7d-a9ed-4160-a395-84fe291ab9ad`의 단일 `main` 통합, 그리고 Run 밖 호스트 터미널에서의 최초 1회 부트스트랩 실행이 완료되기 전까지 전체 운영 복구 완료로 판정하지 않는다.
