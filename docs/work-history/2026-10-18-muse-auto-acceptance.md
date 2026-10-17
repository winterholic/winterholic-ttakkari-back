# Muse 자동 배정 코드 수락 — 2026-10-18

## 판정과 범위

운영 `6d5f58068071fd0a6cdc7711d4f9804d168f30f6`에서 Muse Spark 1.3의 작은 파일 작성·실행, MCP 호출, 동일 엔진 세션 재개를 각각 확인했다. GPT-6 Luna 독립 검증의 실제 명령과 결과까지 read-only RunEvents로 대조하고 원 Muse worker에 `verification_outcome=passed`를 기록했다. 이를 근거로 기존 general/routine 자동 배정 수락 래치와 기본 설정을 연다.

이는 작은 운영 smoke에 대한 수락이다. 이 변경의 배포 후 실제 auto 선택, 공정 벤치마크, 긴 작업, 취소, 실패 복구, 병렬 운영은 별도 미검증이다. 배포 예약 접수도 운영 배포 성공을 의미하지 않는다.

## 근거

- 원 Muse worker: `458b75d2-5016-4365-8301-60fada90bcaf`.
- 첫 Run: `3f53b88c-b48e-4545-bbda-d052b5b634f7`, `succeeded`, exit 0. bash 작성·실행 결과와 `mcp__ttakkari__recall(query="Muse")` 결과는 각각 call_id가 대응하고 `is_error=false`다. bash 결과의 `exit_code=0`도 확인했다.
- `/private/tmp/muse-check/fizz.py`를 기획에서도 독립 실행했다. exit 0, 1~15 FizzBuzz 전체 15줄이 기대값과 일치했다.
- 재개 Run: `dce78032-a207-4ec9-8bd5-c74ec70e8e3f`, `succeeded`, exit 0. prompt에 표식을 주지 않았는데 결과에서 회수했고, 두 Run의 `agent.session.engine_session_id`는 `01a14ab2-e1f1-71b1-ae8e-1643523c842e`로 같았다.
- 두 Run의 `run.finished` 실제 필드는 `engine=muse`, `model=muse-spark-1.3`, `task_type=general`이었다. 자동 보고의 Gemini Flash 문구는 실제 provider의 증거가 아니었다.
- 독립 검증 worker: `942f05a4-42fb-4a49-bfbb-2c6b07342aac`, Run `f7e16eaf-b5b0-469a-8bd7-54e8badc4d6e`, 실제 선택 codex/gpt-6-luna. 세 항목 모두 passed. 기획이 해당 worker의 shell 명령과 출력, 원 Run 상태·완료 이벤트를 `SET TRANSACTION READ ONLY`로 직접 확인했다.
- 검증 worker가 `8791` 연결 거부와 공개 주소의 SPA 응답 때문에 당시 runtime revision을 재확인하지 못한 점은 환경·URL 범위의 한계다. 기획에서 `curl -fsS --max-time 10 http://127.0.0.1:8787/api/version`을 실행해 위 `6d5f580…` 리비전을 재확인했다.
- 원본 근거: outbox `980d98dc-01c7-4122-a1aa-4f8de392805f/muse-operating-smoke.json`, `1b15ef09-3abf-4d06-806c-7b3aebf1a19e/muse-resume-evidence.json`. 이번 대조 요약: outbox `7729dfcb-51cd-4f24-9838-93d083f85422/muse-acceptance-evidence.json`. 원본은 수정하지 않았다.

## 변경과 불변 조건

- `MUSE_AUTO_ROUTING_ACCEPTED=True`, `muse_auto_routing=True`. `muse_enabled=True`는 기존대로 유지한다. 두 환경 설정 중 하나라도 명시적으로 false이면 auto 후보에서 제외된다. 코드 래치가 false일 때 환경 설정으로 우회할 수 없는 계약도 유지한다.
- 확인된 `muse-spark-1.3`만 general의 Sonnet, routine의 Flash와 같은 첫 단계에 후보로 참여한다. 기존 측정 시각·scope·모델별 quota·Run당 여유 비교를 그대로 쓴다.
- 회사 scope, design/verification/reasoning, GPT·Claude 독립 검증 조건은 유지한다. 미관측·stale quota는 여유로 간주하지 않는다. 결제·overage·native sandbox·보호 훅·인증 저장소는 변경하지 않는다.
- 완료 보고의 고정 Gemini Flash 문구를 실제 `run.engine/run.model` 표시로 고쳤다. Muse와 Flash 양쪽 보고에 회귀 검사를 넣었다.

## 검증

격리 DB `ttakkari_muse_accept_20261018_7729`를 새로 생성했다. 운영 DB를 테스트 대상으로 쓰지 않았다.

```sh
TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttakkari_muse_accept_20261018_7729 PYTHONPATH=src .venv/bin/pytest -q --tb=short --timeout=120 tests/test_muse_api.py tests/test_muse_auto_routing.py tests/test_worker_routing.py tests/test_muse_usage_pump.py tests/test_orchestrator.py tests/test_antigravity_model_routing.py unit_tests/test_muse_adapter.py unit_tests/test_muse_guard.py unit_tests/test_muse_protocol.py unit_tests/test_muse_registration.py unit_tests/test_muse_usage.py unit_tests/test_muse_tui_usage.py
```

결과: **201 passed in 34.31s**. 기본 auto 선택, 명시적 opt-out, 미수락 래치, quota 소진·stale fallback, 개인/회사 scope, 독립 검증 상태, 기존 엔진·오케스트레이터 및 Muse 어댑터/guard/protocol/usage 회귀를 포함한다. 실제 모델 실행이 아니라 격리 테스트의 계약 검사다.

TDD: 먼저 기본 활성화와 Muse 완료 보고의 기대값을 바꾸고 `3 failed, 26 passed`를 관찰했다. 실패는 닫힌 기본값·래치와 고정 Flash 보고 문구였다. 이후 최소 구현을 적용했다. 확대 검사에서 발견한 기존 정책 기대값 두 곳도 새 수락 상태에 맞췄다.

- 변경 Python 파일 `ruff check`: All checks passed.
- `git diff --check`: exit 0.
- `py_compile`: passed. Python pitfall scanner: 0 findings.
- `ruff format --check`는 기존 6개 파일 모두 형식 차이를 보고했다. 각 파일의 변경 전 HEAD 내용도 동일하게 exit 1임을 확인했으며 전체 포맷 변경은 하지 않았다.
- mypy 미실행: 프로젝트 venv에 미설치. 문법 검사로 보완했으며 타입 검사 통과를 주장하지 않는다.
- 개발 체크아웃에서 유효 설정 출력은 `muse_enabled=True`, `muse_auto_routing=True`, `effective_auto=True`. 운영 적용 여부는 배포 후 별도 확인한다.
