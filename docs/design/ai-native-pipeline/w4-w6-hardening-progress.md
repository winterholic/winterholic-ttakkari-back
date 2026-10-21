# ADR 0066 W4–W6 후속 보강 진행 보고

기준 draft `f9609ab2715a210fb1d514506ea0fda2505762bd`에서 별도 branch `feature/delivery-hardening-20261021`로 수정했다. `main`과 `back-delivery-review`는 편집하지 않았다. main 통합·push·배포는 하지 않았다.

## 반영한 수정

- **W4 격리·추적:** `TTAKKARI_DELIVERY_PIPELINE_ENABLED` 기본값은 false이며 Settings의 env 로딩 음성/양성 대조 테스트를 추가했다. opt-in 코드 수정 요청은 worker별 worktree와 별도 Workspace를 만들고 worktree만 writable root로 연결하며 ChangeSet을 등록한다. 검토·조사·검증, 비Git, 게임 요청은 이 경로에서 제외한다. worktree의 `commit-msg` hook이 세션 trailer를 추가한다. 제출 시 dirty 상태, 잘못된 commit SHA, session provenance 누락을 거부한다.
- **W5 직렬 검증·통합:** `cmd_process_once`의 dummy pass runner를 제거했다. flag가 꺼져 있거나 runner가 없으면 실행하지 않고 실패한다. 기본 runner는 candidate checkout에서 ruff와 실제 `pytest -m contract`, 전체 pytest를 실행하고 pytest가 0건·실패·명시적 통과 요약 부재면 fail closed 한다. PostgreSQL은 repository+target별 session advisory lock을 잡고, 큐는 `SKIP LOCKED` 및 15분 지난 claim 복구를 사용한다. 제출된 base/head/target SHA와 provenance를 검사한 뒤 merge-tree에서 얻은 tree로 merge candidate commit을 만들고, 동일 SHA의 detached checkout에서 Stage 1/2를 실행한다. 테스트 결과에 `tested_sha`를 저장하고 target ref는 이전 SHA 조건부 `update-ref` CAS로만 전진시킨다. 일반 checkout은 checkout/merge하지 않는다. candidate SHA와 결과를 Git CAS 전에 DB에 commit해 CAS 뒤 DB crash를 재시작 시 복구하고, 남은 candidate worktree도 정리한다. CLI는 commit/rollback/session close/engine dispose 경로를 갖는다.
- **W6 제한 수정:** diff guard를 실제 통합 경로에 연결하고 변경 테스트 소스 AST에서 테스트 삭제 및 `pytest` import 별칭을 통한 skip/xfail 추가도 검사한다. 서명별 횟수를 JSON map으로 보존하고 이전 scalar 카운터도 이관 시 이어 쓴다. 허용된 budget 안에서는 CLI가 원 worker 세션에 구조화된 prompt로 repair Run을 queue한다. 피드백 callback·서명 교대 상한은 테스트했지만 실 PostgreSQL worker 재실행 루프는 검증하지 못했다.
- **migration/fixture:** `0062_delivery_change_sets`는 draft의 최신 TTS `0061_local_tts` 뒤에 남기고 candidate/claim/repair 상태 컬럼을 추가했다. Alembic `env.py`에서 `delivery_models`를 import해 공유 Base metadata에 모델을 등록한다. SQLite async fixture용 `aiosqlite` dev dependency를 명시하고, fixture가 PostgreSQL 전용 전체 metadata trigger를 만들지 않게 ChangeSet 테이블만 생성한다. `session_id` 중복 index 선언도 제거했다.

## TDD 및 실행 증거

- **RED:** `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache ../back/scripts/pytest_slots.py -- uv run pytest -q --confcutdir=tests/delivery tests/delivery/test_merge_queue.py -k 'validation_and_promotion or submit_rejects or missing_validation'` → `3 failed`: 검증 runner가 shared checkout HEAD를 보았고, dirty 제출이 허용됐으며, no-op validator 미설정이 구체적 validation 오류 대신 `NoneType` 호출 오류를 냈다.
- **GREEN:** `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache ../back/scripts/pytest_slots.py -- uv run pytest -q --confcutdir=tests/delivery tests/delivery` → `42 passed in 6.25s`.
- **계약:** `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache ../back/scripts/pytest_slots.py -- uv run pytest -q --confcutdir=tests/delivery tests/test_migration_single_head.py tests/test_contract_marker.py` → `5 passed in 0.44s`.
- **수정 파일 lint:** `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache uv run ruff check src/ttakkari/delivery src/ttakkari/delivery_models.py src/ttakkari/api/orchestrator.py src/ttakkari/config.py tests/delivery alembic/env.py tests/test_migration_single_head.py` → `All checks passed!`.
- **Migration/CLI import smoke:** `uv run alembic heads` → `0062_delivery_change_sets (head)`; `uv run python -m ttakkari.delivery.cli --help`에서 `process-once` 등 명령 확인.
- **전체 프로젝트 lint:** `uv run ruff check --statistics` → 현재 tree 전체 `106 errors` (36 I001, 13 BLE001, 11 F401 등). 수정 대상 파일은 별도로 통과한다. 전체 lint 통과라고 주장하지 않는다.
- **전체 pytest:** `UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache ../back/scripts/pytest_slots.py -- uv run pytest -q -x --timeout=120` → exit 4, `tests/conftest.py` import 중 임시 PostgreSQL 준비 단계에서 `/opt/homebrew/opt/postgresql@17/bin/initdb ...`가 exit 1. stderr는 conftest traceback에 포함되지 않았다. 이는 Codex command sandbox 내 테스트 환경 실패 증거이며 production PostgreSQL 불능을 뜻하지 않는다.
- **초기 환경 분리:** 첫 uv 호출은 기본 cache(`/Users/winterholic/.cache/uv`) 초기화 시 `Operation not permitted`였고, 이후 허용된 `/private/tmp/ttakkari-uv-cache`를 써서 위 테스트가 실행됐다.

## 부모 통합 전 남은 검증

1. 전체 pytest와 PostgreSQL 대상 Stage 1/2, advisory lock 경쟁, target CAS 동시 advance, 실제 DB crash/restart 및 claim timeout recovery를 production-capable executor에서 실행한다. 이 결과 전까지 PostgreSQL 런타임은 미검증이다.
2. 실제 opt-in `/workers` API 요청으로 격리 Workspace·`extra_writable_roots`·ChangeSet 생성까지 통합 검증한다. Settings 단위 로딩과 내부 Git hook 테스트는 통과했지만 API 요청 자체는 실행하지 못했다.
3. W5 설계가 말하는 `submit_change` MCP 노출은 이번 slice에 추가하지 않았다. 제출 함수와 CLI는 있으나 MCP 경로는 미구현이다. 세대와 무관한 상주 integrator, 배치 검증·이분 탐색도 미구현이다.
4. bounded repair의 실제 `queue_run` DB 기록 후 동일 worker가 새 커밋을 내고 재제출해 상한에서 멈추는 전 경로는 PostgreSQL 검증 대기다.
5. **통합 충돌 표면:** feature commit `e1d01bceb2e2d59d4be8908193e7437f121bbc5e`의 직접 변경 중 당시 main 계보와 겹치는 파일은 `src/ttakkari/api/orchestrator.py`다. main 계보의 같은 파일에는 ADR 0067 agent-stop 변경이 있어, 통합 때 두 동작을 모두 보존해야 한다. main은 `tests/test_orchestrator.py`도 변경했지만 이 feature는 그 파일을 수정하지 않았다. migration `0062_delivery_change_sets`는 `0061_local_tts`를 parent로 하며 공통 기준 `25b5f9806656067ceb594d6d3217b37fba28d795`에 TTS `0061`이 있다. 후속 수정은 generation contract와 Alembic head 정합성을 별도로 갱신했다. main rebase/merge 및 실제 충돌 해결은 하지 않았다. 부모는 최신 main head를 기준으로 `0062` 번호/`down_revision`, 모델 metadata import, 의존성 lock 및 migration single-head contract를 재확인해야 한다.

초기 Flash worker 결과의 Antigravity `run_command` sandbox denial은 그 실행 환경에 한정된다. 부모 Codex 환경에서 Git worktree 및 파일 수정은 실제 성공했다. 실패한 환경을 production 제약으로 일반화하지 않는다.

## f99c59ca 독립 검증 후속 반영

독립 verifier의 FAIL 대상은 원 draft `f9609ab2715a210fb1d514506ea0fda2505762bd`다. 전달된 보고서와 부모 재현은 그 SHA에서 `process_success=True/status=merged`인데 `main_unchanged=True`, malformed SHA 및 중복 제출 수락을 확인했다. 이는 원 draft의 실행 증거이며, 기능 브랜치 결과로 재계산하지 않는다.

현재 기능 브랜치에서 같은 재현 파일을 그대로 실행하면 첫 import에서 `check_rebase_or_merge` 심볼을 찾지 못해 중단된다. 따라서 그 스크립트는 현재 API의 검증 증거로 취급하지 않았다. 현재 SHA의 실제 임시 Git 검증은 아래 delivery 테스트와 candidate validator 테스트로 별도 실행했다.

추가 조사에서 `project_stage_validator` 자식 프로세스가 candidate를 `cwd`로 지정하면서 `PYTHONPATH`를 고정하지 않는 결함을 찾았다. 이 저장소 `.venv`의 editable 패키지 경로는 기능 worktree의 `src/ttakkari`를 가리켰다. 임시 Git candidate 안에서만 정의한 `ttakkari.REVISION`을 import하는 Stage 2 회귀 테스트를 먼저 추가했을 때 실패했고, candidate `src`를 Python import 경로 앞에 추가한 뒤 통과했다. 테스트는 같은 candidate SHA에서 Stage 1의 실제 ruff + contract pytest(`1 passed`) 및 Stage 2 pytest(`2 passed`)를 실행한다.

독립 검증에서 다시 지적된 generation contract 불일치도 재확인했다. 테스트 당시 Alembic head는 `0062_delivery_change_sets`, `deploy/generation-contract.json`은 `0061_local_tts`여서 실패했다. 계약 revision을 0062로 올리고 온라인 migration 시작점 목록에 0061을 추가한 뒤 다음과 같이 통과했다.

```text
PYTHONPATH=src .venv/bin/python -m pytest --noconftest -q tests/test_migration_single_head.py tests/test_generation_deployment.py::test_application_generation_contract_matches_its_migration_head
3 passed in 0.31s
```

이번 수정 뒤 범위별 재실행 결과:

```text
PYTHONPATH=src .venv/bin/python -m pytest --noconftest -q --timeout=120 tests/delivery
43 passed in 6.09s

PYTHONPATH=src .venv/bin/python -m ruff check src/ttakkari/delivery src/ttakkari/delivery_models.py src/ttakkari/api/orchestrator.py src/ttakkari/config.py tests/delivery alembic/env.py tests/test_migration_single_head.py
All checks passed!

git diff --check
(출력 없음, exit 0)
```

`--noconftest` delivery 결과에는 SQLite async 및 임시 Git 테스트가 포함되지만 PostgreSQL advisory lock/`SKIP LOCKED`, migration apply, 실제 `/workers` API 실행 증거는 포함되지 않는다. 이 Run에서는 알려진 sandbox `initdb` 실패 경로를 재시도하지 않았다. 격리 호스트에서 실행할 수 있는 전체 Stage 및 migration 명령은 [W4–W6 격리 호스트 검증 manifest](w4-w6-host-validation-manifest.md)에 둔다.
