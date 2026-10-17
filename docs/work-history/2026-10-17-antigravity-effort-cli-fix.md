# Antigravity worker effort 인자 시작 실패 수정 — 2026-10-17

## 원인

- 운영에서 재현된 `invalid model selection (--model "claude-sonnet-5-5-high" --effort "high")`는 코드상 Antigravity 모델 ID의 마지막 `high`를 CLI `--effort high`로 중복 전달한 경우와 일치한다.
- 2026-10-16 `adc20af`에서 `runs/worker_routing.py:effort()`가 Antigravity 모델의 `thinking_level()`을 실행 effort로 반환했다. `runs/manager.py:_launch_model()`에도 같은 계산이 별도로 있었다. `agents/antigravity.py`는 전달받은 effort를 무조건 `--effort`로 조립했다.
- 최초 task-aware 배정은 `worker_routing.effort()`를, 명시 엔진 실행과 재개는 `_launch_model()`을 거친다. 한도 fallback은 새 모델 profile 기준으로 배정 helper를 다시 부른다. 따라서 세 경로 모두 잘못된 인자를 만들 수 있었다.

## 변경

- `model_catalog.supports_effort(engine, model)`에서 엔진·모델별 별도 effort 인자 지원을 정규화한다. Antigravity 모델은 생각 수준을 모델 ID가 이미 표현하므로 false다.
- task-aware 및 고정 엔진 launch에서 Antigravity Run의 effort를 `None`으로 정규화한다. API도 worker 세션에 기본 `high`를 저장하지 않으며 명시 입력은 모델 ID와 일치하는지 계속 검증한다.
- Antigravity adapter도 모델이 지원하지 않으면 `--effort`를 조립하지 않도록 방어한다.
- 최초 자동 배정, quota fallback, explicit Antigravity 및 기존 대화 재개 회귀 테스트를 보강했다.

## 회귀 발생 시점

`git blame` 기준 잘못된 task routing effort 매핑은 2026-10-16 19:59 KST `adc20af`에서 추가됐다. `_launch_model()`의 동일 매핑은 그날 Antigravity 모델 라우팅 통합 과정에 들어왔다.

## 검증

- `tests/test_worker_routing.py`: 57 passed
- `tests/test_antigravity_model_routing.py`: 26 passed
- `tests/test_antigravity_adapter.py`: 10 passed
- `ruff check src tests`는 이번 변경이 아니라 선행 로컬 Muse 후보 커밋의 `tests/test_muse_auto_routing.py` import 정렬/중복 fixture import 6건으로 실패했다. 변경 파일 대상 Ruff는 별도 확인한다.
- 로컬 fake CLI 및 pytest 테스트 증거이며, 운영 배포 후 실제 Antigravity worker 시작은 별도 확인 대상이다.
