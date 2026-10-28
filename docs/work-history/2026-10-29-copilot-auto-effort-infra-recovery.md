# Copilot `auto` effort 경계 교정 — INFRA 후속

## 범위와 운영 상태

- 작업 소유자: 이 INFRA 후속 세션 (backend `main` 통합·배포 단일 소유).
- 격리 checkout: `/Users/winterholic/development/ttakkari-infra-integ-20261029`, branch `integ/infra-permissions-orphan-stop-20261029`.
- 시작 기준 HEAD와 `origin/main`: `0f6f0a7730d4aebdd866e2c8abff2262bc5f0449`. `git fetch origin` 뒤에도 일치했다.
- 최신 배포 조회: `abcb3f7d-a291-4789-9117-ec5c08220eba` revision `12347b3f24f56f2a7c029e8ca3f394495dd240c8`는 `draining`; 기존 `e7d441b9-ca41-4faa-a76e-63a7438a3c6e` revision `0f6f0a7730d4aebdd866e2c8abff2262bc5f0449`는 처음 확인한 `requested`에서 이후 `failed`로 종료됐다(`skip_tests=false`). 배포 pytest는 `tests/test_collection_agent.py::test_status_returns_failure_type_without_payload_or_credentials`에서 `ttakkari.api.operations.personal` import error로 실패했다(LOG-290). `ops_list_deploys`에는 별도 소유 여부 미확인인 revision `c04c5a37`의 `971a0529-c5e1-4145-ac59-59c25139de0f`가 `testing`으로 표시된다. 이를 이 수정의 예약으로 간주하지 않았다. 새 수정은 로컬 커밋됐지만 미게시·미배포이며 새 예약은 없다.
- 공유 dirty checkout `projects/winterholic-ttakkari/back`는 수정하지 않았다.

## 장애 원인 근거

- `ops_search_logs(query="does not support reasoning effort", since_hours=2)`가 release `12347b3f`에서 총 4건을 반환했다: Copilot `model=auto` + `medium` 1건(LOG-287), `high` 3건(LOG-288). 상세 issue에는 profile `GitHub Copilot CLI`, exit code 1, engine `copilot`, model `auto`가 있다.
- 값 흐름: [`src/ttakkari/api/orchestrator.py`](../../src/ttakkari/api/orchestrator.py)의 worker 접수는 요청 effort 또는 `worker_default_models[copilot]`을 선택한다. [`src/ttakkari/config.py`](../../src/ttakkari/config.py)에는 worker 기본 `auto/medium`, orchestrator fallback `auto/high`가 있다. [`src/ttakkari/runs/manager.py`](../../src/ttakkari/runs/manager.py)의 비-orchestrator Copilot 분기는 저장한 effort를 모델 capability 검사 없이 전달한다. [`src/ttakkari/agents/copilot.py`](../../src/ttakkari/agents/copilot.py)는 최종 모델과 상관없이 유효 effort면 CLI `--reasoning-effort`를 추가한다.
- 설치 CLI `/opt/homebrew/bin/copilot`은 `1.0.103`; 일반 help에 reasoning-effort 옵션이 있다. `auto` 조합의 거절은 운영 로그가 직접 입증한다. 일반 help만으로 auto 모델 지원을 추정하지 않았다.

## 변경 및 검증

- 최소 코드 변경: adapter에서 최종 `model == "auto"`일 때만 reasoning-effort 인자를 생략한다. 이름 있는 모델의 기존 effort 인자는 보존한다. 설정 기본값, 라우팅 정책, 계정 상태는 바꾸지 않았다.
- 회귀는 먼저 작성했다. 실행 interpreter는 승인된 기존 repo 환경 `/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python`; 설치·수정하지 않았다. `PYTHONPATH=src`이며 adapter import 경로를 출력해 현재 checkout source임을 확인했다.
- RED 명령: `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -c 'import pytest, ttakkari.agents.copilot as adapter; print(adapter.__file__); raise SystemExit(pytest.main(["-q", "unit_tests/test_copilot_adapter.py", "-k", "auto_model_omits_reasoning_effort or default_auto_model_omits_default_reasoning_effort or named_model_keeps_requested_reasoning_effort"]))'`
- RED 출력: adapter 경로는 현재 worktree `src/ttakkari/agents/copilot.py`; `3 failed, 2 passed, 8 deselected`. auto+medium/high 및 기본 auto+medium에서 인자가 포함되는 실패였고 named model 보존 2개는 통과했다.
- 오케스트레이터 fallback 자체를 연결한 회귀도 추가했다: `_launch_model()`이 `auto/high`를 반환한 뒤 adapter가 effort를 빼야 한다. guard를 제거한 RED → `1 failed, 11 deselected`; 실패한 argv에는 `--model auto --reasoning-effort high`가 있었다.
- GREEN 전체 adapter 단위: `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_copilot_adapter.py` → `12 passed, 2 subtests passed in 0.25s`.
- 자동 라우팅 통합: `PYTHONPATH=src <repo-.venv-python> -m pytest -q tests/test_copilot_integration.py -k auto_worker_uses_monthly` → `1 passed, 11 deselected in 0.79s`; `db reset: 1 selective`, fixture가 운영 provider를 호출하지 않는 통합이다.
- 계약 묶음: `PYTHONPATH=src <repo-.venv-python> -m pytest -q --timeout=120 -p no:cacheprovider --tb=short -rf -m contract` → `84 passed, 5936 deselected in 11.00s`; DB reset 84개 선택, 강제 전체 초기화/폴백 0.
- 정적 검사: repo `.venv/bin/python -m ruff check src/ttakkari/agents/copilot.py unit_tests/test_copilot_adapter.py` → `All checks passed!`; `git diff --check` 종료 0.
- 독립검토 worker `4f50dd66-7d99-4c79-a5bb-5420671ea91b` (Codex GPT-6 Luna, verification)의 첫 verdict는 adapter 경계를 적절하다고 보고하고 orchestrator fallback 직접 테스트 부재를 지적했다. 그 경로를 고정한 위 회귀를 추가한 뒤 재검토를 받아, 해당 범위 한정이 해소됐다고 판정했다. 재검토자는 코드를 직접 읽고 `git diff --check`를 확인했지만 재실행은 하지 않았다. `12 passed`는 이 세션의 실행 증거다. 실제 orchestrator 프로필 선택/운영 복구는 독립검토 범위 밖이다.
- 첫 시도 `uv run pytest ...`는 Codex workspace-write command 환경에서 `/Users/winterholic/.cache/uv` 초기화 중 `Operation not permitted`로 막혔다. `python3 -m pytest`에는 system Python의 pytest가 없었다. 보호 cache 재접근/경로 우회는 하지 않았다. 사용자 지정 기존 repo interpreter로 위 검증을 수행했다.
- repository `scripts/verify_deploy_gate.sh`는 임시 checkout에서 `uv sync --frozen`으로 시작한다. 알려진 보호 cache denial을 재시도하거나 gate skip하지 않았다. 따라서 공식 pre-push gate 및 remote push/deploy는 아직 미실행이다. Push hook 설치 상태는 `back/.git/hooks/pre-push`에서 확인했다.

## Research 후보와 통합 경계

- 독립 e3 검토 결과(기존 e3 session `e3bdd110-703e-4029-9000-0ec444f6f062`, 두 보고 turn): Research 후보 `3abd497a7c93132c26bb8bedb4c86756f9195811`에서 `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest unit_tests/test_research_rules.py -k 'test_catalog_selection_request_model_accepts_catalog_source_and_rejects_invalid or test_catalog_selection_novel_candidate_without_cost_requires_review or test_catalog_selection_direct_failures_distinguish_applicable_and_conflicting_conditions or test_schedule_quota_wait_profile_contract_and_model_capacity_key' -q`는 `4 passed, 19 deselected in 0.80s`; `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_scheduled_actions.py::test_quota_wait_persists_resume_after_across_restart`는 `1 passed in 0.85s`; INFRA 선택 반례 6개는 `6 passed, 12 deselected in 4.62s`로 보고됐다. 통합·운영은 미검증이며 현재 branch는 origin/main보다 9 ahead / 1 behind라 main에 합치지 않았다.
- Research 후보 Git object를 직접 확인해 오래된 work-history의 세 SHA 오기를 교정했다: base `f95cef4bf92c042437bc37d6a11969d50b7ea5b5`, regression `dd747e66e33292a39b9e59a3d06329d8114dc1f8`, merge `277f71e3045b26b992626f02a08cd78e448e766a`. 문서/evidence 전용 `5bf88bddec0e0002c0f6b5cccd24c96ff74585e5`는 실행 코드 변경이 아니다. 상세 교정은 `2026-10-29-research-platform-v2-integration.md`에 기록했다. 해당 문서의 구 INFRA 441 소유자 표기는 최신 계약에 따라 `fcb55b7d`로 정정했다.
- 독립 d9 재검증은 KB 후보 `fix/kb-context-boundary-20261029` HEAD `d3ac880b8c9769070bb0e4e89d5d8974f029ed84`의 코드 경계에 한해 PASS. `/Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_kb_research_boundary_unit.py` 결과는 `4 passed in 0.32s`였다. 실제 PostgreSQL 회귀 및 배포 readback은 미실행이고 직접 ID/연구 resume의 DB 동작은 확인 범위 밖이다. 정상 DB 게이트 전에는 통합하지 않는다.

## 완료 판정과 다음 단계

- 코드 한정: 현재 diff의 회귀/라우팅/계약 테스트는 통과했고 변경 경계 독립검토도 통과했다. 공식 pre-push gate, 게시 및 운영 수정동작은 미검증이다. 이 결과를 LOG-287/288의 운영 복구 완료로 확대하지 않는다.
- 다음 단계: 알려진 workspace-write denial `/Users/winterholic/.cache/uv`를 재접근·우회하지 않고 수행할 승인된 정상 push/CI 게이트 경로를 찾는다. Plugin search에서 GitHub connector를 찾았지만 미설치 상태라 연결 제안만 했고, 아직 Actions/workflow 실행은 확인하지 않았다. pre-push 훅을 bypass하거나 gate를 생략하지 않는다. 게시된 full SHA만 새 `request_backend_deploy`에 예약한다. e7은 이미 `failed`이므로 재사용/중복 예약하지 않고, `c04c5a37`의 현재 testing은 다른 예약이다. 배포된 `12347b3f`의 운영 동작도 아직 별도로 재검증하지 않았다.

## 2026-10-29 누적 통합 후속

- 현재 owner는 `fcb55b7d`이며 이 단일 격리 checkout에서 backend `main` 통합·게시·배포를 계속 맡는다. 구 owner `441ae34d`는 재개하지 않는다.
- 최신 fetch 결과 `origin/main=aa305cb768433e251b2546e4a5d0a11e67801f2e`; 이는 `c04c5a37a6750f9fafe7641a62afba4244d499b8`와 `0f6f0a7730d4aebdd866e2c8abff2262bc5f0449`를 포함한다. 먼저 이를 기존 Copilot 커밋 `f630c610b7f5cf2245332cdd73f87a73c7739101`과 병합해 `b6195cf56d376cffb301517b01b483d66709b240`를 만들었다.
- Research 후보 `3abd497a7c93132c26bb8bedb4c86756f9195811`를 그 위에 병합해 `1336252c73322d1700e5820cd12b91c29899df66`를 만들었다. 두 merge 모두 `ort`로 자동 완료되어 충돌이 없었다. Research의 코드·회귀·문서/evidence 차이를 누적했으며, 원래의 candidate SHA는 `docs/work-history/2026-10-29-research-platform-v2-integration.md`에서 실제 Git 객체에 맞게 정정했다.
- 보존한 실행 증거(테스트를 다시 돌리지 않음): Research focused 4개 `4 passed, 19 deselected in 0.80s`; schedule quota restart `1 passed in 0.85s`; INFRA stop/cancel 반례 `6 passed, 12 deselected in 4.62s`. 이 결과들은 지정된 candidate revisions에 대한 코드 범위 결과다.
- KB 후보 `d3ac880b8c9769070bb0e4e89d5d8974f029ed84`의 독립 unit 결과 `4 passed in 0.32s`는 코드 범위만 확인한다. PostgreSQL boundary 회귀 및 운영 readback이 완료되지 않아 이 후보는 현재 통합에 포함하지 않았다.
- 남은 gate: Research 전체 DB catalog 4 회귀/runtime 접수, Copilot 실경로 stage1 및 후속 MCP/resume/auto-routing 분리 확인, 공식 pre-push/full deploy gate, 게시, 누적 SHA 배포예약과 readback. frontend 577 및 전체 coverage도 미완이다.
- 기존 work-history의 공식 pre-push 경로는 `scripts/verify_deploy_gate.sh`→`uv sync --frozen`이며, 이전 Codex command sandbox에서 `/Users/winterholic/.cache/uv` 접근 `EPERM`이 기록돼 있다. 이 후속에서는 같은 보호 cache에 재접근하거나 gate를 생략하지 않았다. 따라서 현재 merge SHA는 아직 로컬이며, push와 배포 예약은 실행하지 않았다.
