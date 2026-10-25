# 독립 검증: adapter 5e8f17e

**판정: PASSED** — `5e8f17e`가 수정한 LaunchMismatch 정리 판정과 research v2 라우트 인벤토리는 기준을 충족한다. 전체 지정 테스트 명령은 통과하지 않았지만, HostGpu 13건은 기준 커밋에서도 같은 환경에서 동일하게 실패했고 catalog 검사는 기준 커밋에서도 별도 실패했다. 두 실패군은 이 어댑터 커밋의 회귀로 보지 않는다.

검증 대상은 `5e8f17e` (`87fec61`, `476521d` 위)다. 코드 변경은 읽기 전용으로 검토했고 요청한 pytest만 실행했다. 테스트 중 다른 작업자의 미커밋 변경이 worktree에 나타나 이를 보존했다.

## 정리 판정 로직과 테스트

- `src/ttakkari/research/adapters/llm_inference.py:466-474,505-518`: LaunchMismatch 뒤 supervisor와 server 각각의 `(pid, start time)`을 `process_state`로 확인한다. **둘 다 ABSENT일 때만** cleanup success다. PRESENT면 `launch_replaced_but_process_present`, UNKNOWN 또는 probe 예외면 `termination_unverified`로 실패한다. mismatch 경로는 다른 launch에 stop/signal을 보내지 않는다 (`:480-493,505-518`).
- `src/ttakkari/host_gpu_guard.py:45-67`: 기록 PID가 사라졌거나 좀비이거나 시작 시각이 달라 PID가 재사용된 경우 ABSENT, 같은 신원은 PRESENT, 프로세스 조회 실패나 PID/시각 정보 누락은 UNKNOWN이다. 생성 시각 비교에 `START_SKEW = 1.5s` 허용치가 있어, PID가 재사용되고 생성 시각 차이가 1.5초 미만이면 같은 프로세스로 볼 수 있는 이론적 오탐 여지는 남는다. 통상 재사용 판정은 시작 시각 비교로 수행된다.
- `tests/test_research_adapters.py:425-528`: 자식 프로세스는 실제 `subprocess.Popen`으로 생성하고 실제 PID·생성 시각으로 상태를 확인한다. 두 프로세스 종료 후 ABSENT 성공, supervisor/server 각각 생존 시 실패, server identity 누락 시 UNKNOWN 실패를 검증한다. LaunchMismatch 예외 자체는 `stop` mock으로 주입하지만, **성공 결론에 필요한 process evidence는 mock으로 주입하지 않는다**. 다른 launch에 신호가 가지 않는 것도 별도 확인한다 (`:521-528`).
- server identity가 아직 기록되지 않은 시작 직후에는 adapter가 같은 launch state를 다시 읽어 server PID를 보충한다 (`llm_inference.py:377-384`). 그래도 identity가 끝내 누락되면 UNKNOWN으로 실패한다. 테스트가 이 두 경우를 각각 검증한다 (`test_research_adapters.py:494-518`).

### `already_stopped`의 범위와 잔여 위험

`HostGpu.stop(expected_launch_id=...)`는 동일 launch의 state가 이미 비활성일 때 `stopped_now=False`를 돌려준다 (`src/ttakkari/host_gpu.py:842-848`). adapter는 이 결과에서 stopped 상태, `running` 아님, lease lock 해제를 확인한 뒤 `already_stopped`를 성공으로 기록하지만, 그 분기에서 supervisor/server PID를 별도로 조회하지는 않는다 (`llm_inference.py:495-504`). HostGpu supervisor는 server 종료를 `poll()`로 확인한 다음 stopped state를 쓰고 lease를 해제한다 (`host_gpu.py:402-429`). 따라서 정상 HostGpu 상태 전이에서는 서버와 자원 lease 정리가 선행되며, lock이 남아 있으면 adapter는 실패 처리한다. 다만 lease lock이 풀린 것만으로 supervisor 프로세스의 OS 종료까지 증명하지는 않으므로, **프로세스 종료를 엄격히 요구하는 의미의 cleanup success에는 좁은 종료 직전 경합 가능성이 남는다**. 기존 테스트 `test_llm_cleanup_skips_stop_when_launch_already_ended` (`test_research_adapters.py:531-541`)는 `stop_result`를 mock으로 제공하고 PID 상태는 검증하지 않는다. 낮은 심각도의 보증 범위/테스트 간극으로 기록하며, LaunchMismatch 결함 수정 자체를 무효화할 정도의 자원 정리 실패 증거는 아니다.

## HostGpu 비교: 환경 실패와 회귀 구분

현재 worktree에서 실행한 지정 묶음:

```text
$ ./.venv/bin/python -m pytest tests/test_research_adapters.py tests/test_host_gpu.py tests/test_research_v2_core.py tests/test_research_v2_catalog.py tests/test_route_inventory.py tests/test_authz03_host_scope.py tests/test_execution_capabilities.py tests/test_orchestrator.py
collected 201 items
================== 14 failed, 187 passed in 131.05s ==================
```

HostGpu 실패 13건은 같은 Python 3.13.15 / macOS 실행 환경과 동일 venv를 사용한 detached `92c4850`에서 비교했다. 임시 worktree `/private/tmp/ttakkari-adapter-review-92c4850`는 테스트 후 제거했다.

```text
$ /Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/.venv/bin/python -m pytest tests/test_host_gpu.py
================== 13 failed, 19 passed in 111.76s ==================
```

양쪽의 실패 테스트 13개가 동일했다. 현재 결과에는 `supervisor_error: OSError: ` (예외 메시지 비어 있음), ready 대기 timeout, watchdog 검색에서 `found == []`가 관측됐다. 기준 커밋에서도 같은 오류 계열과 실패 목록이 재현됐으므로 **이 HostGpu 실패는 이번 adapter 변경 회귀가 아니라 해당 실행 환경의 기존 실패**로 분류한다. 비교는 실패 재현 여부를 확인했을 뿐, 근본 원인이 OS 제한인지 다른 런타임 상태인지는 확정하지 않는다.

## catalog 실패 분리

지정 묶음의 나머지 실패는 `tests/test_research_v2_catalog.py::test_catalog_loader_full_112_matching`이다. 현재 worktree에서는 seed 파라미터 수가 184로 읽혀 기대치 185에서 실패했다. 실행 도중 worktree에 다른 작업자의 미커밋 변경이 관측됐고, 변경 파일에 catalog seed JSON이 포함되어 있어 이 관측을 깨끗한 기준 상태로 간주할 수 없다. 해당 변경은 건드리지 않았다.

기준 `92c4850` detached worktree에서 같은 개별 테스트도 실행해 분리 확인했다:

```text
$ /Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2/.venv/bin/python -m pytest tests/test_research_v2_catalog.py::test_catalog_loader_full_112_matching
FAILED: stats["relations"] == total_seed_relations; observed 126 != 128
```

기준 버전은 파라미터 수 검사(185)를 통과한 뒤 관계 수 검사에서 실패했다. 이는 대상 `5e8f17e` 변경 범위(어댑터·어댑터 테스트·route inventory)가 아니며 adapter 회귀 판정에서 제외한다. catalog 전체 검증은 **현재 동시 수정으로 확인 불가**, 기준 커밋의 관계 수 정합성은 별도 실패로 보고한다.

## 라우트 인증 분류

- `tests/test_route_inventory.py:71-99,118-139`는 라우트 dependency tree에서 이름만 일치하는 가짜 함수를 신뢰하지 않고 실제 등록된 auth callable identity (`require_user`, orchestrator auth, `_simple_auth`)와 scope marker를 수집해 라벨을 만든다.
- 연구 API는 `src/ttakkari/main.py:374-375`에서 owner 및 agent router가 모두 등록된다. manifest에는 owner research 라우트가 `owner-plain`, `/api/agent/research`가 `agent-plain`으로 등록되어 있다 (`tests/test_route_inventory.py:528-539,661,796-816`).
- 지정 실행에서 `tests/test_route_inventory.py` 전체가 통과했다. 실제 dependency 기반 live inventory와 분류표가 일치하며 누락·stale·분류 변경이 없음을 확인했다. 단위 테스트의 인증 marker 음성 사례도 포함돼 있다.

## 지정 테스트 결과와 커밋 범위

- 어댑터, HostGpu 비교 외 v2 core, route inventory, authz03, execution capabilities, orchestrator 테스트는 통과했다. 지정 전체 결과는 187 passed / 14 failed이며 실패는 위 HostGpu 13건과 catalog 1건으로 한정됐다.
- `git diff --check`는 출력 없이 종료 코드 0이었다.
- 임시 비교 worktree 두 개는 각각 제거했다. 보고서 작성 직전 worktree에는 다른 작업자의 미커밋 변경이 있었으며, 이번 커밋에는 지정 보고서만 포함한다.
