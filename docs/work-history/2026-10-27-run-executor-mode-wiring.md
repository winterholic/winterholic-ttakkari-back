# Remote API 기동 모드 배선

## 결함

`Slots.start`가 쓰는 generation API plist에는 `TTAKKARI_RUN_EXECUTOR_MODE`가 없었다.
`GenerationRuntime`은 `get_settings().run_executor_mode`(기본값 `local`)에서 모드를 읽으므로,
제품 경로로는 API가 항상 local로 기동한다. 반면 `ensure_executor`와
`executor.validate_handoff`는 모든 live API가 protocol2 remote일 것을 요구한다.
그래서 활성화 게이트는 제품 경로에서 절대 통과할 수 없는 상태였다.
현재 두 매니페스트가 `activation: disabled`이므로 운영 장애가 아니라 소스 경계다.

## 교정

- `Slots.start`는 기동하는 릴리스의 고정 계약을 읽어 plist env에 모드를 명시한다.
  `run_executor.activation == "enabled"`일 때만 `remote`, 그 외(없음·disabled·
  알 수 없는 값·계약 읽기 실패)는 `local`이다. 기존 호출부 3곳은 그대로다.
- executor 프로세스(`ExecutorRuntime`)는 모드를 `"local"`로 고정하므로 매니페스트 변경은 없다.

## 증거

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q unit_tests/test_executor_safety.py --tb=short
```

신규 4 param RED(`KeyError`, 1.62s) → 교정 후 21 passed(0.69s).
이웃 `test_run_executor_unit.py`·`test_executor_migration_contract.py` 합계 51 passed,
1 failed는 base에서도 동일하게 실패하는 샌드박스 loopback EPERM
(`test_api_remote_tick_does_not_consume_executor_commands`, `Slots` 미사용)이라
이 변경과 무관하다. ruff·`git diff --check` 통과.

## 남은 일

매니페스트 `enabled` 전환(activation 후보)은 별도 변경이다. 전환 릴리스는 먼저
local 보고 릴리스 활성 뒤 remote 활성의 2단계 순서를 따른다(실행계약 FND1).
배포 게이트 전체 회귀와 운영 게이트(drain된 lease 경계 등)는 controller가 소유한다.
