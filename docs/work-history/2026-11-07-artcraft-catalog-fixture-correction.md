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

## 이전 부분 커밋의 검사 기록 (최종 검사 아님)

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

이 단락의 `7050e6e472a8b356abe9d37e2c8cd726f53176d8`은 Ruff 교정/최신 main 통합 전의 중간 SHA다. 최종 커밋과 혼동하지 않는다.

```text
$ UV_CACHE_DIR=/private/tmp/ttakkari-uv-cache-0464247c ./scripts/verify_deploy_gate.sh --commit 7050e6e472a8b356abe9d37e2c8cd726f53176d8 --base fe90547e09136551a482457b18d88082298fd855
실패 ruff (변경된 파이썬 1개) (1초, exit 1): 기존 RUF012 12건
통과 계약 묶음 (24초): 87 passed, 6884 deselected
통과 바뀐 테스트 파일 (1개) (4초): 34 passed
사전 검증 실패 1건: ruff (변경된 파이썬 1개)
```

## 최신 main 통합 및 Ruff 교정 후속

- 현재 Run `43cdcc43-f1bc-43ee-bd94-36674007bc84`의 `/api/runs/{UUID}` 결과 profile은 `회사 코덱스 ai2`, engine `codex`, model `gpt-6-luna`, effort `high`다.
- `git fetch origin main` 뒤 통합 기준은 `7a10d0ccf71c9f3e5ce7f2314bc3db5c1513517c`. 원격의 fixture에는 아직 선택 측정값이 없고, service.py는 `best_result_metric_not_measured_on_selected_run` exact finite measurement 검증을 유지한다.
- `git merge --no-edit origin/main`은 merge commit `b9f0179144f58c08421d8ee15fd2b8c52fe57fff`로 성공했다. 부모는 기존 source `8f04cb3ca6ce85674e07343e2aa609be6815363e`와 main `7a10d0ccf71c9f3e5ce7f2314bc3db5c1513517c`; 병합된 타 세션 파일은 보존했다.
- 카탈로그 테스트의 여섯 mock adapter는 `supported_methods`와 `supported_parameters`를 실행 지원 capability로 읽는다. 이 테스트 파일에서 두 목록의 변경/mutation은 없다. 프로젝트 source와 tests가 `ClassVar[list[str]]`를 같은 class-level capability/list 선언에 쓰므로 여섯 mock의 12개 list 속성에 `ClassVar[list[str]]`를 명시했다. 값과 테스트 동작은 유지된다.
- fixture `Measurement(metric_key="decode_tps", value=42.0)` 및 system seed 거부·user-scope promotion/sync assertion은 merge 뒤에도 남아 있다.
- Ruff 정적 검사 `../back/.venv/bin/ruff check tests/test_catalog_seed_readiness_path.py`와 `git diff --check`는 통과했다.
- 현재 Run의 `TTAKKARI_TEST_DB`는 URL의 `ttakkari_test`, `127.0.0.1:52972`와 실제 접속 `current_database=ttakkari_test`, `current_user=ttk_test`가 일치한다. 하지만 `/tmp/ttakkari-run-test-*/run-test-cluster.marker` 12개 중 현재 Run UUID 또는 port `52972`를 소유한 marker는 0개였다. 소유 증명 없는 DB에서 suite를 쓰지 않으므로 집중 DB 검사와 `verify_deploy_gate.sh`는 실행하지 않았다. 이전 `8f04cb3c` 시점 검사 결과를 현재 SHA의 통과로 재사용하지 않는다.
- 최신 변경 source commit은 `cc6236b185db2d5bef0716659a5bfba182b3fc16`, 부모는 통합 merge `b9f0179144f58c08421d8ee15fd2b8c52fe57fff`, 기준 main은 `7a10d0ccf71c9f3e5ce7f2314bc3db5c1513517c`다. source diff는 테스트 48 줄 변경과 evidence 갱신 130 줄이며 `git diff --check <base> <source>` 통과. `../back/.venv/bin/ruff check tests/test_catalog_seed_readiness_path.py` 출력은 `All checks passed!`다.

최신 main 기준 source diff (service.py에는 변경 없음):

```diff
 tests/test_catalog_seed_readiness_path.py
@@
+from typing import ClassVar
@@ six mock adapter classes
-        supported_methods = [...]
-        supported_parameters = [...]
+        supported_methods: ClassVar[list[str]] = [...]
+        supported_parameters: ClassVar[list[str]] = [...]
@@ system seed adoption fixture
+        db.add(Measurement(
+            run_id=succ_run.id,
+            experiment_id=exp.id,
+            configuration_id=cfg.id,
+            metric_key="decode_tps",
+            metric_label="Decode tokens/s",
+            value=42.0,
+            unit="tokens/s",
+        ))
```

이번 통합 source의 집중 DB 검사와 필수 gate는 owner marker 확인 실패 때문에 **미실행**이다. 이 SHA의 gate 통과를 주장하지 않는다. 후속 evidence-only 문서 tip은 source commit과 별도로 최종 보고에서 구분한다.

## 범위

후속 변경은 테스트 fixture의 명시적 `ClassVar` 타입과 이 evidence 기록이다. 서비스 검증, 운영 DB, 계정, 바이너리와 provider 실행은 변경하지 않았다.
