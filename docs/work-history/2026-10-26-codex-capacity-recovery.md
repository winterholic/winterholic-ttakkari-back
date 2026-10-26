# Codex 모델 혼잡 자동 복구

- 사용자 요청: `Selected model is at capacity. Please try a different model.` 발생 시 진행을 보존하고 다른 엔진 세션에서 자동으로 복구.
- 기준 코드: `origin/main` `5a0b4ed1`. 기존 기본 작업 트리의 미커밋 변경과 분리한 `fix/codex-capacity-recovery-20261026`에서 작업.
- 재현: 가짜 Codex CLI가 진행 메시지를 출력한 뒤 해당 오류로 실패하도록 구성. 변경 전 실제 Run 관리자 통합 시험에서 `failed != succeeded`로 실패 확인.

## 변경

- `profiles.py`: 정확한 혼잡 문구를 계정 quota와 구분. 엔진·모델 단위 UUID cooldown 키를 기존 영속 저장소에 보존.
- `manager.py`: 오류·stderr·실패 결과에서 감지하면 `engine.capacity`를 남기고 120초 동안 같은 모델을 모든 계정 후보에서 제외. 기존 `_attempt_handoff`로 대화·진행·도구 결과·중복 실행 방지 지침을 전달.
- 후보가 없으면 `resume_handoff`를 저장하고 같은 Run을 자동 대기·재개. `strict_engine`, 개인/회사 범위, 취소, 최대 대기 횟수를 유지. 혼잡만으로 Qwen 사용량 소진 비상 경로를 실행하지 않음.
- `run.waiting.reason=model_capacity`, 혼잡 대기 푸시 문구와 API·ADR 문서를 갱신.

## 실행 증거

환경: Codex 명령 샌드박스, 소스 작업 트리, 가짜 엔진 프로세스와 전용 임시 PostgreSQL. 운영 DB·실제 공급자 요청은 사용하지 않음.

```text
PYTHONPATH=src ../back/.venv/bin/python -m pytest -q tests/test_codex_capacity.py tests/test_orchestrator.py tests/test_worker_routing.py tests/test_accounts.py unit_tests/test_model_capacity.py --tb=short
148 passed, 1 warning in 29.78s
```

- 경고 1건은 기존 FastAPI/Starlette 422 상수 deprecation.
- 새 기능 시험: stdout/stderr 혼잡 → 다른 엔진 대화 복구, 이전 대화·진행 인계, 같은 모델의 다른 계정 반복 제외, 후속 지시의 혼잡 회피, 다른 Codex 모델 허용, 영속 cooldown 재로드, 대체 경로 없는 대기, 자동 재개, 최대 대기 상한, quota 비상 라우팅과 분리.
- 변경 Python 파일 Ruff 검사: `All checks passed!`.
- Python pitfall scan은 기존 `profiles.py`의 `choose()` 기본 인자 함수 호출 1건을 지적. 이번 변경 영역 밖이며 범위 확대 없이 유지.

## 미검증 범위

- 공급자의 실제 모델 혼잡을 강제로 발생시키는 운영 검증은 미실행. 가짜 CLI 출력과 실제 Run 관리 경로의 검증을 운영 증거로 확대하지 않음.
- 이 기록 작성 시 배포 전. 배포 예약 접수와 활성화 성공은 별도 상태로 확인해야 함.
