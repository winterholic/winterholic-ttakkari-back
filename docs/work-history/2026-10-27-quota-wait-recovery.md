# 한도 회복 후 오래된 대기 자동 재평가

사용자 요청: 초기화가 10월 30일인 대기 작업이라도 적합한 계정이 회복되면 그 계정으로 이어간다.
첨부는 `OpenCode 오류 진단 교차검증` 작업이며 다른 기획 소유다.

## 직접 관측

- `account_usage`: 개인 Codex 두 계정이 신선한 측정에서 사용 가능, 모델별 여유 73/74%.
  개인 Antigravity 한 계정의 Claude 계열 여유 100%. 다른 quota pool은 별개로 유지한다.
- `read_session(ecc176d9-1bdd-4f04-b6bf-6432477e51ee)`: 기존 독립 검증 지시가 queued.
  현재 도구는 이 작업의 strict_engine, Run ID, 최근 이벤트, lease를 반환하지 않는다.
  타 기획 소유 `get_worker`는 404라 그 경로로 변경하지 않았다.
- 지원 `runtime_report` 8455df78: serving `e4899781e95f3350314b82b90f940914d65ef27d`,
  active generation `93f5d24a-df0f-42b7-a949-767a3ff377b0`, local 실행 모드.
  재평가 코드 자체는 운영 버전에 존재한다. 이 첨부 작업의 정확한 제외 원인은 아직 미확인이다.

## 재현한 누락과 교정

기존 `_reconsider_quota_waits`는 마지막 이벤트가 `run.waiting`일 때만 검사한다.
기존 대기 뒤에 진단 log 또는 usage가 기록된 모양을 주입된 격리 PostgreSQL에서 재현했다.
사용 가능한 실제 라우팅 후보가 있어도 4일 뒤 resume_after를 유지해 `2 failed, 1 passed in 0.76s`.
음성 대조의 새 pool retry 상태는 계속 대기했다.

최근 **수명주기 이벤트**(`run.waiting`, `run.status`)를 기준으로 재평가하도록 좁혔다.
원래 같은 Run, resume_handoff, 요청 모델, 개인/회사 범위, strict_engine 및 lease 경계를 유지한다.
기존 수집기/active coordinator가 호출하며 공급자 추가 호출이나 별도 타이머를 만들지 않는다.

운영 원인 관측을 위해 `read_session`에 routing engine/model/task_type/strict_engine/pool과
Run ID/엔진/모델/resume_after/마지막 이벤트 종류/lease 유무만 추가했다.
최근 이벤트 payload·진행 인계 본문·계정 비밀은 추가 반환하지 않는다. 기존 회사 범위 차단을 유지한다.

## 함께 해결한 배포 검사

main `1083a0ce`의 배포 `c5fb3942`는 `FakeProfile.model` 누락으로 3244 pass 후 실패했다.
동일 검사 단독 `1 failed in 0.58s`를 확인하고, 더미를 실제 `profiles.Profile`로 교체했다.
운영 모델 혼잡 판정은 수정하지 않았다.

```sh
PYTHONPATH=$PWD/src .venv/bin/python -m pytest -q \
  tests/test_worker_quota_recovery.py tests/test_worker_routing.py \
  tests/test_scheduled_actions.py tests/test_agent_scope_auth.py \
  tests/test_usage_collector.py tests/test_codex_capacity.py --tb=short
```

`121 passed in 16.10s`. 자동 후보 선택·기존 진행/대화·세대 변경·정확히 한 번 재개,
strict/all-capped/cancelled/backoff/owned 제외, 메타데이터의 비공개 본문 누출 방지와 범위 차단 포함.
변경 Python 5파일 Ruff: `All checks passed!`; `git diff --check`: exit0.
초기 `read_session` 진단 테스트는 `KeyError: routing`으로 RED 확인 후 교정했다.

미검증: 첨부된 운영 대기의 정확한 원인 및 재개, 새 revision 전체 release gate/활성화.
배포 후 보강된 read_session으로 해당 Run/제약을 확인하고 같은 작업의 실행 결과까지 추적한다.
원 V2의 P0/remote/복원·실모델/MCP 요구는 별도로 남겨 둔다.
