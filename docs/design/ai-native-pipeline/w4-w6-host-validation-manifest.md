# W4–W6 격리 호스트 검증 manifest

현재 Run의 Codex command sandbox에서 알려진 PostgreSQL fixture `initdb` 실패를 재시도하지 않는다. 아래 명령은 PostgreSQL 임시 클러스터를 시작할 수 있고 사용 승인을 받은 격리 테스트 호스트에서만 실행한다. 운영 호스트/DB, 공유 DB, 서비스 자격 증명을 사용하지 않는다. 각 pytest 프로세스는 `tests/pg_sandbox.py`가 소유하는 `/tmp/ttakkari-test-*` 클러스터를 만들어야 한다. `TTAKKARI_TEST_DB`를 설정하면 이 격리가 우회되므로 설정하지 않는다.

## 불변 checkout 및 전체 delivery gate

`FEATURE_SHA`에 최종 기능 브랜치 SHA를 넣고, 검증 전용 detached worktree에서 실행한다.

```bash
set -eu
FEATURE_SHA='<최종 기능 SHA>'
BASE_SHA='25b5f9806656067ceb594d6d3217b37fba28d795'
VERIFY_ROOT="/private/tmp/ttakkari-delivery-verify-${FEATURE_SHA}"
git worktree add --detach "$VERIFY_ROOT" "$FEATURE_SHA"
cd "$VERIFY_ROOT"
test "$(git rev-parse HEAD)" = "$FEATURE_SHA"
unset TTAKKARI_TEST_DB TTAKKARI_DATABASE_URL DATABASE_URL PGHOST PGPORT PGDATABASE
export TTAKKARI_NO_ENV_FILE=1
uv sync --frozen --group dev
git diff --name-only -z "$BASE_SHA..$FEATURE_SHA" -- '*.py' | xargs -0 uv run ruff check
PYTHONPATH="$VERIFY_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" uv run pytest -q --timeout=120 \
  tests/delivery \
  tests/test_migration_single_head.py \
  tests/test_generation_deployment.py::test_application_generation_contract_matches_its_migration_head
git status --short --branch
git rev-parse HEAD
```

`tests/conftest.py`가 자체 `/tmp/ttakkari-test-*` PostgreSQL 클러스터를 만들고 끝날 때 정리한다. `TTAKKARI_TEST_DB`를 지정하지 않는다. 이 gate가 성공해야 실제 PostgreSQL이 필요한 delivery 동작의 별도 검증을 진행한다.

완료 기준: `git rev-parse HEAD`가 고정한 `FEATURE_SHA`와 같고, 변경 Python 경로 ruff가 exit 0, 지정 pytest 묶음이 exit 0이며 종료 후 `git status --short`가 비어 있어야 한다. 테스트 수와 전체 stdout/stderr를 기록한다. 이 단계는 테스트 suite 통과 증거이지 advisory lock/crash-recovery 전용 증거로 대체되지 않는다.

## 실제 Stage 1/2 명령과 candidate SHA 결합

같은 detached checkout에서 배포 경로의 validator를 호출한다. Stage 1은 delivery ruff와 contract-marked pytest를, Stage 2는 전체 pytest를 실행한다. 각 pytest 프로세스가 전용 임시 PostgreSQL을 만들므로 PostgreSQL host에서 실행한다.

```bash
PYTHONPATH="$VERIFY_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" uv run python - <<'PY'
import asyncio
import subprocess
from pathlib import Path

from ttakkari.delivery.validation import project_stage_validator

root = Path.cwd()
sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()

async def main():
    for stage in ("stage1", "stage2"):
        passed, log, signature = await project_stage_validator(stage, root, sha)
        print(f"stage={stage} passed={passed} sha={sha} signature={signature}")
        print(log)
        if not passed:
            raise SystemExit(1)

asyncio.run(main())
PY
```

통과 기준은 두 단계 모두 `passed=True`이고 각각의 결과 로그에 동일한 `tested_sha=<FEATURE_SHA>`가 포함되는 것이다.

완료 기준: 출력된 두 Stage 모두 `passed=True`, 두 `tested_sha`가 고정 `FEATURE_SHA`와 일치, 각 Stage 로그에 실제 pytest 통과 요약이 있어야 한다. 하나라도 실패하거나 summary/SHA가 없으면 전체 gate 실패다.

## 실제 임시 PostgreSQL에 migration 적용 및 컬럼 확인

아래 검사는 `tests/pg_sandbox.py`가 소유하는 새 임시 PostgreSQL cluster/database만 사용한다. 공유 PostgreSQL에 DB를 만들지 않는다.

```bash
PYTHONPATH="$VERIFY_ROOT/src:$VERIFY_ROOT/tests${PYTHONPATH:+:$PYTHONPATH}" \
TTAKKARI_NO_ENV_FILE=1 uv run python - <<'PY'
import asyncio
import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

import pg_sandbox

root = Path.cwd()
cluster = pg_sandbox.start_cluster()
try:
    url = cluster.create_database("ttakkari_delivery_migration")
    env = os.environ | {"TTAKKARI_DATABASE_URL": url, "TTAKKARI_NO_ENV_FILE": "1"}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=env, check=True)

    async def inspect():
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                head = await conn.scalar(text("SELECT version_num FROM alembic_version"))
                rows = (await conn.execute(text(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name = 'change_sets'"
                ))).all()
                columns = dict(rows)
                print(f"alembic_head={head}")
                print(f"candidate_sha={columns.get('candidate_sha')}")
                print(f"stage1_result={columns.get('stage1_result')}")
                print(f"signature_attempts={columns.get('signature_attempts')}")
                assert head == "0062_delivery_change_sets"
                assert columns.get("candidate_sha") == "character varying"
                assert columns.get("stage1_result") == "jsonb"
                assert columns.get("signature_attempts") == "jsonb"
        finally:
            await engine.dispose()

    asyncio.run(inspect())
finally:
    cluster.stop()
PY
```

완료 기준: upgrade가 새 임시 DB에서 종료 코드 0, `alembic_version`이 그 통합 checkout의 유일한 head, `change_sets.candidate_sha`가 `varchar`, `stage1_result`와 `signature_attempts`가 `jsonb`여야 한다. 통합 후 revision 번호/부모가 조정되면 위의 기대값을 추정해서 고정하지 말고, 최종 migration 그래프와 contract에서 읽은 기대 revision으로 검사한다.

## 아직 전용 테스트가 없는 PostgreSQL/API 동작

아래 테스트 모듈과 node는 현재 feature HEAD에 없다. 따라서 지금 이 명령이 “no tests collected”, import error 또는 node 없음으로 끝나면 통과가 아니다. 별도 구현에서 전용 테스트를 추가한 뒤, 동일한 승인 테스트 호스트의 임시 클러스터에서 실행한다.

```bash
uv run pytest -q --timeout=120 \
  tests/delivery/test_merge_queue_postgres.py \
  tests/test_orchestrator_delivery_isolation.py
```

`test_merge_queue_postgres.py`에는 다음을 실제 두 PostgreSQL 세션/process와 disposable Git repo로 검증하는 테스트가 필요하다.

1. 같은 repository+target을 처리하는 두 integrator가 advisory lock 구간에서 겹치지 않고, 두 번째 작업은 첫 번째 ref advance 뒤의 target을 다시 읽는다. `SKIP LOCKED` claim은 같은 ChangeSet 중복 반환 없이 서로 다른 행을 claim한다.
2. validation 동안 외부 target advance가 발생하면 CAS 실패로 안전 재시도하고 거짓 `merged`가 나오지 않는다.
3. failpoint로 **DB에 candidate/result 저장 뒤 CAS 전** 프로세스를 중단하면 target은 기존 SHA에 남고, 재시작 후 검증/승격이 idempotent하게 완료된다.
4. failpoint로 **Git CAS 성공 뒤 DB 최종 commit 전** 중단하면 재시작 시 target=candidate와 SHA-bound Stage proof를 확인해 `merged`로 복구하고 중복 승격은 없다.
5. PostgreSQL commit/rollback 뒤 재조회로 claim/status/result가 기대값과 일치한다. candidate/source worktree 정리 결과도 검사한다.

`test_orchestrator_delivery_isolation.py`는 실제 `/workers` API 요청에서 opt-in flag on 시 전용 worktree/Workspace writable root/ChangeSet session 연결을 확인하고, flag off 및 read-only 요청에서는 기존 공유 Workspace를 유지하며 ChangeSet을 만들지 않는 음성 대조를 포함해야 한다. 인증된 요청 응답과 DB에 저장된 row를 함께 검사한다.

완료 기준: 위 전용 테스트가 명시된 노드 수만큼 실제 실행되어 모두 통과하고, 각 테스트가 만든 PostgreSQL cluster와 Git repo 경로 및 ref/row before-after를 stdout 또는 테스트 artifact로 남긴다. 현재 이 테스트들이 없어 PostgreSQL 동시성·crash recovery·실제 API는 여전히 **미검증**이다.

## 남는 런타임 판정

## 통합 전 충돌 및 기능 완료 범위

독립검증이 확인한 다른 executor commit `95fc0a7`에는 `alembic/versions/0060_run_executor.py`가 있고, 현재 이 feature 계보에는 `0060_collector_job_runtime.py → 0061_local_tts.py → 0062_delivery_change_sets.py`가 있다. 두 revision 모두 `0060`을 사용하므로 두 migration을 그대로 병합하면 revision 충돌/다중 head가 난다. 양쪽의 schema 변경과 순서를 보존해 revision/down_revision을 통합한 뒤 `uv run alembic heads`, `tests/test_migration_single_head.py`, generation contract 테스트를 다시 실행해야 한다. 두 계보는 `README.md`, `deploy/generation-contract.json`, `pyproject.toml`, `src/ttakkari/config.py`, `tests/test_migration_single_head.py`도 함께 바꾸므로 수동 통합 점검이 필요하다. main ADR 0067 변경과는 `src/ttakkari/api/orchestrator.py`, `src/ttakkari/config.py`가 겹친다. 이 feature branch에서는 이 통합을 수행하지 않는다.

`submit_change` MCP 노출, 상주 integrator, batch validation 및 이분 탐색은 미구현이다. 이 manifest의 gate가 통과해도 W4–W6 전체 완료를 의미하지 않는다.
