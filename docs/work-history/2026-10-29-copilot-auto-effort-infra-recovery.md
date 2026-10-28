# Copilot `auto` effort 경계 교정 — INFRA 후속

## 범위와 운영 상태

- 작업 소유자: 이 INFRA 후속 세션 (backend `main` 통합·배포 단일 소유).
- 격리 checkout: `/Users/winterholic/development/ttakkari-infra-integ-20261029`, branch `integ/infra-permissions-orphan-stop-20261029`.
- 시작 기준 HEAD와 `origin/main`: `0f6f0a7730d4aebdd866e2c8abff2262bc5f0449`. `git fetch origin` 뒤에도 일치했다.
- 2026-10-29 이번 증거 기록 전 bounded snapshot: local HEAD `4e5936215c6f46e0b1b1c13101d8970516aaef10`, clean, origin/main=`aa305cb768433e251b2546e4a5d0a11e67801f2e`. Copilot·Research 누적 코드는 격리 checkout에만 있고 push/deploy되지 않았다. KB `d3ac880b8c9769070bb0e4e89d5d8974f029ed84`는 독립 코드 검증 PASS 후속을 받았으나 PostgreSQL 통합 gate 전이므로 아직 통합하지 않았다.
- 최신 배포 조회: `abcb3f7d-a291-4789-9117-ec5c08220eba` revision `12347b3f24f56f2a7c029e8ca3f394495dd240c8`는 `draining`; 기존 `e7d441b9-ca41-4faa-a76e-63a7438a3c6e` revision `0f6f0a7730d4aebdd866e2c8abff2262bc5f0449`는 `failed`(`skip_tests=false`). 배포 pytest는 `tests/test_collection_agent.py::test_status_returns_failure_type_without_payload_or_credentials`에서 `ttakkari.api.operations.personal` import error로 실패했다(LOG-290). 별도 revision `c04c5a37` 예약 `971a0529-c5e1-4145-ac59-59c25139de0f`는 현재 `testing`; 이를 이 수정의 예약으로 간주하지 않는다.
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
- 독립 d9 재검증은 KB 교정 후보 `fix/kb-context-boundary-20261029` HEAD `d3ac880b8c9769070bb0e4e89d5d8974f029ed84`의 코드 경계에 한해 PASSED이며, 기획이 원 worker에 `verification_outcome=passed`를 기록했다. 승인 repo venv의 `PYTHONPATH=src .../python -m pytest -q unit_tests/test_kb_research_boundary_unit.py` 결과는 `4 passed in 0.32s`. 실제 PostgreSQL `tests/test_kb_research_boundary.py`는 미실행이다. canonical/covered alias 테스트는 목록에서 alias를 숨기는 것만 검사하고, alias 직접 ID 열람·인용/hash·bytes·연구 resume 보존 조합은 아직 검증되지 않았다. 기존 `56d48793`의 FAILED는 구 버전 결함 기록으로 계속 유효하며 교정본에 대해서만 통합 보류를 해제한다.

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

## 2026-10-29 KB 독립 재검증 후속 및 현재 게이트 상태

- 사용자가 전달한 최신 독립 증거: GPT Luna worker `d9a009a9-0879-4585-ad7d-586725fa8da5`, Run `d51be32c`의 기록 `/Users/winterholic/development/projects/winterholic-ttakkari/back-kb-p2-review-d3ac880/docs/work-history/2026-10-29-kb-p2-independent-reverification.md`. 보고된 명령은 `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q unit_tests/test_kb_research_boundary_unit.py`; 실제 출력 `4 passed in 0.32s`. 독립 verifier는 code scope만 PASSED 했고 기획에서 outcome passed를 기록했다.
- 독립 verifier가 열거한 미검증: PostgreSQL `tests/test_kb_research_boundary.py`; direct alias ID·quote/hash·bytes·resume 조합; list/search/current_only=False/summary/embedding mixed-history DB readback; 운영 데이터 분포와 배포 후 readback. 후보를 정상 DB gate 전에는 통합하지 않는다.
- 이번 bounded readback: `git status --short --branch` → `## integ/infra-permissions-orphan-stop-20261029`; `git rev-parse HEAD` → `4e5936215c6f46e0b1b1c13101d8970516aaef10`; `git ls-remote origin refs/heads/main` → `aa305cb768433e251b2546e4a5d0a11e67801f2e refs/heads/main`. `ops_list_deploys` → `971a0529-c5e1-4145-ac59-59c25139de0f @ c04c5a37 testing`, `e7d441b9-ca41-4faa-a76e-63a7438a3c6e @ 0f6f0a77 failed/LOG-290`. 새 reservation은 없다.
- 이번 Run에서는 알려진 Antigravity loopback EPERM 및 보호 uv cache를 재시도하거나 우회하지 않았고 테스트/게이트를 실행하지 않았다. 독립 deployment gate는 게시된 SHA에 대한 `request_backend_deploy` 뒤에 사용할 수 있지만, 현재 SHA는 remote에 없다. 현재 tool catalog에 별도 CI workflow-dispatch는 없다. 기존 hook gate의 보안 요구를 충족할 실행 결과가 없으므로 KB 통합·push·누적 full-SHA 배포예약은 보류다.
- 원래 AIP worker의 최근 `read_session`은 `OSError(24, 'Too many open files')` 결과를 포함하지만 현재 lifecycle은 확인하지 못했다. GAME worker의 최신 `read_session`에는 결과가 없었다. 두 `get_worker` 조회는 404 `이 작업 세션의 하위 세션이 아닙니다`였으므로 상태 확인이 아니다. 별도 재배정/복제는 하지 않았다.

## 2026-10-29 공통 API·게시 게이트 복구 후속

- 사용자가 다시 지정한 격리 checkout을 확인했다. 시작 때 HEAD는 `80f1143014c7d9a80fffbba6c5b78ae27abf07fd`로 이전 보고의 `f630c610b7f5cf2245332cdd73f87a73c7739101` 이후 Copilot·Research 통합과 문서 교정 커밋까지 포함했고, clean이었다. `git fetch origin` 후 `origin/main=aa305cb768433e251b2546e4a5d0a11e67801f2e`로 기존 계약과 같았다.
- `backend_deploy_status(e7d441b9-ca41-4faa-a76e-63a7438a3c6e)`와 `ops_deploy_log(summary)`를 직접 읽었다. e7은 revision `0f6f0a7730d4aebdd866e2c8abff2262bc5f0449`, status `failed`, stage `pytest`; 로그는 지정 실패에서 `1 failed, 837 passed, 2 skipped, 3 warnings, 6 subtests passed`였다. `ops_list_deploys` 및 직접 상태 조회에서 별도 `971a0529-c5e1-4145-ac59-59c25139de0f @ c04c5a37a6750f9fafe7641a62afba4244d499b8`는 `testing`이므로 이 변경의 예약으로 취급하지 않는다.
- `collection_missions.py`의 status/retry가 `ttakkari.api.operations.personal`을 import했지만 해당 모듈의 세션 권한 함수명은 `caller`다. `rg` 결과 수집 API의 두 오래된 import가 유일한 불일치였고, `api/deployments.py`의 같은 이름 `personal` 구현은 별도 유효 구현이다.
- RED: `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_collection_agent.py::test_status_returns_failure_type_without_payload_or_credentials tests/test_collection_agent.py::test_retry_starts_one_new_run_and_duplicate_request_cannot_cancel_it tests/test_collection_agent.py::test_agent_recovery_is_authenticated_and_owner_bound` → 세 실제 API 테스트가 모두 `ImportError: cannot import name 'personal' from 'ttakkari.api.operations'`로 실패했다.
- 최소 교정은 두 수집 API가 `operations.caller(sid, db)`를 사용하도록 한 것이다. GREEN 명령 `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_collection_agent.py` → `6 passed in 1.16s`, `db reset: 6 selective (avg 10.7/120 tables), 0 forced full, 0 fell back to full`. 여기에는 status/retry, 무인증, 타 tenant 거절, owner/work session이 포함된다. Ruff 두 변경 경로와 `git diff --check`도 통과했다. `rg`는 해당 낡은 import를 찾지 못했고 별도 deployments 구현만 남았다.
- 공식 push 게이트는 계속 `scripts/verify_deploy_gate.sh`의 `git archive` 임시 snapshot과 `uv sync --frozen`을 쓴다. `uv sync --help`에서 공식 `--cache-dir` / `UV_CACHE_DIR` 지원을 확인했다. 이어서 허용된 이 Run outbox 아래 전용 캐시 경로를 지정해 통상 `git push origin HEAD:main` pre-push hook을 실행한다. 보호 `~/.cache/uv`는 재접근하지 않으며 게이트, snapshot, frozen sync, 검사 선택/실패 기준은 바꾸지 않는다. 이 기록 시점에 공식 게이트·게시·배포 예약은 아직 미실행이다.
- 다음: 이 수정과 기록을 커밋하고, 위 공식 게이트를 통과한 정상 main push의 원격 full SHA를 확인한 뒤 그 SHA만 새 `request_backend_deploy`에 예약한다. 예약 후 폴링하지 않는다. 배포 후 collection API readback, Copilot stage 1, runtime/MCP/resume 및 다른 미완 복구 목표는 별도 미검증으로 유지한다.
