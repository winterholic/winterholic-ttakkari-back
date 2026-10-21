# 2026-10-21 에이전트 전역 중지 보강 (ADR 0067)

## 배경
새벽 4시 "에이전트 전부 중지"에서 AIP 작업 세션만 "다른 세션이 끼어들 수 없다"며 남았다. (1) 재배포 타이머 유실은 f086be1(ScheduledAction)이 다뤘고, 이번에는 (2)와 주변 구멍을 고쳤다.

## 확인된 원인
- `stop_worker` 는 호출한 오케스트레이터 소유 작업자만 찾는다.
- 기존 전역 중지는 `manager.cancel` 만 불러, lease 도 프로세스도 없는 running Run 과 재개 대기 interrupted Run 을 놓쳤다.
- 취소된 작업자의 보고 Run 이 부모 오케스트레이터를 깨워 새 작업을 만드는 경로가 있었다(이전 중단 시도에서 발견한 것을 재현·차단).

## 변경
- `runs/agent_control.py` 신규: `stop_all_agents`, 범위 필터, 보고 억제 표식, 구세대 보고 폐기, 보고 스윕.
- `POST /api/orchestrator/stop-all`, `POST /api/orchestrator/agents/{session_id}/stop`, MCP `stop_agents`. 일반 `stop_worker` 제한은 유지.
- `/api/system/stop-all`, 예약 `stop_all` 이 같은 서비스를 사용. 예약 payload 범위는 예약한 오케스트레이터 풀로 고정.
- `manager.dispatch` 에 구세대가 만든 취소 보고 폐기 가드.
- `resume_interrupted` 가 미집행 cancel 명령을 회수하고 재개 대신 취소로 마감(`_consume_pending_cancel`). 소유 세대 사망 후 부활 경로 차단.
- 스키마 변경 없음. alembic head `0059_collector_intake`, `deploy/generation-contract.json` 불변.

## 검증
- `tests/test_agent_stop_all.py` 14건. 수정 전 코드에서 12건 실패, 1건 통과(예약 stop_all). 수정 후 14건 통과(배포 게이트에서 확인).
- 재현 3종: 다른 오케스트레이터 소유 작업자, 다른 세대 소유 running Run(실제 두 세대 런타임 + cancel 명령 집행), 한도 대기 Run.

## 독립 검증 재작업 (기준 9afe3b2, 수정 구현 검증 대기)

### 시작 상태 대조
- 원본 `/Users/winterholic/development/projects/winterholic-ttakkari/back` 는 staged agent-control/ADR/test 삭제, unstaged collector 및 W1 작업, `main...origin/main [behind 1]` 이 섞여 있었다. 원본에는 쓰지 않았다.
- 원격 `origin/main` 은 `1bf9097a8fa2a86631b7e654c27524d066c225f3`; 기준 SHA 다음의 TTS API 문서·migration 포맷 커밋만 확인했다. 수정은 기준 SHA의 새 분리 worktree `/private/tmp/back-stop-rework-20261021` 에서 했다.
- 기존 `/private/tmp/back-stop-f086` 는 기준 SHA와 같은 전용 브랜치였고, `.muse-runtime/node_modules` 미추적 1건만 있어 재사용하지 않았다. `/.../back-agent-control` 은 별도 미커밋 기능 변경이 있어 건드리지 않았다.
- `ps -axo ...` 는 Codex 명령 환경에서 `operation not permitted` 로 차단됐다. 호스트 `server_resources` 는 읽기 전용으로 확인했으며 실제 프로세스를 종료하지 않았다. 이전 예약 `2c6afdaa-619a-4a5e-9f74-1910cb69763a` 의 최종 상태는 `failed`, 사유는 `schema contract requires an idle migration/bootstrap; active work preserved` 였다.

### 감사 반례 보완
- lease 부재만으로 running 을 취소하지 않는다. 새 `pid_started_at` 과 PID/시작 시각/pgid/엔진 명령 확인이 모두 가능할 때만 프로세스 그룹에 신호를 보내며, 확인 불가·생존은 remaining/unstopped 로 둔다. 마이그레이션 `0062_agent_stop_fences` 로 시작 시각 컬럼을 추가했다.
- `AgentStopFence` scope row lock과 epoch snapshot을 queue, dispatch, stop에서 공유한다. stopping 동안 enqueue를 막고, finally dispatch와 `resume_interrupted` 도 fence를 확인한다. 오래된/epoch 미상 queued Run 은 이후 명시 재개 뒤에도 실행하지 않는다. 사용자의 직접 새 Run 제출만 stopping 종료 후 해당 scope를 다시 연다.
- orchestrator stop은 `AgentStopReceipt` 를 먼저 claiming 가능한 durable row로 쓰고 별도 DB session/task에서 집행한다. 호출 child 종료·HTTP disconnect 뒤 owner-auth receipt 목록/단건 API로 상태·결과를 다시 조회한다.
- 새 보고에는 원본 `stop_source_run_id` 를 실어 정확한 억제 marker ID를 조회한다. 원본 ID가 없는 구세대 보고에 한해서 worker+1h 호환 fallback을 남긴다.
- partial 결과는 response `status=partial`, Korean message, `remaining`/`unstopped` 사유 및 감사 로그로 드러낸다. 예약 action도 `ScheduledAction.status=partial` 로 저장하고 remaining 수를 로그로 남긴다.

### 검증 실행 및 제한
- 동적 DB 비의존 검사: `PYTHONPATH=src TTAKKARI_NO_ENV_FILE=1 TTAKKARI_ENV=dev TTAKKARI_DATABASE_URL=postgresql+asyncpg://test:test@127.0.0.1:1/isolated_test pytest --noconftest -q tests/test_agent_stop_contract_unit.py` → 1차 `4 passed in 0.30s`, 원격 main `1bf9097` 위 rebase 후 재실행 `4 passed in 0.33s`. 범위 분리, exact epoch 비교, 실제 inert fake child의 식별정보 부재 및 PID birth-time 불일치 시 신호 미전송을 확인했다.
- 정적 확인: `python3 -m compileall -q src/ttakkari tests/test_agent_stop_all.py tests/test_agent_stop_contract_unit.py alembic/versions/0062_agent_stop_fences_and_receipts.py` 성공. 변경 파일 `ruff check --fix ...` 는 2 import 순서만 정리했고, 이어서 non-fix `ruff check ...` 는 `All checks passed!` 를 반환했다. `git diff --check` 통과.
- migration graph read-only 확인: 저장소 `.venv` Python 으로 `ScriptDirectory.from_config(Config('alembic.ini'))` 실행 결과 `migration_heads=['0062_agent_stop_fences']`, `current_head=0062_agent_stop_fences`.
- DB 통합 검사 5개(lease 없는 running child, stop/enqueue 경합·dispatch·restart, caller 이후 receipt 조회, worker 새 report, 예약 partial)는 `tests/test_agent_stop_all.py` 에 추가했으나 미실행이다. 이 Run 환경은 `TTAKKARI_TEST_DB` 미설정이고 `/tmp/ttakkari-test-*` test cluster가 없었다. 이전 fixture의 initdb `shmget: No space left on device`를 같은 조건으로 재시도하지 않았다. 운영 DB도 사용하지 않았다.
- 교차세대 lease 실집행, pool/workspace·worker token 회귀, schedule catch-up·중복 방지의 현 수정 SHA 동적 결과는 미확인이다. 기존 테스트는 삭제/skip/기대값 완화하지 않았다.
- 모델 정책은 `engine=auto`, `task_type=general` 그대로 두었다. 전체 운영 중지 및 05:00 취소 예약에는 접근하지 않았다.
- 코드 변경 커밋은 원격 main `1bf9097` 위에 rebase 완료했고, rebase 뒤 단위 검사·lint·compile·migration head 검사를 재실행했다. 이 기록 갱신 뒤 푸시 및 수정 SHA gate 예약을 진행한다. DB 통합 재검증 전 판정을 통과로 올리지 않는다.

### 후속 상태 정정 (2026-10-21, 수정 SHA 독립 확인 계속)
- 별도 worktree의 현재 HEAD는 `32dce03e7ec186d60da2211cb4aeafcb7bd6e42f`이며 feature 브랜치 `fix/agent-global-stop-audit-followup-20261021`까지 원격 push 완료했다. `fe0386a2f0bada633ce9f6b34f8b9421c7c6eac8` 현재 원격 main 변경(collector/TTS/test harness)을 병합했으며 이 변경을 다시 구현하거나 원본 main에 덮어쓰지 않았다.
- 원본 worktree는 여전히 staged agent-control/ADR/test 삭제와 collector/W1 변경이 섞여 있고 `main...origin/main [behind 1]`이다. 이후에도 원본 worktree는 수정하지 않았다.
- 수정 SHA의 새 DB 비의존 재실행: 같은 격리 URL 인자와 `pytest --noconftest -q tests/test_agent_stop_contract_unit.py` → `4 passed in 0.42s`. Ruff는 기존 원본 저장소 `.venv/bin/ruff`로 변경 대상 파일을 확인해 `All checks passed!`; `compileall`, `git diff --check`도 통과했다. Alembic 조회 결과 `migration_heads=['0062_agent_stop_fences']`, `current_head=0062_agent_stop_fences`.
- 이번 worktree에는 `.venv`가 없어 최초 `.venv/bin/ruff` 호출은 `no such file or directory`로 lint를 실행하지 못했다. 이어서 기존 원본 저장소 venv의 ruff로 같은 변경 파일을 검사해 통과했으며 의존성 설치나 가상환경 변경은 하지 않았다.
- 배포 조회를 분리해 확인: 기준 SHA 예약 `2c6afdaa-619a-4a5e-9f74-1910cb69763a`는 `failed` (`schema contract requires an idle migration/bootstrap; active work preserved`). 다른 수정 `a042f509...` 예약 `d767a2cc-abcc-4f87-8309-6f0de67e4167`는 조회 시 `testing`이었다. 병행 부하를 피하려고 DB 통합 테스트/배포 게이트는 시작하지 않았다.
- current merge SHA를 원격 main에 push하려 한 명령 `git push origin HEAD:refs/heads/main`은 pre-push `scripts/verify_deploy_gate.sh --pre-push`의 `uv sync` 단계에서 실패했다. Codex 명령 도구 sandbox가 `/Users/winterholic/.cache/uv` 아래 `sdists-v9/.git` 열기를 `Operation not permitted (os error 1)`로 거부했고 gate는 `임시 디렉터리의 uv sync 실패(의존성 설치)`를 반환했다. 이는 해당 sandbox 실행 실패만 입증한다. `--no-verify`/skip 없이 실행할 수 있도록 임시 cache 경로로 재시도할지는 다른 배포 testing 종료 뒤 판단한다.
- DB 통합 반례 5개와 기존 교차세대 실집행·pool/workspace·worker token·catch-up·중복방지의 수정 SHA 동적 결과는 계속 미검증이다. 같은 `initdb` shmget 실패 조건을 재시도하지 않았고 실제 운영 DB/전체 중지/취소된 05:00 예약을 건드리지 않았다. 전역 중지 실행 보증은 하지 않는다.
- 현재 merge SHA의 main push와 deploy reservation은 아직 미완료다. feature branch push 성공은 main 반영이나 배포 성공을 뜻하지 않는다.
