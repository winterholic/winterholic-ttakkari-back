# 배포 drain 복구 Run guard 독립 검증 — `baee36f` (2026-10-30)

## 판정

**좁은 판정: PASS — baee36의 Run-bound API apply 우회 교정이 격리 fixture에서 차단됐다.** API/controller apply 호출 전 거부, 임시 journal 바이트 보존, Run-bound dry-run 허용, Run 밖 controller 경로 positive control을 확인했다.

| 항목 | 판정 | 범위 |
|---|---|---|
| 고정 source archive/import | PASS | full SHA `baee36fd4812b0cbc3f4db7203856c7120bfa08a`; archive 파일 Git blob과 일치 |
| active Run-bound bearer `apply=true`, API env marker 없음 | PASS | HTTP 409; controller 호출 0; journal bytes 및 fake DB 상태 불변 |
| legacy bearer (`rid` 없음) `apply=true` | PASS | HTTP 409; controller 호출 0; journal bytes 및 fake DB 상태 불변 |
| Run 인증·소유 세션 경계 | PASS | inactive/missing Run은 공통 auth 401; 다른 세션의 running Run은 409; 직접 `(sid, rid)` 불일치와 잘못된 Run marker는 409 |
| API `TTAKKARI_RUN_ID` 및 controller `run_id`/env guard | PASS | API env guard 409; controller 각각 `run_id` 지정과 env marker에서 거부, journal bytes/state 불변 |
| 일치하는 Run-bound bearer의 `apply=false` | PASS | HTTP 200 dry-run; `run_id` 전달; journal bytes 및 fixture DB 상태 불변 |
| Run 없는 controller apply positive control | PASS (임시 fixture) | `run_id=None`, env marker 없음에서 임시 job이 `draining`으로 전이; `slots.stopped=[]` |
| 이전 c936 취약 assertion regression control | FAIL (기대된 실패) | 기존 probe의 “Run-bound apply가 200” 단언은 baee36에서 HTTP 409를 받아 실패. 이것만으로 PASS를 주장하지 않고 위의 별도 정상 기대 probe를 실행 |
| Audit DB commit 후행 실패 | 이전 c936 관찰 유지 | 이 교정의 대상이 아님. 첫 응답 500 뒤 journal side effect 및 같은 fence idempotent 재시도 관찰을 c936 정본에 보존; 재실행하지 않음 |
| 실제 PostgreSQL 경합·운영 recovery·Run 밖 host bootstrap | UNVERIFIED | 실제 DB/운영/host 접점 사용 안 함. 이전 c936 경계 유지 |

## 고정 대상과 원문 경로

- 구현 저장소: `/Users/winterholic/development/ttakkari-deploy-recovery-20261030`
- branch: `fix/deploy-controller-recovery-20261030`
- 검증 HEAD: `baee36fd4812b0cbc3f4db7203856c7120bfa08a`
- parent: `eee8029a6922488a9b3f8377217f9984772cf2d2`
- c936 구현 기준: `2df82c46f490e75c0dbe6f5835f73fc7dd6f73c1`
- 이전 판정: `docs/verification/deploy-recovery-independent-c936-20261030.md`
- 이전 probe: `/private/tmp/deploy-recovery-c936-import/independent_counterexamples_c936_test.py`
- 이번 고정 archive: `/private/tmp/deploy-recovery-baee36-import`
- 이번 최소 probe: `/private/tmp/deploy-recovery-baee36-import/run_guard_baee36_test.py`
- baee36 구현자 검증 기록: `docs/verification/deploy-recovery-hardening-20261030.md`
- working tree는 검증 전후 clean이었다. 제품 checkout은 편집하지 않았다.

기준 `eee8029…` 대비 6파일 `195 insertions(+), 18 deletions(-)`. 이 변경에는 API Run-bound 인증 경계, controller Run ID guard, route inventory 항목과 문서·구현자 회귀 검사가 포함된다. 검증 중 전체 49/57 suite는 반복하지 않았다.

### Import/hash 고정

`git archive baee36fd4812b0cbc3f4db7203856c7120bfa08a`를 `/private/tmp/deploy-recovery-baee36-import`에 추출했다. `shasum -a 256` 결과와 archive 파일의 `git hash-object` Git blob 확인:

| 파일 | SHA-256 | Git blob |
|---|---|---|
| `src/ttakkari/api/deployments.py` | `4b153e4c2c149b9f912b8ae461bb190c89fd7002744c62e6101677df5c0cb42c` | `84eeb78dd96f8928c65c9bf511b8d647ebdc77ce` |
| `src/ttakkari/generation_deployment.py` | `649b1b9af59b9e18df959a9049114522fe7de8e8faf2cc4c77fa554c2035f44f` | `0ddd299711e92173b80e5b93a49ec8f1ff1c2672` |

`git diff --check eee8029a6922488a9b3f8377217f9984772cf2d2..baee36fd4812b0cbc3f4db7203856c7120bfa08a`는 출력 없이 종료했다. root status는 `## fix/deploy-controller-recovery-20261030`, `HEAD=baee36fd4812b0cbc3f4db7203856c7120bfa08a`였다.

## 코드 경계 대조

`src/ttakkari/api/deployments.py`는 `require_orchestrator`가 공급한 sid 외에 Bearer를 다시 `_authenticate()`해 Run ID를 얻고, token sid와 요청 sid가 다르면 409를 낸다. Run ID가 있으면 `Run` row의 존재·`running` 상태·`session_id == sid`를 검사한다. `apply=true`에는 API 프로세스의 `TTAKKARI_RUN_ID`, token Run ID, Bearer credential 존재, 현재 세션의 running Run 중 하나라도 있으면 409를 반환한다. 거부 분기 뒤에서만 `GenerationController.recover_drain()`을 호출한다.

`GenerationController.recover_drain(..., run_id=...)`도 `apply=true`일 때 전달된 비어 있지 않은 Run ID 또는 process environment marker를 거부한다. 따라서 endpoint guard만 호출하지 않는 direct controller 경로도 보강된다. `apply=false`에는 이 guard가 적용되지 않으며 token Run ID를 controller에 전달한다.

## 독립 probe 및 결과

기존 c936 probe의 fixture/import 맥락을 재사용하되, c936의 취약 assertion은 안전 기대값으로 바꾸지 않고 그대로 regression control로 남겼다. 신규 probe는 고정 baee36 archive의 코드에서 실행했다. 테스트 설정은 pytest 자동 plugin을 끄고, 임시 archive에 `--confcutdir=.`를 지정해 repository conftest가 PG를 띄우지 않게 했다. DB row/lease/journal은 fixture와 임시 경로만 사용했다.

실행 명령:

```sh
cd /private/tmp/deploy-recovery-baee36-import
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=src:unit_tests \
  /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/pytest \
  -p pytest_asyncio.plugin -q -s --confcutdir=. run_guard_baee36_test.py
```

실제 출력:

```text
http: Run apply=409; legacy apply=409; inactive/missing=401; cross-session/direct tuple and malformed marker=409; env guard=409; matching dry-run=200/no journal/DB mutation; rejected controller calls=0
.controller: explicit run_id/env marker each refused with journal bytes+state unchanged; no-Run host fixture applied and transitioned journal to draining
.
2 passed in 1.12s
```

`2 passed`는 이 fixture에서 probe 기대값이 맞았다는 뜻이다. 실제 PostgreSQL이나 운영 API가 실행됐다는 뜻은 아니다. Run-bound 및 legacy `apply=true` 각각에서 응답 코드가 409이고 controller call counter가 0이었다. journal JSON 원본 bytes, recovery intent 부재, fake recovery DB state가 거절 전후 동일했다. 일치 Run의 dry-run만 controller에 `apply=False`와 `run_id`를 전달하고 결과 200을 반환했으며 같은 파일·상태 불변을 확인했다.

동일 대화에서 기존 c936 probe의 Run-bound 취약 단언도 baee36 import로 타깃 실행했다. 그 오래된 단언은 `HTTP 200`을 기대했으나 실제 응답 `409 Conflict`를 받아 실패했다. 이 실패는 취약 동작이 더 이상 관측되지 않았다는 regression 신호로만 취급하고, 안전 PASS는 위의 새 probe 결과로 판정했다.

## 제한과 이월 항목

- 사용한 API fixture는 실제 FastAPI HTTP transport와 실제 `issue_token(sid, rid)`를 사용했지만 DB/Run rows/controller journal은 격리 fake/임시 파일이다. 운영 token·세션·Run 데이터는 읽지 않았다.
- `apply=true`를 authenticated HTTP 경로에서 전면 거부하는 정책까지 테스트했다. 실제 host CLI의 지원 여부와 최초 `recovery_pending` bootstrap 접점은 c936 판정대로 **UNVERIFIED**이며 이번 검증에서 재조회·실행하지 않았다.
- 실제 PG `FOR UPDATE` 경합, DB transaction crash/ambiguous commit, API 운영 배포 revision, 실제 OS slot/PID/supervisor, 실제 recovery apply, 통합·push·deploy는 **UNVERIFIED / 미실행**이다.
- 이전 c936의 AuditLog commit 실패 후행 side-effect/500 및 retry 동작은 교정 대상이 아니며 기존 정본의 관찰을 유지한다. 넓은 전체 복구 목표는 아직 완료되지 않았다.
- 검증 worker는 backend receipt상 `task_type=verification`, `engine=codex`, `model=gpt-6-luna`, `effort=high`, personal scope로 선택됐다. 선택 계정 quota snapshot은 5시간 사용 7%(잔여 93%), 7일 사용 47%(잔여 53%)로 receipt 시각 기준 age 5초였다. 이 독립 검증 probe와는 별개의 상태이며, receipt 조회 시 worker는 `running`이어서 worker 분석 결과를 본 판정의 근거로 사용하지 않았다.
