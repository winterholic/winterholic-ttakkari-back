# 백엔드 교정(0c37878) 독립 교차검증

**종합 판정: PASSED.** 이전 FAIL 보고서(`catalog-core-review-3e2fb4d.md`)의 D1~D4는 같은 재현 절차로 모두 해소됐다. 다른 dispatch 경로를 전수 확인했지만 readiness 검사를 건너뛰는 경로는 없었다. 시드 112개 방법의 readiness 분포는 3e2fb4d와 한 건도 다르지 않다. 차단 결함은 없고, 비차단 관찰 3건을 아래에 적었다.

## 대상과 검증 경계

- worktree `/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2`, branch `research-v2`, HEAD `0c37878`, 기준 `3e2fb4d`. `ttakkari.research.readiness.__file__`이 이 worktree의 `src/`임을 확인했다.
- 읽기와 테스트 실행만 했다. 기능 코드, seed, 테스트 파일은 수정하지 않았다. reset, stash, checkout, push 없음. 메인 체크아웃 `back/`은 건드리지 않았다.
- 프로브는 `/tmp/rv_probe/`(이전 것 재사용)와 `/tmp/rv_probe2/`(신규)에 두었고 커밋하지 않았다. repo의 임시 PostgreSQL 격리 fixture(`PYTHONPATH=src:tests` + `-p conftest`)를 썼고 운영 DB는 쓰지 않았다. `run_experiment_batch`는 프로브에서 stub으로 바꿔 실제 llama 서버가 뜨지 않게 했다.
- 비교용 3e2fb4d는 `git worktree add --detach /tmp/rv_old_3e2fb4d 3e2fb4d`로 만들고, 그 경로를 PYTHONPATH 앞에 둔 채 실행한 뒤 `git worktree remove`로 지웠다(`readiness.__file__`이 `/tmp/rv_old_3e2fb4d/src/...`임을 확인).

## 판정표

| 항목 | 판정 | 요약 |
|---|---|---|
| D1 readiness 모델 조건 | **해소** | 이전 fail-open 5개 입력 중 4개가 fail-closed, 1개(`none_unsupported + X_local`, 어댑터 모델 X)는 구체 선언 우선 정책대로 허용 |
| D2 external_report `/run` | **해소** | 422 `external_report_experiments_are_not_locally_executable`, Run 행 0, 상태 queued 유지, `auto_start`도 dispatch 없음 |
| D3 compared 우회 | **해소** | method_id 없이 compared만, 주 방법 실행 가능+compared 비실행 모두 422, Run 행 0 |
| D4 legacy `/execute` | **해소** | 연결된 비실행 방법은 422, 상태 queued 유지, benchmark 미호출. v1 실험은 200 유지 |
| 새 우회 탐색 | **구멍 없음** | dispatch 경로 3곳 전부 검사 통과. Node 참조 `method_ids`는 카탈로그와 연결되지 않아 우회가 아님 |
| 시드/정상 경로 | **회귀 없음** | 112개 방법의 상태 분포와 개별 결과가 3e2fb4d와 동일 |
| 테스트 | **통과** | 지정 9개 파일 188 passed, `-k research` 114 passed |

## 실행 결과

```text
$ PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_catalog_seed_contract.py tests/test_catalog_seed_readiness_path.py tests/test_research_v2_catalog.py tests/test_research_v2_api_contract.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_adapters.py tests/test_research_lab.py tests/test_route_inventory.py -q
188 passed in 6.85s
$ PYTHONPATH=src ./.venv/bin/python -m pytest tests -k research -q
114 passed, 3103 deselected in 6.91s
```

작업자 주장(188, 114)이 그대로 재현됐다. 이 통과만으로 결함 없음을 말하지 않는다. 아래 항목은 별도 프로브로 확인했다.

## 1. D1~D4 재현 절차 재실행

### D1 readiness (`src/ttakkari/research/readiness.py:46` 정규화, `:55` 튜플 검증, `:73` 어댑터 모델 식별, `:242` 미식별 fail-closed, `:250-260` 구체 튜플 우선)

`/tmp/rv_probe/test_probe_readiness.py`(이전 보고서와 같은 프로브)를 재실행했다.

```text
none_local + X_unsupported, adapter model=X         executable=False   (이전 True)
none_local + X_unsupported, available_models=[X]    executable=False   (이전 True, available_models만으로 식별 불인정)
model vs model_id 혼용 (X unsupported / X local)    executable=False   (이전 True)
only X_local, 어댑터 모델 식별 없음                  executable=False   (이전 True)
none_unsupported + X_local, adapter model=X         executable=True    (이전도 True, 구체 선언 우선 정책)
legacy_no_key=True, key_empty/none/dict/bad status=False, 같은 조건 충돌 양 순서=False (이전과 동일)
```

신규 엣지 프로브 `/tmp/rv_probe2/test_p2_readiness.py`(16개):

```text
tuple model=' ' / model=5(int) / model=X+model_id=Y         executable=False (튜플 무효, fail-closed)
어댑터 model=X + model_id=Y 상충                              executable=False (미식별)
어댑터 model non-str(list)                                    executable=False (미식별)
X 수준 상충(X unsupported + X local) + 일반 local             executable=False
어댑터 모델 Y, 선언은 일반 local + X unsupported               executable=True  (Y에 대한 선언이 없으므로 일반 선언 적용)
tuple model=X + model_id=X (동일 철자)                         executable=True
```

판정 정책은 작업자 설명과 일치한다. 모델 조건 튜플은 일반 튜플보다 구체적이며, 어댑터가 식별한 모델의 튜플이 있으면 그것을 쓰고, 없으면 일반 튜플로 간다. 같은 수준의 상충은 fail-closed다.

### D2 external_report `/run` (`src/ttakkari/research/runner.py:272-278`, `src/ttakkari/api/research.py:533`)

```text
S1 /run on external_report experiment: 422 [] batch_calls []            (이전: succeeded local_measured)
S8 flash + external_report /run:      422 new_runs 0
A  auto_start=true external_report:   201 runs_in_resp 0 new_runs 0 batch []
B  /run 후: 422 external_report_experiments_are_not_locally_executable new_runs 0 state queued
C  /external-report: 201, runs [('external_report', 'succeeded')]
M  adapter_id 'External_Report' / ' external_report' / 'external_report ' + /run: 422, new_runs 0
```

외부 결과는 `/external-report`에서만 `execution_origin=external_report`로 기록된다. 대소문자, 공백 변형 adapter_id는 레지스트리에서 어댑터로 인식되지 않아 plan 검증에서 막히므로 차단 문자열 비교를 우회하지 못한다.

### D3 compared 우회 (`src/ttakkari/research/planner.py:360-381`, `:430-437`)

```text
S2 method_id=None, compared=[qlora], llm_inference /run: 422 new_runs 0 batch []   (이전 200 + Run 1행)
S5 primary flash + compared qlora, llm_inference /run:   422 new_runs 0             (이전 200)
S3 qlora llm_inference /run: 422 new_runs 0 / S4 auto_start: 422 new_exps 0 new_runs 0
S7 validate qlora external_report: valid=True warnings 1 / S7b qlora llm_inference: valid=False errors 1
```

검증 응답의 설계 유효성(external_report는 warning)과 로컬 실행 가능성(local 어댑터는 error)이 분리되어 있다.

### D4 legacy `/execute` (`src/ttakkari/api/research.py:568-585`)

```text
E qlora(method_id) + retrieval_eval /execute:        422, state queued, benchmark called []   (이전 200 + benchmark 호출)
F compared-only qlora /execute:                      422, state queued, benchmark called []
G flash(method_id) + retrieval_eval /execute:        422 (flash의 힌트가 llm_inference라 retrieval_eval로는 불가, /run과 같은 판정)
D v1 실험(method_id=None, compared=[]) /execute:      200, state completed
S6b 같은 실험 /run:                                   422
```

검사가 `row.state = "running"`과 `svc.event(...)`, `benchmark.evaluate` 앞에 있어서 거부 시 상태와 이벤트가 바뀌지 않는다.

## 2. 새 우회 탐색

### dispatch 경로 전수 (grep)

```text
$ grep -rnE "start_experiment_runs|run_experiment_batch|execute_run_instance|benchmark\.evaluate|asyncio\.create_task" src
runner.py:343/350  run_experiment_batch  ← start_experiment_runs 내부에서만 호출
api/research.py:534   start_experiment_runs  (experiment() 의 auto_start)
api/research.py:1010  start_experiment_runs  (POST /experiments/{id}/run)
api/research.py:591   benchmark.evaluate     (POST /experiments/{id}/execute, legacy)
```

- `Run(` 생성 지점은 `runner.py:320`(local_measured, `start_experiment_runs` 안)과 `api/research.py:1171`(`/external-report`, external_report 출처) 둘뿐이다. `start_experiment_runs`는 `external_report`를 먼저 422로 거부한 뒤 `validate_experiment_plan` → `local_readiness_issues`를 통과해야 Run을 만든다.
- `run_experiment_batch`와 `execute_run_instance`는 `src/ttakkari/research/` 밖에서 호출되지 않는다.
- sweep, repeat, rerun 전용 엔드포인트는 없다. `repeat`는 `strategy="repeat"`로 `start_experiment_runs` 내부에서 반복 수만 늘린다. 재실행은 `/run`(configuration_ids 지정)뿐이고 같은 검사를 거친다.
- `/experiments/{id}/control`(`api/research.py:663`)은 상태 전이(pause/resume/cancel/block)만 하고 dispatch하지 않는다.
- MCP 연구 도구 `manage_research`(`src/ttakkari/orchestrator/mcp_server.py:61-88`)는 `/api/agent/research`로 전달만 한다. agent 라우트(`api/research.py:1403-1520`)에서 실행을 일으키는 연산은 `execute`(→ legacy `execute()`)와 `experiment`/`experiment_create`(→ `experiment()`의 `auto_start`)뿐이고, `/run`에 해당하는 연산은 없다. 즉 세 경로 모두 위 검사를 통과한다.
- 판정 한계: agent 라우트는 오케스트레이터 토큰이 필요해서 실제 호출은 못 했다(이전 보고서와 동일). 같은 함수를 호출한다는 점은 코드로만 확인했다.

### `ExperimentIn.method_ids`(Node 참조) 제외는 우회 구멍이 아니다

- `method_ids`는 `Node(kind="method")`의 id다(`api/research.py:418-431`). `Node` 모델에는 카탈로그 `Method`로의 FK나 컬럼이 없다(`models.py:47-62`).
- 코드에서 `Node.kind == "method"`를 읽는 곳은 `select_methods`(`api/research.py:375`, Node 제목/요약 검색과 랭킹)와 `experiment()`의 존재 확인뿐이다. `conditions["method_id"]` 같은 필드를 카탈로그 연결로 해석하는 코드는 없고, runner와 어댑터는 `exp.method_ids`를 읽지 않는다(`grep method_ids runner.py adapters/` 결과 없음).
- 프로브 H: Node(`details.method_id=qlora의 카탈로그 id`)만 `method_ids`로 걸고 `llm_inference` `/run` → 200, Run 1행. 이는 카탈로그 연결이 없는 실험(method_id 없음, compared 없음)과 같은 경로이고, 이번 변경 이전부터 그랬다. 카탈로그 방법을 지정하지 않으면 검사할 방법이 없다. 어댑터 자체의 `check_environment`와 `validate_config`는 그대로 적용된다.
- 따라서 Node를 카탈로그 readiness 검사에 포함하지 않은 것은 정의상 맞다. 다만 "카탈로그 readiness는 `method_id`/`compared_method_ids`에만 걸린다"는 사실이 곧 "연결 없는 실험은 검사 대상 아님"이라는 뜻이므로 아래 관찰 O3으로 남긴다.

## 3. 시드 readiness 분포 비교 (3e2fb4d vs 0c37878)

방법: 각 worktree에서 실제 `catalog_loader.load_catalog_seeds`로 시드를 임시 DB에 적재하고, 적재된 `Method` 행 전체에 `compute_execution_status(m, scope="personal")`을 돌려 결과를 JSON으로 저장해 비교했다(`/tmp/rv_probe2/test_p3_dist.py`, `cmp.py`). 어댑터 환경은 두 조건으로 돌렸다. `real_env`는 이 Mac Studio 실제 상태(llm_inference ready, apple_silicon_metal × llama.cpp, 모델 별칭 2개, retrieval_eval cpu × scikit-learn, tts_synthesis cpu × onnxruntime, external_report ready)이고, `forced_ready`는 모든 어댑터의 `check_environment`를 ready=True로 강제한 경우다.

| 상태 | executable | 3e2fb4d | 0c37878 | 차이 |
|---|---|---:|---:|---:|
| immediately_executable | True | 5 | 5 | 0 |
| tool_integrated | True | 4 | 4 | 0 |
| partially_supported | True | 86 | 86 | 0 |
| adapter_required | False | 11 | 11 | 0 |
| environment_unavailable | False | 5 | 5 | 0 |
| hypothesis_stage | False | 1 | 1 | 0 |
| **합계** | | **112** | **112** | **0** |

- `real_env`와 `forced_ready` 모두 방법별 결과(status, executable, 매칭 어댑터, 사유) 불일치가 0건이다. 슬러그 집합도 같다. 실행 가능 방법은 95개다.
- 시드의 `combinations`는 방법 6개, 튜플 23개이고 키는 `hardware, runtime, status, note`뿐이다. `model`/`model_id` 키가 하나도 없어서 새 모델 조건 정책이 시드에는 영향을 주지 못한다(전수 스크립트로 확인). 6개 방법의 결과는 두 커밋에서 같다.

```text
kv_cache_quantization   [llm_inference] metal×llama.cpp=local_executable → tool_integrated, executable=True
flash_attention         [llm_inference] metal×llama.cpp=local_executable → tool_integrated, executable=True
speculative_decoding    [llm_inference] metal×llama.cpp=local_executable → partially_supported, executable=True
memory_layout_optimization [llm_inference] metal×llama.cpp=unverified    → environment_unavailable, False
lora / qlora            [external_report] 로컬 튜플 없음                 → environment_unavailable, False
```

정상 실행 경로도 API로 확인했다(`/tmp/rv_probe2/test_p4_normal.py`, 시드 방법을 `/api/research/catalog/sync`로 적재).

```text
N flash_attention          llm_inference  validate True | /run 200 new_runs 1 origin ['local_measured']
N kv_cache_quantization    llm_inference  validate True | /run 200 new_runs 1 origin ['local_measured']
N speculative_decoding     llm_inference  validate True | /run 200 new_runs 1 origin ['local_measured']
N hybrid_retrieval         retrieval_eval validate True | /run 200 new_runs 1 origin ['local_measured']
N tts_sentence_segmentation tts_synthesis validate True | /run 200 new_runs 1 origin ['local_measured']
```

Mac Metal/llama.cpp, retrieval, tts의 Full Support가 잘못 막히지 않는다. 실제 llama 서버 기동과 GPU 실행은 하지 않았다(배치는 stub).

## 4. D4 v1 보존

- `tests/test_research_lab.py`는 지정 9개 파일 안에서 통과했다(188 passed에 포함). 이 파일의 v1 테스트는 `method_ids: [m["id"]]`(Node id)를 쓰고 `/execute`를 호출해 `receipt.origin == "server_executed"`를 확인한다(`tests/test_research_lab.py:50, 167-171`).
- v1 실험 데이터 형태: `experiment()`가 만든 v1 실험은 `method_id=None`, `compared_method_ids=[]`이고(프로브 D의 응답에서 직접 확인), 컬럼은 `compared_method_ids JSONB NOT NULL DEFAULT '[]'`(`alembic/versions/0072_research_lab_v2.py:114`)라서 기존 행도 `[]`다. 코드는 `list(row.compared_method_ids or [])`로 None도 안전하게 처리한다(`api/research.py:572-575`).
- 프로브 D: v1 실험 `/execute` → 200, state completed. 연결된 방법이 없으면 `linked_method_ids`가 비어 검사를 건너뛴다.
- 신규 테스트 `test_legacy_execute_applies_method_readiness_but_keeps_v1_experiments`가 같은 두 방향(연결 방법 422, v1 유지)을 단언한다.

## 반례 재현법 (이전 보고서 대비)

- D1: `/tmp/rv_probe/test_probe_readiness.py`의 CASES를 `compute_execution_status`에 넣고 `is_executable`을 본다. 예: `combinations=[{hw,rt,status:local_executable},{hw,rt,model:"X",status:unsupported}]` + 어댑터 `details.model="X"` → 이제 False.
- D2~D4: `PYTHONPATH=src:tests ./.venv/bin/python -m pytest -c pyproject.toml --rootdir . -p conftest /tmp/rv_probe/test_probe_api.py /tmp/rv_probe/test_probe_s1.py -q -s -p no:cacheprovider`(S1~S8 출력). 신규 프로브는 `/tmp/rv_probe2/test_p2_api.py`(A~H), `test_p5_misc.py`(M).
- 주의: `/tmp`의 프로브는 repo의 rootdir 밖이라 `-c pyproject.toml --rootdir .`를 줘야 `asyncio_mode=auto`가 적용된다.

## 비차단 관찰

- **O1 모델 식별은 대소문자와 철자를 구분하는 정확 일치다.** 선언 `model: "X"`가 unsupported이고 어댑터가 `"x"`를 보고하면 일반 튜플로 판정돼 executable=True가 된다(프로브 `adapter 'x' vs tuple 'X'`). 공백은 strip되어 정규화된다. 현재 실제 어댑터는 모델을 보고하지 않으므로(O2) 이 경로에 도달하지 않는다. 모델을 보고하는 어댑터를 붙일 때 정규화 규칙(대소문자, 별칭)을 정해야 한다.
- **O2 실제 `llm_inference.check_environment`의 `details`에는 `model`/`model_id`가 없다**(`adapters/llm_inference.py:151-161`는 `available_models`만 담는다). 그래서 모델 조건 튜플이 있는 방법은 현재 어떤 어댑터로도 `local_executable`이 될 수 없다. 사용자 방법에 모델 조건을 걸면 항상 fail-closed다. 보수적인 방향이라 결함은 아니지만, 모델별 선언을 쓰려면 어댑터가 사용 모델을 보고하도록 바뀌어야 한다.
- **O3 카탈로그 방법에 연결되지 않은 실험은 readiness 검사 대상이 아니다.** method_id 없음, compared 없음, Node 참조만, 존재하지 않는 compared id(프로브 `M random compared id`: 201/200, Run 1행)가 모두 어댑터로 dispatch된다. `local_readiness_issues`가 `Method`를 찾지 못하면 건너뛴다(`planner.py:371-373`). 이전 변경 이전부터의 동작이고 검사할 방법이 없는 경우라 우회로 보지 않았다. 존재하지 않는 compared id를 422로 거부할지는 별도 판단 사항이다.

## 미검증 범위

- agent 라우트 실제 호출(오케스트레이터 토큰 필요). 같은 함수를 호출한다는 점은 정적 확인만 했다.
- 브라우저/UI 동작과 front 코드.
- 실제 llm_inference/host GPU 실행, 실제 llama 서버 기동. 프로브는 배치를 stub으로 막았다.
- 운영 DB의 기존 v1 행 실데이터(임시 DB와 alembic 컬럼 정의로만 확인).
- `/run`이 실험 상태(paused, cancelled)를 확인하지 않는 점은 이번 변경 범위가 아니라 보지 않았다.
- seed 출처 문서 재검증과 TTS 측정값은 이번 대상이 아니다.

## 요약

- 판정 PASSED. D1~D4 모두 해소 확인, 새 우회 경로 없음, 시드 112개 readiness 분포는 3e2fb4d와 동일.
- 지정 9개 파일 188 passed, `-k research` 114 passed.
- 비차단 관찰 O1(모델 식별 정확 일치), O2(어댑터가 모델 미보고), O3(연결 없는 실험은 검사 대상 아님).
