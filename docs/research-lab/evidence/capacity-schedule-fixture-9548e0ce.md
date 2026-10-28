# 누적 배포의 예약 시험 프로필 계약 교정

2026-10-26 리드 Codex 직접. 기준9548e0ce, 원격main1083a0ce 병합 후.

runtime_report34b7e693은 활성e4899781/PID57410, agent lease4/process group4를
반환했다. 직전 d5bf 세대는offline/lease0/group0이다. 이는 현재 세대 관측이며
연구 A 전후 RuntimeLease 비교 또는 새 카탈로그 활성 인수가 아니다.
RuntimeLease는 agent runs.id FK이고 research ownership은 별도 기존flock이다.
두 계약을 같은 것으로 취급하거나 연구용 RuntimeLease 추가 구현을 요구하지 않는다.

최신 누적controller c5fb3942(1083a0ce)는3244 passed/17 skipped/2 xfailed 후
test_scheduled_actions의 quota_wait_persists_resume_after_across_restart에서 실패했다.
manager._wait_for_reset의 새 model_capacity_key(p.engine,p.model) 조회가
model 필드 없는 시험용 FakeProfile에 닿아 AttributeError를 냈다.
실제 profiles.Profile은 model: str|None=None 필드를 이미 가진다. 따라서 제품에
getattr 우회나 예외 삼킴을 추가하지 않고 시험 프로필을 실제 dataclass로 바꿨다.
재개시각 영속 저장과 restart 뒤 re-arm의 기존 단언은 유지했다.

```text
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_scheduled_actions.py::test_quota_wait_persists_resume_after_across_restart -q --timeout=30 --tb=short
1 failed in0.66s: AttributeError: FakeProfile has no attribute model
$ PYTHONPATH=src .venv/bin/python -m pytest tests/test_scheduled_actions.py tests/test_codex_capacity.py unit_tests/test_model_capacity.py -q --timeout=30 --tb=short
33 passed in7.45s
$ .venv/bin/ruff check tests/test_scheduled_actions.py
All checks passed!
$ git diff --check
exit0
```

runtime와controller 원문은runtime-and-deploy-9548e0ce.json이다. 이 교정이 전체
controller gate를 통과했다는 뜻은 아니다. 앞선b644 Docker mount 오류는 다른
실패이며, 같은 기록소스 API 첫 빌드1PASS만으로 해결했다고 판정하지 않는다.
카탈로그 구현/67검사·front94 이전범위·현재4검사/build·필수84/4는
catalog-selection-5a0b4ed1.md/json의 각 소스hash·실행 경계를 따른다.

개인GitHub 인증은 이 Codex 환경에서 계정전환keyring161, 명령단위개인username
credential은Device not configured다. 두 저장소 변경은로컬clean커밋에 보존하고
원격게시·신규배포예약은 미실행으로 남긴다. 인증 상태 변화 없이 반복하지 않는다.
R1~16/PMS 원목표는 계속 미완이며 새worker를 만들지 않았다.
