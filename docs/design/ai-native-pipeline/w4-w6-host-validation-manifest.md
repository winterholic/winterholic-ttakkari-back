# W4–W6 격리 호스트 검증 manifest

현재 Run의 Codex command sandbox에서 PostgreSQL fixture의 `initdb` 실행을 재시도하지 않는다. 아래 명령은 PostgreSQL 임시 클러스터를 시작할 수 있는 격리 호스트에서 실행한다. 공유 DB나 운영 DB를 지정하지 않는다.

## 불변 checkout 및 전체 delivery gate

`FEATURE_SHA`에 최종 기능 브랜치 SHA를 넣고, 검증 전용 detached worktree에서 실행한다.

```bash
set -eu
FEATURE_SHA='<최종 기능 SHA>'
VERIFY_ROOT="/private/tmp/ttakkari-delivery-verify-${FEATURE_SHA}"
git worktree add --detach "$VERIFY_ROOT" "$FEATURE_SHA"
cd "$VERIFY_ROOT"
uv sync --frozen --group dev
unset TTAKKARI_TEST_DB
PYTHONPATH="$VERIFY_ROOT/src${PYTHONPATH:+:$PYTHONPATH}" uv run pytest -q --timeout=120 \
  tests/delivery \
  tests/test_migration_single_head.py \
  tests/test_generation_deployment.py::test_application_generation_contract_matches_its_migration_head
git status --short --branch
git rev-parse HEAD
```

`tests/conftest.py`가 자체 `/tmp/ttakkari-test-*` PostgreSQL 클러스터를 만들고 끝날 때 정리한다. `TTAKKARI_TEST_DB`를 지정하지 않는다. 이 gate가 성공해야 실제 PostgreSQL이 필요한 delivery 동작의 별도 검증을 진행한다.

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

## 남는 런타임 판정

현재 테스트 집합은 PostgreSQL `SKIP LOCKED` 경합, 저장소·target advisory lock 경합, DB commit 직후/target CAS 직후 프로세스 중단 복구를 직접 입증하는 전용 테스트를 포함하지 않는다. 전체 gate와 migration 적용이 통과해도 이 세 동작은 별도 PostgreSQL fault-injection 결과가 생기기 전까지 미검증으로 보고한다. 또한 실제 opt-in `/workers` 요청은 별도 API 통합 검증 항목이다.
