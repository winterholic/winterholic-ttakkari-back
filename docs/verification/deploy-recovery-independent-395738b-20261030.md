# 배포 drain 복구 독립 검증 — `395738b` (2026-10-30)

## 판정 요약

| 항목 | 판정 | 근거와 범위 |
|---|---|---|
| 고정 SHA import | PASS | `git archive 395738b1952784aa66b2ca470c5b520ed983605b`로 새 임시 test home에 추출. 핵심 파일 SHA-256이 git object와 동일하고, 그 추출본을 `PYTHONPATH=src`로 import해 테스트했다. 원 저장소 변경 없음. |
| 살아 있는 previous generation 복구·후속 foreground | PASS (mock 경계) | 장기 Run lease 2와 이전 슬롯 identity를 둔 기존 검증이 `resume_draining`, no-stop, 기존 슬롯 유지, 이후 f407을 foreground로 선택·처리함을 확인. 실제 운영 PID/DB/Run은 사용하지 않았다. |
| dry-run·재호출·Run 거부·stale fence·non-drain 거부 | PASS (격리 fixture) | dry-run journal 불변, idempotent 재호출, `TTAKKARI_RUN_ID` apply 거부, active epoch/identity/journal/slot stale 거부와 별도 `requested` 상태 거부를 확인. |
| OS lock/PID 관측 불능 fail-closed | **FAIL** | `PermissionError`/`psutil.AccessDenied`를 lock-free/PID-dead로 반환하는 최소 반례 재현. 이는 현재 Python 실행환경에서 만든 mock 결과이며 운영 macOS의 권한 상태를 측정한 것이 아니다. |
| apply 시 fence 재검사·offline DB/job 일관성 | **FAIL** | snapshot await 및 offline 재관측 시 epoch·lease/group가 변경되어도 stale `draining`/`succeeded` journal이 기록되는 mock 경합 반례 재현. |
| 비밀·경로·오류 비노출 | **FAIL** | 진단 queue의 blocking job `error`가 원문 그대로 출력됨을 synthetic marker로 확인. supervisor 요약에 경로 필드는 반환하지 않지만 queue error는 scrub되지 않는다. |
| 문서의 host CLI 절차 실행 가능성 | UNVERIFIED / 현재 MCP 접점 없음 | 문서는 Run 밖 host 터미널의 새 Python CLI 실행을 요구한다. 현재 MCP `host_operation_start`는 고정 `kind`만 받고 shell/path/임의 CLI를 허용하지 않는다. 새 CLI 실행, 서비스 변경, 실제 복구는 하지 않았다. 다른 host 접점의 부재까지 증명한 것은 아니다. |
| 운영 supervisor revision / 실제 abcb 복구 | UNVERIFIED | 제공된 17:14:15Z runtime readback의 활성 API는 `48eac0…`, 구현 SHA는 `395738…`; 새 코드는 활성화되지 않았다. old `abcb` blocking은 사용자 제공 관측과 코드상 조건부 추론이다. |

## 재현 환경과 고정본 확인

- 기준 저장소: `/Users/winterholic/development/ttakkari-deploy-recovery-20261030`, branch `fix/deploy-controller-recovery-20261030`, 고정 SHA `395738b1952784aa66b2ca470c5b520ed983605b`.
- 테스트용 export/import: `/private/tmp/deploy-recovery-395738-import.hij9iE` (git archive로 생성한 별도 사본).
- `src/ttakkari/generation_deployment.py` SHA-256: `7d7c0f731035d943983f7a7c285d73400208073b16f9e82fe7907e4971da9f56` (추출본과 git object 일치).
- 기존 독립 원인 분석 `docs/verification/deploy-recovery-pending-abcb-20261030.md`는 재조회·동일 테스트하지 않고, 새 반례와 현재 사용자 제공 runtime snapshot을 추가했다.
- PostgreSQL/initdb의 이전 알려진 거부를 재실행하지 않았다. PG, 전체 suite, 실제 사용자 Run, 운영 PID, host CLI, 배포/통합은 실행하지 않았다.

## 방금 실행한 검증

1. 고정 SHA에서 기존의 좁은 drain 테스트만 실행 (`cwd=/private/tmp/deploy-recovery-395738-import.hij9iE`):

```sh
PYTHONPATH=src /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/pytest -q \
  unit_tests/test_generation_hardening.py::test_recover_drain_resumes_live_draining_slot_and_unblocks_newer_foreground \
  unit_tests/test_generation_hardening.py::test_recover_drain_retires_unreachable_slot_only_when_proven_dead_and_lease_free \
  unit_tests/test_generation_hardening.py::test_recover_drain_refuses_identity_mismatch_epoch_stale_and_concurrent_lock \
  unit_tests/test_generation_hardening.py::test_drain_recovery_db_state_evaluates_real_orm_rows_and_os_locks \
  unit_tests/test_generation_hardening.py::test_deployment_cli_diagnose_and_recover_drain_subcommands
```

출력:

```text
.....                                                                    [100%]
5 passed in 0.74s
```

2. 테스트 home의 `independent_counterexamples_395738_test.py`에서 synthetic permission/race/error/non-drain cases 실행:

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src \
  /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/pytest \
  -p pytest_asyncio.plugin -q -s --confcutdir=. independent_counterexamples_395738_test.py
```

출력:

```text
observed=_lock_held(PermissionError) -> False
observed=mark_offline calls=[False, True]; job=succeeded; epoch=124; leases=1
observed=queue_diagnostics.blocking_jobs[0].error -> SYNTHETIC_SECRET_PATH_CANARY_98c1
observed=psutil.AccessDenied -> previous_pid_alive=False
observed=flock(PermissionError) -> _lock_held=False
observed=non-drain status=requested -> refused; journal unchanged
observed=slot await drift epoch=124, leases=7, groups=3; job=draining
7 passed in 0.23s
```

이 synthetic 테스트의 `passed`는 의심 동작을 재현했다는 의미다. 제품 안전성 PASS로 읽지 않는다.

## 세부 근거

### PASS — 살아 있는 previous 슬롯 재개 경로

`unit_tests/test_generation_hardening.py::test_recover_drain_resumes_live_draining_slot_and_unblocks_newer_foreground`는 기존 lease 2를 둔 fake slot에서 recovery를 dry-run/apply하고 다음을 assert한다: `action=resume_draining`, 이전 슬롯 identity 보존, `slots.stopped == []`, proxy route 유지, `pending_jobs()`에서 recovery job은 drains로 이동하고 후속 f407은 foreground로 선택되어 처리된다. 같은 targeted command에서 이 테스트 포함 5개가 통과했다. 이 검증은 mock slot/DB fixture에 한정된다.

기존 선택 안전장치도 동일 targeted command에서 확인했다: unreachable에 lease/group/lock/PID가 남으면 거부, epoch/active identity/journal/slot mismatch 거부, Run 안 apply 거부, restart lock 경합 거부, CLI dry-run 출력. 별도 probe는 `status=requested`를 apply할 때 거부하고 job journal이 동일함을 확인했다.

### FAIL — 관측 불능을 free/dead로 분류

`src/ttakkari/deployment.py:520-539`의 `_lock_held()`는 `Path.open()` 또는 `flock()`에서 일반 `OSError`가 나면 `False`를 반환한다. 격리 파일에 대해 open과 flock 각각 `PermissionError`를 주입했을 때 실제 결과는 `_lock_held=False`였다.

`src/ttakkari/generation_deployment.py:1117-1128`에서 PID 확인 `psutil.Error`/`OSError`는 `previous_pid_alive=False`가 된다. fake ORM row의 PID에 `psutil.AccessDenied`를 주입한 결과가 그대로 재현됐다. 이후 unreachable 분기의 `recover_drain()`은 1295-1306에서 이 boolean 값들을 실제 0/free/dead와 동일하게 검사한다. 그러므로 권한 거부 또는 관측 예외를 죽음 증거로 오인할 수 있다.

### FAIL — apply 중 stale fence와 DB/job journal 정합성

`src/ttakkari/generation_deployment.py:1095-1115`는 control·generation·lease/group를 일반 조회하고, `mark_offline=True`면 generation state만 `offline`으로 갱신·commit한다. 해당 조회는 control/owner 행을 잠그지 않는다. lock/PID 측정은 DB session 종료 뒤 1117-1128에서 이뤄진다.

`recover_drain()`은 restart gate를 잡은 뒤(1180-1184), DB state를 관측하고(1223-1253), active/previous slot snapshot을 await한다(1237-1261). 이후 apply 직전 control epoch, active generation/revision, lease/group, PID, OS lock을 다시 읽거나 fence하지 않는다. 

- mock snapshot await에서 epoch `123 → 124`, lease `2 → 7`, process group `2 → 3`으로 변경했지만 결과는 `job=draining`, `action=resume_draining`이었다.
- unreachable 경로에서 첫 확인 뒤 `mark_offline=True` 호출 시 epoch `123 → 124`, lease `0 → 1`로 변경하고 offline commit을 흉내 냈다. 반환값을 버리는 현재 호출 경로 때문에 최종 job은 `succeeded`가 됐다.

따라서 stale apply가 거부되지 않으며, database offline 상태와 job succeeded journal이 현재 소유권과 불일치할 수 있다. 이는 mock으로 만든 interleaving 재현이다. 실제 동시 DB transaction/lock 경합은 PG를 실행하지 않아 UNVERIFIED다.

### FAIL — diagnostics 원문 error 노출

`src/ttakkari/deployment.py:542-558`의 `_DIAG_JOB_KEYS`에 `error`가 포함되고 `_job_summary()`는 값을 그대로 반환한다. 최소 canary `SYNTHETIC_SECRET_PATH_CANARY_98c1`를 blocking job error에 넣자 `queue_diagnostics().blocking_jobs[0].error`에서 동일 문자열이 출력됐다. 이 경로는 `diagnose` 및 `runtime_report`의 queue summary에 연결된다. supervisor 쪽은 release 경로 문자열을 output dict에 넣지 않지만 queue `error`는 scrub이 없다.

### UNVERIFIED — 지원 실행 경로와 운영 현실

문서 `docs/design/generation-deployment.md:130-142`는 Run 밖 host 터미널에서 `python -m ttakkari.deployment diagnose` → `recover-drain` → 이후 `upgrade-supervisor`를 실행하라고 한다. CLI parser는 `src/ttakkari/deployment.py:746-760`에 있지만, 현재 지원 MCP `host_operation_start`는 `src/ttakkari/orchestrator/mcp_server.py:938-951`의 열거된 operation kind만 받고 설명상 shell/path/DB URL을 받지 않는다. 이 세션에서 host 밖 실행 또는 서비스 변경은 하지 않았다. 따라서 MCP는 새 CLI를 호출하는 접점이 아니고, 해당 문서 절차는 이 Run에서 실제 복구를 수행하지 못한다. 다른 지원 host terminal/접점은 확인하지 않았다.

활성 런타임 관측은 사용자가 제공한 17:14:15Z 스냅샷을 인용한다: API `48eac0c612d7186e8c418fe607a036ea2ab4da09`, active `bb450175`/epoch 123, old GAME `01c6010d@c70` draining/alive/heartbeat 0/leases 2. 후보 `395738…`가 이 runtime에서 실행된다는 증거는 없다. supervisor 실제 revision도 미관측이다. 이전 `abcb`가 현재 후속 예약의 직접 blocker라는 것은 현재 조건부 추론이지 새 live readback이 아니다.

## 교정 최소 조건

원본 저장소는 수정하지 않았다. 교정 후 재검증에서 최소한 다음을 요구한다.

1. OS lock/PID 관측 결과를 held/free/alive/dead/unknown으로 분리하고, permission/OS probe failure는 apply 불허로 처리한다.
2. DB control owner/generation fence를 원자적 transaction/row lock 또는 동등한 검증으로 보호한다. 모든 HTTP/OS await 뒤 identity·revision·epoch·lease/group/PID/lock 조건을 apply commit 직전에 다시 검증한다.
3. `offline` DB 커밋과 job journal `succeeded`를 같은 stale fence 아래서 정합되게 전이한다. `mark_offline` 반환값/최신값을 폐기하지 않는다.
4. 진단 응답의 raw `error`를 제거하거나 명시적 scrub/allowlist를 적용하고 synthetic 경로·secret canary negative case로 확인한다.
5. 독립적으로 지원되는 host CLI 실행 접점이 추가되거나, 문서 절차가 현재 지원 surface에서 실제 실행 가능한 경로로 바뀌어야 한다. 운영 변경은 이 검증 범위가 아니다.

FAIL 발견으로 구현은 직접 수정하지 않았다. 원 c936f6c0 작업자가 위 조건을 반영한 고정 revision을 낸 후 이 독립 검증을 다시 수행한다.
