# INFRA 복구 접점 독립 검증 — 2026-10-29

## 판정

**조건부 실패: active generation fence가 ABA를 막지 못한다.** 현재 active generation의 ID와 revision이 과거 값으로 돌아오면 `RuntimeControl.epoch`가 바뀌어도 stale 요청이 새 복구 접점에 통과한다. dispatch/reconcile API와 MCP에서 epoch를 입력받지 않고, dispatch manager의 잠금 안 재검사도 ID·revision만 비교한다.

최소 수정 제안: 두 API/MCP 입력에 `expected_claim_owner_epoch`를 필수로 추가하고 API에서 현재 `RuntimeControl.epoch`와 비교한다. `manager.dispatch`도 세션 잠금 아래 같은 epoch를 다시 비교한다. reconcile은 active-owner 확인 때 epoch를 비교한다. fresh read/runtime receipt가 그 값을 제공해야 한다.

## 고정 범위와 증거 출처

- 독립 검토 checkout: `/Users/winterholic/development/ttakkari-infra-integ-20261029`; 검토 전 clean, `HEAD=6a5589c5e08de35d66aed9b53f5cd6b6245fc756`.
- 복구 코드 변경: `587d1fe1bf6efff14f6bea1f710457fded0345bf`.
- MCP/assistant tool manifest 변경: `3005697fe33f5a5f07fb17f171f36771ee35a3bf`.
- 최종 고정본은 위 복구 경로와 manifest를 포함한다. `manager.py`에는 후속 main 변경(종료 signal의 EPERM 처리)이 있어 고정본에서 다시 읽었다.
- import 경로와 고정 SHA 확인 명령/출력:

  ```text
  $ PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -c 'import ttakkari.api.orchestrator as m; print(m.__file__)'
  /Users/winterholic/development/ttakkari-infra-integ-20261029/src/ttakkari/api/orchestrator.py
  $ git rev-parse HEAD
  6a5589c5e08de35d66aed9b53f5cd6b6245fc756
  ```

## 범위별 결과

| 확인 | 결과와 근거 |
|---|---|
| caller tree / pool / 정확한 Run | fake API 반례에서 필수 입력 누락, 스푸핑 idempotency key, 다른 pool, 다른 worker tree, 대상 session에 없는 Run이 각각 422/404로 거절됐다. `orchestrator.py`는 `(Run.id, Run.session_id)`를 함께 조회한다. |
| queued 순서 / 중복 dispatch | `manager.dispatch`는 session row lock 후 `expected_run_id`가 queue head인지 확인하고, running Run과 local handle이 있으면 거절한다. `tests/test_run_dispatch_transaction.py::test_targeted_dispatch_never_skips_an_older_queued_run`도 오래된 queued Run을 건너뛰지 않는 반례다. 새 Run 생성 호출은 없다. |
| stale revision | fake API에서 현재 revision과 다른 expected revision은 409로 거절됐다. targeted reconcile은 `reconcile_one`에서 Run의 lease owner ID/revision/status가 하나라도 다르면 `fence_mismatch`다. |
| lease/process fence | 기존 `_read_orphans`→`_apply_one` 경로를 재사용한다. `_apply_locked`는 control→lease→owner→process-group→Run을 잠그고 lease token, owner PID/start-time, 전체 process-group fence를 다시 맞춘다. 응답에는 PID/token을 반환하지 않는다. |
| 살아있는 draining GAME | fake에서 owner가 살아 있으면 disposition은 `owner_alive`, `_apply_one`은 DB/OS 경로에 들어가기 전에 `False`를 반환했다. 실제 GAME 프로세스/운영 DB는 호출하지 않았다. 실제 생존 판단 및 PostgreSQL lock fence는 미검증이다. |
| API/MCP 입력 경계 | Pydantic 필수 필드·Literal status, API의 정확한 idempotency key를 확인했다. MCP는 key를 입력받지 않고 worker/run 조합으로 생성하며 대상 도구는 worker/orchestrator 역할 카탈로그에 등록돼 있다. MCP 서버 실호출은 하지 않았다. |

### 결함 재현

`generation.activate()`는 매 활성화마다 `RuntimeControl.epoch += 1` 한다. 반면 API 입력 모델에는 epoch가 없고 `dispatch_queued_worker_run` / `reconcile_worker_lease`는 expected generation ID와 revision만 비교한다. `manager.dispatch`의 잠금 내부 재검사도 동일하다. harness에서 current control이 과거 A와 동일한 ID/revision이면서 `epoch=19`인 상태를 만들고 그 과거 ID/revision 요청을 주자 dispatch가 받아들여졌다. 이는 A→B→A 상태의 fake 재현이며, 세대 전환 자체를 운영에서 수행한 것은 아니다.

- 위치: `src/ttakkari/api/orchestrator.py:165-182, 247-254, 290-297`; `src/ttakkari/runs/manager.py:340-373`; epoch 증가: `src/ttakkari/generation.py:88-93`; MCP 입력: `src/ttakkari/orchestrator/mcp_server.py:1078-1117`.
- 영향: 오래된 generation 관측으로 생성한 dispatch/reconcile 요청이 같은 세대 ID/revision의 재활성화 뒤에도 통과할 수 있다. 복구 접점의 freshness fence가 요구대로 강제되지 않는다. 다른 worker tree나 새 Run에 접근하는 권한 확대는 관측되지 않았다.

## 실행 증거

재현 가능한 deterministic fake harness는 [`infra_recovery_endpoint_fake_probe.py`](infra_recovery_endpoint_fake_probe.py)에 보존했다. 이번 검토에서 실행한 명령:

```sh
PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python docs/verification/infra_recovery_endpoint_fake_probe.py
```

출력:

```text
/Users/winterholic/development/ttakkari-infra-integ-20261029/docs/verification/infra_recovery_endpoint_fake_probe.py:56: StarletteDeprecationWarning: 'HTTP_422_UNPROCESSABLE_ENTITY' is deprecated. Use 'HTTP_422_UNPROCESSABLE_CONTENT' instead.
PASS fake API: required input / spoofed key / other pool / cross-tree / exact run lookup / stale revision fail closed
FAIL fence: stale A+revision accepted with control.epoch=19; API/MCP have no expected epoch
PASS fake reconcile: live owner disposition=owner_alive; _apply_one returned False before DB/OS action
```

구현자 실행 결과는 재사용했고 반복하지 않았다. 복구 focused 묶음 `17 passed in 2.07s`; merge 후 범위 묶음 `154 passed, 2 xfailed, 2 warnings in 8.60s`; pre-push contract `84 passed, 5985 deselected in 13.50s`, changed tests `57 passed in 14.53s`, Ruff 통과. 이 결과는 각각 그 기록에 지정된 코드/merge/pre-push revision의 단위·계약 범위다.

## 미검증 및 안전 경계

- 이 Codex `exec_command` 환경에서 PostgreSQL fixture 초기화의 `initdb`가 `shmget: Operation not permitted`로 차단된 것은 확정된 기존 결과다. 같은 PG target을 다시 실행하거나 다른 엔진으로 우회하지 않았다. 실제 DB 트랜잭션, row-lock 경합, OS PID/process-group 검증은 미검증이다.
- 실제 worker session/Run dispatch, orphan lease reconcile, GAME draining process, 운영 DB 쓰기, Run 취소, 배포는 수행하지 않았다.
- 배포 예약 `1ec180ed-987a-42c3-95a0-1918917bbcd1`은 `requested`였다는 root 기록만 재사용한다. 이는 배포 성공이나 운영 복구 증거가 아니다.

