# Research Lab v2 통합 1단계 기록

작성일 2026-10-25. 범위는 research-v2 브랜치를 origin/main 위로 올리고 통합 회귀를 통과시키는 것까지다. push와 배포는 하지 않았다.

## 1. rebase 전후 SHA

| 저장소 | rebase 전 | origin/main | rebase 후 (테스트 대상) |
|---|---|---|---|
| 백엔드 back-research-v2 | `198e7297290969c3fdc8b17ee63ca351b0e9f6bb` (ahead 40 / behind 11) | `600eb84` (3차 rebase 시점) | 전체 회귀 대상 `7567912e6fe30a88127c1907a5db046d64e1c4ca`(ahead 42 / behind 0), 최종은 보고서 커밋을 포함한 브랜치 HEAD(ahead 43 / behind 0) |
| 프론트 front-research-v2 | `6930e52fe60850928d326211e5099c7310d0120d` (ahead 8 / behind 6) | `ad6149d` | `2c6fa10d11e5754e741c4d0827dd0b98d8e6e92f` (ahead 8 / behind 0) |

백엔드는 세 번 rebase했다. 1차는 `bc37116`, 2차는 main이 `98f4165`, `4501e34`(runs 풀 예산 수정)로 2커밋 전진한 뒤, 3차는 문서 커밋 직후 main이 `25839fb`, `600eb84`(runs/desk 연결 반납)로 2커밋 더 전진한 뒤다. 3차 새 main 커밋은 `runs/desk.py`, `runs/manager.py`, `tests/test_run_db_pool_budget.py`만 바꾸며 research, alembic, deploy와 겹치지 않는다. 3차 후에는 전체 재실행 대신 새 테스트와 (a) 묶음만 다시 돌렸다: `tests/test_run_db_pool_budget.py unit_tests/test_schema_head_admission.py` → `22 passed in 3.56s`, (a) → `293 passed, 2966 deselected in 60.55s (0:01:00)`, `alembic heads` → `0072_research_lab_v2 (head)`. 5절의 (b)(c) 전체 결과는 2차 rebase 후 `7567912` 기준이고, 3차에서 늘어난 main 변경분 전체 회귀는 미실행이다(`미실행: 변경이 runs 풀 코드에 한정돼 research 영향이 없다고 판단, 시간 비용`). 롤백용 로컬 태그 `pre-integration-stage1-back`(198e729), `pre-integration-stage1-front`(6930e52)를 남겼다.

### rebase 대신 merge를 쓰지 않은 근거
- 이 브랜치는 로컬 전용이고 사용자가 히스토리 재작성을 허용했다.
- 양쪽 모두 텍스트 충돌이 0건이었다. merge 커밋은 얻는 것이 없고 이력만 복잡해진다.
- 백엔드 40여 개 커밋이 독립 검증 보고서의 커밋 SHA(예: 0c37878)를 인용하므로, rebase 후에는 보고서 속 SHA와 실제 SHA가 달라진다. 보고서의 내용 근거는 그대로이고 SHA 매핑만 바뀐다는 점을 여기에 남긴다.

## 2. 충돌과 해결

- 텍스트 충돌: 백엔드 0건, 프론트 0건.
- 양쪽이 같은 파일을 건드린 곳(auto-merge): `src/ttakkari/orchestrator/mcp_server.py`, `tests/test_route_inventory.py`. 충돌 없이 합쳐졌고 `route_inventory` 테스트가 통과한다.
- main 쪽 변경(OpenCode/Copilot 엔진, TTS admission, runs 풀 예산)은 research와 겹치는 파일이 없어 그대로 보존됐다.
- 프론트는 main의 accounts/shell 변경 6커밋과 겹치는 파일이 없었다.

## 3. alembic

- `alembic heads`: `0072_research_lab_v2 (head)`. 단일 head.
- 체인: `0068_library_intent`, `0070_background_consumer_queue` → `0071_library_platform_v2_merge` → `0072_research_lab_v2`.
- main에는 0072 이상이 없다. 그래서 번호와 down_revision(`0071_library_platform_v2_merge`) 조정은 필요 없었다.
- 격리 PostgreSQL 17 임시 클러스터(포트 58721, 테스트 후 종료)에서 확인한 줄:

```
## alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 0071_library_platform_v2_merge -> 0072_research_lab_v2, Research Lab v2: ...
0072_research_lab_v2 (head)
## alembic downgrade -1
INFO  [alembic.runtime.migration] Running downgrade 0072_research_lab_v2 -> 0071_library_platform_v2_merge, Research Lab v2: ...
0071_library_platform_v2_merge (mergepoint)
## alembic upgrade head
INFO  [alembic.runtime.migration] Running upgrade 0071_library_platform_v2_merge -> 0072_research_lab_v2, Research Lab v2: ...
0072_research_lab_v2 (head)
```

### 0072의 온라인 호환(추가형) 판단

`alembic/versions/0072_research_lab_v2.py`(415줄)를 읽고 판단했다.

- 새 테이블 9개(taxonomy_terms, methods, method_relations, configurations, runs, measurements, analyses, artifacts, best_results)와 인덱스를 만든다. 이전 세대 코드는 이 테이블을 모른다.
- 기존 테이블 `research_studies`(2개 컬럼), `research_experiments`(9개 컬럼)에는 `ADD COLUMN IF NOT EXISTS`만 쓴다. NOT NULL 컬럼은 모두 상수 DEFAULT가 있고 나머지는 nullable이다. 이전 세대의 INSERT/UPDATE는 새 컬럼 없이도 성립한다. 기존 컬럼은 삭제, 이름 변경, 타입 변경이 없다.
- 데이터 이행은 기존 v1 실험마다 configuration, run, measurement 행을 추가하고 `research_experiments.adapter_id`와 `baseline_configuration_id`를 채우는 UPDATE뿐이다. 이전 세대가 읽는 컬럼은 건드리지 않는다.
- `research_measurements`에 BEFORE UPDATE OR DELETE 불변 트리거가 있으나 새 테이블에만 걸린다.
- 한계 1: 이행은 일회성 스냅샷이다. 이전 세대가 전환 중에 만든 v1 실험은 v2 configuration/run이 없다. 새 세대 코드가 이 행을 만나면 baseline이 없는 v1 형태로 읽힌다(레거시 `/execute` 경로가 이 경우를 유지한다).
- 한계 2: downgrade는 v2 테이블을 DROP하므로 v2 데이터가 사라진다. 물리 롤백은 데이터 보존형이 아니다.
- 한계 3: `ADD COLUMN ... REFERENCES`와 FK 추가는 짧은 ACCESS EXCLUSIVE 락을 잡는다. 실험 테이블은 작아서 영향이 짧다고 보지만 운영 행 수는 확인하지 못했다. `확인 필요`.

따라서 온라인 이행 허용이 타당하다고 판단한다.

### 계약·분류와의 일치

- `deploy/generation-contract.json`: `schema_revision`이 `0072_research_lab_v2`이고, `online_migration_from`에 `0071_library_platform_v2_merge`가 추가됐다. 즉 DB가 0071이면 "online"으로 이행한다. 위 판단과 일치한다. main의 기존 항목은 모두 보존됐다.
- `GenerationController.schema_plan`(`src/ttakkari/generation_deployment.py:961`)은 `resolved == schema_revision` 이면 "current", `online_migration_from`에 있으면 "online", 아니면 "idle"을 돌려준다.
- 이 정책에 맞춰 `unit_tests/test_schema_head_admission.py`의 기대값을 고쳤다. `0071` → `("online", 0071)`, 신규 케이스 `0072_research_lab_v2` → `("current", 0072_research_lab_v2)`. 고치기 전에는 `('online', ...) == ('current', ...)`로 실패했다. 이 실패는 계약 변경의 정상 귀결이고, 테스트 기대값만 낡았던 경우다.
- `scripts/platform_v2_inventory.py`의 migration 분류는 0072를 별도 처리하지 않지만 인벤토리 테스트가 통과한다.

## 4. O3 처리 (backend-review-0c37878.md)

- 동작: 실험 생성(`POST /studies/{id}/experiments`)과 검증(`POST /experiments/validate`, agent `experiment_validate`)에서 `method_id`, `compared_method_ids`가 카탈로그 `Method`에 없으면 422 `research_catalog_method_not_found: <id>`로 거절한다.
- 구현: `planner.unknown_catalog_method_ids`, `planner.unknown_method_message`, `api/research.py::require_catalog_methods`. `planner.validate_experiment_plan`도 같은 오류를 errors에 넣는다. 저장된 실험을 `/run`할 때(runner 경로) 삭제된 방법이 끼어 있어도 422가 된다.
- 회귀 테스트: `tests/test_research_v2_api_contract.py::test_unknown_catalog_method_ids_are_rejected_with_422`. 세 경우(method_id 단독, compared 단독, 존재하는 id와 섞인 compared)에서 validate와 create 모두 422, 실험·Run 행 수 불변, 배치 디스패치 없음을 확인한다. `test_known_catalog_method_ids_still_pass_validation`은 존재하는 id가 막히지 않는지 확인한다.
- 수정 전 코드에서 신규 테스트가 실패하는 것도 확인했다(src를 되돌려 실행: `1 failed, 36 deselected`). 수정 후 `37 passed`.
- 범위 밖: `method_ids`(Node 방법) 필드는 기존에 Node 조회로 404를 낸다. 이번에는 바꾸지 않았다.

## 5. 백엔드 회귀 (대상 `7567912`, 격리 임시 PostgreSQL 클러스터)

pytest는 `TTAKKARI_TEST_DB`, `PGHOST`, `PGPORT` 등을 비워 프로세스마다 임시 클러스터를 쓰게 했다. 결과 줄 원문이다(`/tmp/rv2-int-*.log`).

| 묶음 | 명령 요지 | 결과 줄 |
|---|---|---|
| (a) research 등 | `tests -k "research or catalog or host_gpu or route_inventory or schema or migration"` | `293 passed, 2960 deselected in 62.95s (0:01:02)` |
| (b) unit_tests + benchmark_tests (재실행, 깨끗한 환경) | `unit_tests benchmark_tests` | `1988 passed, 1 skipped, 6 warnings, 5014 subtests passed in 303.42s (0:05:03)` |
| (c) tests 나머지 | `tests -k "not (...)"` | `2939 passed, 19 skipped, 293 deselected, 2 xfailed, 3 warnings, 6 subtests passed in 564.82s (0:09:24)` |

(a)와 (c)는 선택 조건이 정확히 서로의 보수이고(2960 = 2939+19+2, 293 = 293) tests 전체를 덮는다. unit_tests와 benchmark_tests도 포함해 실행했다.

### 분류된 실패

최종 남은 실패는 0건이다. 실행 중 관찰한 실패와 분류는 다음과 같다.

1. `unit_tests/test_host_resource_recovery*.py` 14건 (b 첫 실행). 분류: 환경 오염, research 변경과 무관. 근거는 오류 원문이다: `GpuError: live test-server processes exist [2824]; stop them first, this command never signals processes`. PID 2824는 이전 세션에서 중단된 제 pytest가 남긴 가짜 llama-server 고아 프로세스였다(ppid 1, 이 worktree의 venv, pytest 임시 폴더, `--alias ttakkari-gpu-test`). 해당 프로세스를 종료하고 두 파일만 다시 돌려 `56 passed in 9.19s`, 이어 unit_tests 전체를 재실행해 `1988 passed`를 확인했다. 코드는 바꾸지 않았다. 샌드박스의 프로세스 목록 제약에 의한 실패는 아니었고, 실제 호스트에 live 테스트 서버가 있었던 것이다.
2. `unit_tests/test_schema_head_admission.py::...[heads1-expected1]` (이전 세션 전체 실행). 분류: research 변경이 원인인 계약 기대값 불일치. 위 3절대로 테스트를 수정했고 통과한다.
3. `tests/test_transfers.py` 3건 (이전 세션 전체 실행). 분류: 환경 오염으로 판단. 해당 실행은 `TTAKKARI_TEST_DB`가 가리키는 공유 DB(다른 작업자와 공용)를 쓰고 있었다. 격리 클러스터로 다시 돌린 (c)에서 전부 통과했다. 원 실패의 정확한 원인(공유 DB 경합인지 로깅 캡처 문제인지)은 규명하지 못했다. 추가 조사 없이 "공유 DB 실행에서만 관찰됨"으로 기록한다.
4. origin/main 단독 baseline: 임시 worktree(`git worktree add --detach origin/main`)를 만들었으나 같은 공유 DB 문제로 baseline 전체 실행이 완료되지 못했고, 실패가 최종 0건이라 baseline 비교가 필요한 항목이 없어 사용하지 않았다. worktree는 제거했다.

### 사고 기록: 공유 테스트 DB 오염과 정리

환경변수 `TTAKKARI_TEST_DB`가 지정돼 있어, 첫 전체 실행이 공유 테스트 DB에서 `drop_all`/`create_all`을 수행했다. 이 DB에는 다른 브랜치(kb-v3) 스키마도 올라와 있었다. 그 결과 research v2 테이블 9개가 남아 main 코드의 `drop_all`이 `DependentObjectsStillExistError`로 실패하는 상태가 됐다. 이 9개 테이블만 `DROP TABLE IF EXISTS ... CASCADE`로 지웠고(다른 테이블은 건드리지 않음), 남은 `research_*` 테이블은 v1 5개뿐임을 확인했다. v1 `research_experiments`에 v2 컬럼이 남아 있을 수 있으나 main의 `drop_all`이 테이블째 지우므로 문제가 되지 않는다. 이후 모든 실행은 임시 클러스터를 사용했다.

## 6. 프론트엔드 (대상 `2c6fa10`)

```
vitest run   Test Files  146 passed | 2 skipped (148)
             Tests  1410 passed | 10 skipped (1420)      exit=0
tsc -b       exit=0
eslint .     exit=0
vite build   exit=0   (PWA v2.0.0 ... precache  93 entries (3626.13 KiB))
```

`pnpm install --frozen-lockfile`은 lockfile이 최신이라 변경 없이 끝났다. 작업 트리는 clean이다(`dist`는 무시 대상).

## 7. 미검증 범위

- 실제 HTTP 시나리오 A~D, 브라우저 동작은 2단계 범위라 이번에 하지 않았다.
- 운영 DB 행 수와 락 시간은 확인하지 못했다.
- 프론트가 새 422 오류 문구(`research_catalog_method_not_found`)를 어떻게 표시하는지는 확인하지 않았다.
- push, 배포는 하지 않았다.
