# ArtCraft 배포 카탈로그 실패 교정

## 입력 및 판정

- 배포 예약: `793ee964-872c-4cbe-a543-d9155e43c13e`, source `fe90547e09136551a482457b18d88082298fd855`.
- 최신화 대조: 착수 때 `git fetch origin main` 성공, 당시 HEAD와 `origin/main`은 `b56057c03c944d93cbd08857318955cc9cbeac1c`. 검증 뒤 다시 fetch한 최신 tip은 `6dbc20ba70af9e32fa3806298d42f47dbe36589d`; 그 tip에서도 테스트는 같은 성공 Run + 무측정 상태이고 서비스는 exact finite measurement 검증을 유지한다. 동일 원인의 선행 교정이 없어 중복 수정은 아니며, 본 작업에서는 push/rebase 하지 않았다.
- Run: `0464247c-b1b4-494e-89ce-4ac1673e9faf`, profile `회사 코덱스 ai2`, engine `codex`, model `gpt-6-luna`, effort `high`.
- DB: 비밀을 출력하지 않고 URL의 `database=ttakkari_test`, `host=127.0.0.1`, `port=60925`, 사용자명 존재 여부만 확인했다. `/tmp/ttakkari-run-test-99596-8c76eac9/run-test-cluster.marker`는 같은 포트와 현재 Run UUID를 기록했다. 실제 SQL 접속 결과 `current_database=ttakkari_test`, `current_user=ttk_test`가 URL identity와 일치했다. 새 initdb/cluster를 만들지 않았다.
- `ops_deploy_log`의 pytest summary는 `1 failed, 871 passed, 2 skipped, 3 warnings, 6 subtests passed in 127.72s`; 실패는 지정 카탈로그 테스트다. `adopt_best_result()`가 `src/ttakkari/research/service.py:1049`에서 `best_result_metric_not_measured_on_selected_run` 422를 냈다.
- 판정: 테스트는 성공 Run과 `decode_tps` 채택을 구성하면서 해당 Run에 `Measurement`를 만들지 않았다. 명세 `docs/research-lab/evidence/research-lab-practical-recovery-20261106.md`는 성공 Run, component 일치, 선택 지표의 유한한 실측을 채택 요건으로 기록한다. 제품 검증이 정합하고 테스트 fixture가 불완전하다.

## Red / green 증거

### Red (교정 전)

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_catalog_seed_readiness_path.py::test_system_seed_promotion_rejected_and_user_scope_promotion_preserved_across_sync -q -p no:cacheprovider
F                                                                        [100%]
E   fastapi.exceptions.HTTPException: 422: best_result_metric_not_measured_on_selected_run
E   src/ttakkari/research/service.py:1049: HTTPException
1 failed in 0.60s
```

### Green (fixture 교정 후)

```text
$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_catalog_seed_readiness_path.py::test_system_seed_promotion_rejected_and_user_scope_promotion_preserved_across_sync -q -p no:cacheprovider
.                                                                        [100%]
1 passed in 0.75s

$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_catalog_seed_readiness_path.py -q -p no:cacheprovider
..................................                                       [100%]
34 passed in 1.89s

$ PYTHONPATH=src ../back/.venv/bin/pytest tests/test_research_v2_core.py::test_adopt_best_result_and_method_promote tests/test_research_v2_core.py::test_best_result_requires_finite_measurement_and_matching_trial tests/test_research_v2_core.py::test_regression_best_result_status_and_consistency -q -p no:cacheprovider
...                                                                      [100%]
3 passed in 1.06s
```

각 DB 테스트 실행에서 selective reset이 사용됐으며 forced full/fallback은 0이었다.

## 변경 diff

```diff
diff --git a/tests/test_catalog_seed_readiness_path.py b/tests/test_catalog_seed_readiness_path.py
index 83ceaede..5eed09a7 100644
--- a/tests/test_catalog_seed_readiness_path.py
+++ b/tests/test_catalog_seed_readiness_path.py
@@ -14,11 +14,20 @@ import pytest
 from fastapi import HTTPException
 from sqlalchemy import select, update
-from ttakkari.models import utcnow
+from ttakkari.db import sessionmaker
+from ttakkari.models import utcnow
 from ttakkari.research import catalog_loader
 from ttakkari.research.adapters.registry import get_adapter_registry
-from ttakkari.research.models import BestResult, Configuration, Experiment, Method, Node, Run, Study
+from ttakkari.research.models import (
+    BestResult,
+    Configuration,
+    Experiment,
+    Measurement,
+    Method,
+    Node,
+    Run,
+    Study,
+)
 from ttakkari.research.planner import validate_experiment_plan
 from ttakkari.research.readiness import compute_execution_status
 from ttakkari.research.service import adopt_best_result, promote_method
@@ -508,6 +508,16 @@ async def test_system_seed_promotion_rejected_and_user_scope_promotion_preserved
         )
         db.add(succ_run)
         await db.flush()
+        db.add(Measurement(
+            run_id=succ_run.id,
+            experiment_id=exp.id,
+            configuration_id=cfg.id,
+            metric_key="decode_tps",
+            metric_label="Decode tokens/s",
+            value=42.0,
+            unit="tokens/s",
+        ))
+        await db.flush()

         # 2. Direct promotion of system seed method must be rejected with 422
```

## 배포 gate 결과

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-0464247c ./scripts/verify_deploy_gate.sh --commit d6a270192cc8b0507b6c98929c9cd496c4be0656 --base fe90547e09136551a482457b18d88082298fd855
실패 ruff (변경된 파이썬 1개) (1초, exit 1)
통과 계약 묶음 (24초): 87 passed, 6884 deselected
통과 바뀐 테스트 파일 (1개) (4초): 34 passed
사전 검증 실패 1건: ruff (변경된 파이썬 1개)

$ git show fe90547e09136551a482457b18d88082298fd855:tests/test_catalog_seed_readiness_path.py > /tmp/test_catalog_seed_readiness_path-fe905.py
$ ../back/.venv/bin/ruff check --config pyproject.toml --output-format concise /tmp/test_catalog_seed_readiness_path-fe905.py
Found 13 errors: I001 1, RUF012 12 (same pre-existing findings as the gate)

$ ../back/.venv/bin/ruff check --select I tests/test_catalog_seed_readiness_path.py
All checks passed!
```

원본 `fe90547e`에서도 동일한 Ruff baseline이 확인됐다. 새 import 정렬만 교정했으며 기존 mock class의 `RUF012` 12건은 범위 밖이라 수정하지 않았다. 전체 gate는 Ruff 때문에 실패했으며 성공으로 기록하지 않는다.

최종 대상은 커밋 `7050e6e472a8b356abe9d37e2c8cd726f53176d8`이다. 같은 코드 변경을 포함한 이 SHA에 다시 gate를 실행했다.

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-0464247c ./scripts/verify_deploy_gate.sh --commit 7050e6e472a8b356abe9d37e2c8cd726f53176d8 --base fe90547e09136551a482457b18d88082298fd855
실패 ruff (변경된 파이썬 1개) (1초, exit 1): 기존 RUF012 12건
통과 계약 묶음 (24초): 87 passed, 6884 deselected
통과 바뀐 테스트 파일 (1개) (4초): 34 passed
사전 검증 실패 1건: ruff (변경된 파이썬 1개)
```

## 범위

수정 파일은 테스트 fixture 하나와 이 evidence 기록이다. 서비스 검증, 운영 DB, 계정, 바이너리와 provider 실행은 변경하지 않았다.
