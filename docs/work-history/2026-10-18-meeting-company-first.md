# 회의록 분석: 회사 계정 우선·Gemini 우선 (2026-10-18)

사용자 요청: 켜진 회사 계정을 먼저 사용하고, 이후 Gemini → GPT Luna medium → Sonnet medium 순서로 분석한다. 10/16의 개인 계정 전용·Luna 우선 결정을 대체하며 일반 오케스트레이터/작업자 모델 정책은 유지한다.

## 구현

- `meetings/routing.py`: `pool=work`의 `work_enabled` 계정이 먼저다. 사용 가능한 회사 후보가 없으면 `pool=personal`의 `enabled` 계정으로 넘어간다. 두 범위 모두 Gemini Flash high → GPT Luna medium → Antigravity Sonnet medium → native Sonnet medium 순서다. Gemini는 기존 Flash high를 유지했다.
- `Profile.pool`을 계정에서 전달하고 `first_stage`가 범위·엔진·모델을 함께 비교한다. 개인 후보의 잔여량이 더 많아도 회사 후보와 섞어서 선택하지 않는다. 회사 모드에서 끈 계정은 개인 모드의 enabled가 켜져 있어도 재진입하지 않는다.
- 기존 모델 지원 목록·usage window·계정 상한·cooldown·런타임 한도 전환을 유지한다. 모두 소진되면 기존 대기/개인 범위 비상 정책을 유지한다. 회사 범위는 로컬 Qwen 비상 경로에 들어가지 않는다.
- `runs/manager.py`는 native Luna/Sonnet에 medium을 실제 LaunchSpec과 Run에 기록한다. Antigravity는 모델 ID에 생각 수준이 있으므로 별도 effort 인자를 넘기지 않는다.
- 회의 `scope`와 분석 세션 `pool`은 데이터/MCP 범위로 유지한다. 모델별 provider conversation 분리도 유지한다.
- ADR 0010, README의 현재 정책을 갱신하고 10/16 기록에는 대체됨 표시를 남겼다.

## 검증 환경과 증거

Codex 명령 실행 환경의 분리 작업 트리 `back-meeting-priority`, 전용 DB `ttakkari_test_meeting_priority_1018`, 합성 엔진으로 검사했다. 실제 LLM 분석 결과·MCP 실행 성공과는 구분한다.

RED: 기존 코드에서 `tests/test_meeting_routing.py -x`는 Gemini 우선 기대와 달리 `gpt-6-luna`를 선택해 `1 failed, 1 passed`로 실패했다.

첫 검사: 관련 11개 파일 `276 passed, 1 warning in 82.62s`. 이후 병행 작업 커밋 `01779bea08c159a0f345e0382245d4d975d630ad` 위에 rebase해 최종 재검사를 실행했다. 최종 결과: **293 passed, 1 warning in 83.45s**. 경고는 기존 accounts API의 Starlette HTTP 422 상수 deprecated 알림이다. 최종 검사 명령은 다음과 같다.

```sh
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_test_meeting_priority_1018 PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/pytest tests/test_meeting_routing.py tests/test_meetings.py tests/test_meeting_scope.py tests/test_meeting_pms.py tests/test_meeting_exports.py tests/test_account_selection.py tests/test_accounts.py tests/test_api_runs.py tests/test_antigravity_model_routing.py tests/test_generations.py tests/test_worker_routing.py unit_tests/test_qwen_emergency.py -q --tb=short --junitxml=/Users/winterholic/.ttakkari/data/outbox/7964f8ed-cae1-414c-9f39-e84c6cae3e7e/meeting-priority-tests.xml
```

검사 범위: 회사 계정 우선, 개인/회사 토글 독립, 회사 한도 사전 제외와 런타임 한도 fallback, 네 모델 순서와 native medium, Antigravity Gemini/Claude quota 그룹 독립 및 credits=0, 회사 회의의 모두 소진 시 대기, 모델별 대화 키, 회의 범위·조회 권한, 일반 Run·계정 선택·worker routing·generation 회귀.

정적 검사: 변경 Python 파일 Ruff check와 `git diff --check`, `py_compile` 통과. Ruff format --check는 변경 대상 5개 모두 기존 HEAD에서도 실패하는 저장소 형식 차이를 확인해 일괄 포맷하지 않았다. mypy는 프로젝트 venv에 미설치라 미실행. pitfall_scan의 `profiles.choose` 기본 인자 경고는 기존 HEAD와 동일한 immutable `frozenset()`에 대한 기존 결과이며 새 결함으로 판단하지 않았다.

운영 확인: gateway `/api/version`은 `c5c0040e5d68b1312bbc851d54f58bdc3ae0b09d`, `/api/health`는 `{"ok":true}`. gateway는 `/api/ready`를 의도적으로 404로 숨긴다(`generation_deployment.py`). 운영 분석을 새로 실행하지 않았다.

## 배포

검증 후 clean commit을 origin/main에 push하고 generation 배포를 예약한다. 예약 접수와 운영 활성화는 구분하며, Run을 유지하며 배포 완료를 폴링하지 않는다.
