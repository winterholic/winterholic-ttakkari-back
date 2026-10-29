# 배포 drain 복구 교정본 독립 안전성 검증 — `c936` (2026-10-30)

## 판정

**전체 안전 판정: FAIL.** controller의 프로세스 환경변수 Run guard는 확인됐지만, 새 HTTP API를 통해 실행 중인 Run-bound 오케스트레이터가 `apply=true`를 수행하는 반례가 재현됐다. 나머지 격리 반례가 통과한 사실은 이 API 경로의 우회를 상쇄하지 않는다.

| 항목 | 판정 | 범위 |
|---|---|---|
| 고정 구현본과 임시 import 사본 | PASS | HEAD `eee8029a6922488a9b3f8377217f9984772cf2d2`; 임시 사본과 세 핵심 모듈 SHA-256 일치 |
| OS lock unknown fail-closed | PASS (주입 fixture) | permission 거부→unknown; 없는 파일→free; 실제 잠금 보유→held |
| PID unknown fail-closed | PASS (주입 fixture) | unknown 거부, 명시적인 dead negative control 유지 |
| await 뒤 fence drift / journal 불변 | PASS (주입 fixture) | snapshot 뒤 epoch·lease drift 거부, journal payload 불변 |
| recovery intent crash/re-entry | PASS (파일·DB fake fixture) | DB offline 뒤 파일 실패에서 pending intent 유지, 재호출 수렴, 동일 fence 멱등, 변경 epoch 거부 |
| diagnostics canary 및 allowlist | PASS (synthetic fixture) | canary 미노출, 오류 코드·단계·상태 allowlist 값은 유지 |
| API 인증·pool/kind/archive·schema·owner | PASS (HTTP fixture) | 401/403/422/409 경계 및 일치하는 실제 `GenerationRuntime` 객체의 no-stop 경로 확인 |
| **활성 Run의 API 경로 apply 차단** | **FAIL** | Run-bound bearer와 실행 중 Run으로 호출 시, API 프로세스에 `TTAKKARI_RUN_ID`가 없으면 HTTP 200 및 `applied=true` |
| audit DB commit 실패 | 관찰·부분 검증 | 첫 요청 500이지만 journal은 이미 `draining`; 같은 fence 재호출은 200/idempotent. 실제 AuditLog 영속성은 fake DB가 확인하지 않음 |
| 실제 PostgreSQL row-lock 경합 | UNVERIFIED | 잠금 순서만 코드 대조. 실제 PG 트랜잭션/동시 claim 경합 미실행 |
| Run 밖 host bootstrap / 운영 복구 | UNVERIFIED | MCP operation catalog에 임의 host CLI 실행은 없지만, 별도의 사용자 host 터미널 접점 유무는 확인하지 않음. 실제 복구·운영변경 없음 |

## 대상 고정

- 작업 저장소: `/Users/winterholic/development/ttakkari-deploy-recovery-20261030`
- branch: `fix/deploy-controller-recovery-20261030`
- 검증 HEAD: `eee8029a6922488a9b3f8377217f9984772cf2d2`
- 교정 기준: `2df82c46f490e75c0dbe6f5835f73fc7dd6f73c1`
- 이전 기준: `395738b1952784aa66b2ca470c5b520ed983605b`
- 이전 독립 정본: `docs/verification/deploy-recovery-independent-395738b-20261030.md`
- 고정 검증 사본: `/private/tmp/deploy-recovery-c936-import`
- 구현 저장소 working tree는 검증 중 clean이었다. 구현 파일은 수정하지 않았다.

세 구현 파일의 working tree와 임시 import 사본 SHA-256이 각각 일치했다.

| 파일 | SHA-256 |
|---|---|
| `src/ttakkari/generation_deployment.py` | `c31ee8620629cefffd3ea2e5e9280c5c08ebc63504aeb0947103f802e688fb64` |
| `src/ttakkari/deployment.py` | `525f192f3f731006eb38c5edf7a41dc50e40d6be2fda8dcc3a01ddfa1500ef98` |
| `src/ttakkari/api/deployments.py` | `b2131c09e058477328de6f9dd5234f20eb3fc132138a1a59bfa535ae13476dfa` |

## 독립 반례 실행

기존 교정 probe를 재사용했다. 최초 실행은 probe 디렉터리에서 `unit_tests`가 Python import 경로에 없어 collection 오류(`ModuleNotFoundError: test_generation_hardening`)로 종료됐다. 제품 코드는 건드리지 않고 fixture 경로를 추가해 재실행했다. 이후 API Run-guard 대조를 같은 격리 probe에 추가했다.

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=unit_tests:src \
  /Users/winterholic/development/ttakkari-infra-integ-20261029/.venv/bin/pytest \
  -p pytest_asyncio.plugin -q -s --confcutdir=. independent_counterexamples_c936_test.py
```

실행 위치: `/private/tmp/deploy-recovery-c936-import`

```text
lock: denied=unknown; missing=free; held=held
pid: unknown rejected; explicit dead negative control retained
snapshot drift: rejected; journal byte-level payload unchanged
crash: pending intent after file failure; reentry converged; same fence idempotent; changed epoch refused
diagnostics: canary absent; error/status allowlists still emit expected values
api HTTP: unauth=401; extra/type=422; worker/work/archived=403; GET diagnostics=200; old runtime=409; env Run guard=409; real active runtime resumes without stop; audit failure=500 after journal changed; retry idempotent=200; active Run-token apply accepted when API env has no Run marker
6 passed in 1.52s
```

`6 passed`는 각 격리 probe의 기대 결과가 관측됐다는 뜻이다. API Run-bound 반례는 기대 동작을 위반했으므로 제품 안전 PASS가 아니다.

## FAIL — Run-bound 호출이 HTTP API의 Run guard를 통과

컨트롤러 `recover_drain()`은 `apply=True`일 때 현재 프로세스의 `TTAKKARI_RUN_ID`를 확인한다. probe에서 그 변수를 설정하면 API 요청은 409로 막혔다. 이는 controller guard의 동작만 보여준다.

같은 HTTP fixture에서 다음 조건을 바꿔 대조했다.

1. API 서버 환경에서 `TTAKKARI_RUN_ID`를 제거했다.
2. 실제 `issue_token(session_id, run_id)`로 Run-bound bearer를 발급하고, 인증 조회가 해당 `Run`을 `running`으로 반환하게 했다.
3. 세션은 개인 `orchestrator`; payload는 정확한 recovery fence와 `apply=true`; current runtime은 실제 `GenerationRuntime` 인스턴스였다.
4. endpoint 호출 결과가 `200`, `applied=true`였고 임시 journal이 `draining`으로 바뀌었다.

API endpoint는 `AgentSession`/`require_orchestrator` 의존성을 통해 `sid`만 받는다. `_authenticate()`는 Run-bound 토큰의 Run 상태를 검사한 뒤 `(sid, run_id)`를 반환하지만 `require_orchestrator()`가 `sid`만 노출한다. 새 endpoint는 `sid`의 개인·오케스트레이터·비보관 여부만 확인하고 활성 Run 여부는 확인하지 않는다. 따라서 controller의 환경변수 guard가 API 서버 프로세스에 전달되지 않는 경로에서는 실행 중인 Run이 API를 통해 우회할 수 있다.

이 재현은 API/DB/slot을 fake fixture로 만든 격리 HTTP 테스트다. 운영 프로세스 환경을 관측한 것은 아니다. 다만 소스 계약상 endpoint가 Run ID를 받지 않는다는 점과 Run-bound token을 사용한 반례 결과가 함께 있어, **API 경로의 Run 차단은 현재 코드에서 증명되지 않았고 fixture에서는 실제로 실패했다.**

## 나머지 안전성 대조

### PASS — 잠금/PID unknown과 negative controls

- `PermissionError` 주입은 lock `unknown`으로 분류됐다.
- 파일이 없을 때 `free`, 독립 파일 핸들이 잠금을 보유할 때 `held`로 남아 checker가 모든 값을 unknown으로 바꿔 통과하는 경우를 배제했다.
- 이전 PID 상태 unknown은 apply 거부, 명시적 dead는 dead로 반환됐다.
- 실제 OS PID 접근 권한이나 운영 lock 상태를 측정한 것은 아니다.

### PASS — await 뒤 fence drift 및 recovery intent 재진입

- `slots.snapshot()` 완료 뒤 epoch 및 lease 수를 바꾸자 recovery가 거부됐고, job journal의 원 payload는 바이트 단위로 같았다.
- DB offline 처리 후 final journal 파일 쓰기에 1회 오류를 주입했다. `recovery_pending`과 `recovery_intent`가 남았고, 재호출에서 완료로 수렴했다. 같은 fence 재호출은 idempotent였으며 epoch를 바꾸면 거부됐다.
- 이 fixture는 crash ordering과 re-entry를 확인한다. 실제 DB/파일시스템 전원장애의 fsync 내구성은 실측하지 않았다.

### PASS — diagnostics와 endpoint 경계

- `SYNTHETIC_SECRET_PATH_CANARY_C936`는 응답에서 사라졌고 `has_error`, `redacted_error`, `error_stage`, `status`는 예상 allowlist 값을 유지했다.
- HTTP fixture에서 무인증 `401`, extra/type `422`, worker/work/archived `403`, old current runtime `409`, diagnostics `200`을 확인했다.
- `GenerationRuntime` 실제 클래스의 인스턴스를 썼다. fixture가 해당 객체 ID를 DB active generation과 맞추어 일치 경로와 불일치 경로를 구성했으며, 일치 경로에서 slot stop은 없었다. 이는 실제 startup/register와 운영 소유권 측정이 아니다.
- audit commit을 실패시키자 API가 `500`을 반환했지만 recovery journal은 이미 `draining`이었다. commit 실패를 해제한 같은 fence 재시도는 `200`, `idempotent=true`였다. 즉 호출자는 첫 응답만 보면 실패로 보지만 변경은 적용된 상태다. fixture는 AuditLog 행의 재시도 후 실제 영속성을 검증하지 않았다.

### PASS (코드 대조만) — DB 잠금 순서

복구 경로는 `RuntimeControl(id=1)` → active generation → 이전 generation lease 행 → previous generation → process-group 행 순서로 `FOR UPDATE` 한다. 소스상 `activate`, `GenerationRuntime.register/claim/release/record_process_group`는 `_control()`을 먼저 호출하고, `executor_reconcile._apply_locked`도 `_control()` 다음에 lease/generation/group를 잠근다. `0064_run_executor.py`의 API/lease/command trigger도 먼저 `runtime_control FOR UPDATE`를 취한다.

따라서 신규 lease claim과 epoch/owner 변경을 직렬화하려는 설계는 기존 writer 순서와 일치한다. PG loopback 권한 거부 이력이 있어 알려진 실행을 반복하지 않았으며, 실제 transaction lock/phantom 경합은 **UNVERIFIED**다. 코드 순서 대조를 실 PostgreSQL PASS로 확대하지 않는다.

## Bootstrap / 운영 경계

- `host_operation_start` catalog는 유한한 `kind` enum만 받으며 shell/path/DB URL 또는 임의 Python CLI 실행을 제공하지 않는다. 현재 MCP에서 `recover-drain --apply`를 host CLI로 호출할 지원 operation은 확인하지 못했다.
- 이것은 Run 밖 사용자 호스트 터미널이 존재하지 않는다는 증거가 아니다. 그 접점과 그곳에서의 실행 권한은 **UNVERIFIED**다.
- 신규 `/api/agent/deploy/...` endpoint가 현재 활성 서버에 배포되어 있는지는 이번 검증에서 조회하지 않았다. MCP 도구로 등록된 것도 확인되지 않았다.
- 운영 DB, OS PID/lock, supervisor, `abcb3f7d` 실제 상태, 복구 apply, 통합·게시·배포는 건드리지 않았다.
- 기준 `395738b`의 앞선 5개 정상 경로 및 7개 취약점 재현 증거는 이전 정본에 보존되어 있으며 반복하지 않았다.

## 결론과 다음 판정

이 단계 결과는 **FAIL: HTTP API가 Run-bound `apply=true`를 차단하지 않음**이다. 이 독립 검증에서는 구현을 수정하지 않았다. API가 Run ID를 명시적으로 받아 active Run 요청을 거부하거나, 동등한 서버 측 guard를 추가한 고정 revision에서 이 반례를 다시 확인해야 한다. audit commit 실패 후 side-effect가 먼저 남는 응답 모호성도 설계 계약을 정하고 함께 다룰 필요가 있다.

실 PostgreSQL 경합, 외부 host bootstrap, 실제 운영 recovery는 여전히 미검증이다. 이 보고서만으로 통합·게시·배포·운영 복구를 승인하지 않는다.
