# 카탈로그 코어 교정(3e2fb4d) 독립 검증

**종합 판정: FAILED.** 작업자 주장 4개 중 항목 1·2·3·4의 명시된 반례(빈 combinations, 순서 뒤집힌 충돌 중복, external-report 설계, system seed 승격 flag)는 모두 해결됐고 지정 테스트도 전부 통과했다. 그러나 이번 검증 지시가 따로 물은 우회 구멍 두 곳이 재현됐다. (D1) 모델 조건이 섞인 조합에서 readiness가 fail-open이다. (D2) `external_report` 어댑터 실험을 `/run`으로 부르면 외부 입력이 `local_measured` 출처의 성공 Run으로 기록된다. 둘 다 기능 코드 수정이 필요하므로 PASSED로 올리지 않는다.

## 대상과 검증 경계

- worktree `/Users/winterholic/development/projects/winterholic-ttakkari/back-research-v2`, branch `research-v2`. 대상 3e2fb4d, 기준 582bc58, 이전 FAIL 보고서 `catalog-review-c121c01.md`.
- 검증 중 HEAD가 다른 검증자의 문서 커밋(`1318f50`)으로 이동했다. `git diff 582bc58 3e2fb4d --stat`은 src 5개와 tests 2개이고, 이후 커밋은 docs만 추가했다. src/tests는 3e2fb4d와 동일하다. reset/stash/checkout 없음.
- 기능 코드와 seed는 수정하지 않았다. 임시 프로브는 `/tmp/rv_probe/`(커밋 안 함)에 두고 `PYTHONPATH=src:tests` + `-p conftest`로 repo의 임시 PostgreSQL 격리 fixture를 썼다. 운영 DB는 쓰지 않았다. `run_experiment_batch`는 프로브에서 stub으로 바꿔 실제 llama 서버가 뜨지 않게 했다. S1의 실측 한 건만 `external_report` 어댑터를 그대로 실행했다(GPU 무관).
- `PYTHONPATH=src`에서 `ttakkari.research.readiness.__file__`이 이 worktree의 `src/ttakkari/research/readiness.py`임을 확인했다.
- 브라우저/UI 렌더링은 실행하지 않았다. front는 읽기 전용으로 코드만 봤다(`front-research-v2` HEAD 20e69de 계열).
- agent 라우트(`/api/agent/research`)의 `adopt_best_result`·`experiment_create`는 오케스트레이터 토큰 인증(401 확인)이라 실제 호출은 못 했다. 같은 함수(`adopt_best_result_endpoint`, `experiment`)를 호출하는 코드(`api/research.py:1478-1481`)를 정적으로만 확인했다.

## 판정표

| 항목 | 판정 | 요약 |
|---|---|---|
| 1. readiness combinations | **조건부 FAIL (D1)** | 키 없음 legacy 유지, 키 있음+빈/잘못된 값 fail-closed, 같은 조건 충돌 순서 무관 fail-closed는 확인. 모델 조건 혼재와 `model`/`model_id` 혼용에서 fail-open 재현 |
| 2. planner/runner | **조건부 FAIL (D2, D3, D4)** | system seed의 로컬 어댑터 주 방법은 error·`/run` 422·Run 행 0개 확인. external-report 설계는 valid+warning 확인. 단 `external_report` 어댑터 `/run`이 local_measured로 샘다. method_id 없이 compared만 쓰면 검사를 우회한다. legacy `/execute`는 검사 없음 |
| 3. system seed 채택/승격 | **PASS** (UI 알림 갭 D5는 비차단) | promote_method=true여도 201, skipped 기록, 직접 promote 422 유지, 사용자 방법 승격 유지, 실패 주입 시 전부 롤백 |
| 4. 테스트 strategy 수정 | **PASS** | `manual`은 무효값이라 우연히 invalid였다. `single`로 바꾸고 plan_local 대조군을 추가해 의미가 강해졌다. 새 테스트는 구 src에서 12개 실패 |

## 실행 결과

```text
$ PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_catalog_seed_contract.py tests/test_catalog_seed_readiness_path.py tests/test_research_v2_catalog.py tests/test_research_v2_api_contract.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_adapters.py -q
144 passed in 5.79s
$ PYTHONPATH=src ./.venv/bin/python -m pytest tests -k research -q
109 passed, 3083 deselected in 7.03s
$ PYTHONPATH=src ./.venv/bin/python -m pytest tests/test_route_inventory.py -q
7 passed in 1.26s
```

작업자가 말한 144 passed(7개 파일)는 재현됐다. 이 통과가 곧 결함 없음은 아니다. 아래 D1~D4는 기존 테스트가 다루지 않는 입력이다.

## 1. readiness combinations

코드: `src/ttakkari/research/readiness.py:148`(`has_combinations = "combinations" in compatibility`), `:150-169`(키 있음+비었음/None/비list/잘못된 entry fail-closed), `:200-228`(같은 조건 충돌), `:230`(`next(...)`로 local_executable 우선 선택).

프로브(`/tmp/rv_probe/test_probe_readiness.py`, 가짜 llm_inference 어댑터 hardware=apple_silicon_metal, runtime=llama.cpp):

```text
legacy_no_key                         executable=True  (키 없음만 legacy)
key_empty / key_none / key_dict / bad status   executable=False
conflict local_executable+unsupported (양 순서)  executable=False
same-model conflict (model=X 둘, 상충)          executable=False
only X_local, adapter model=Y                    executable=False
```

작업자 주장 중 legacy가 `키 없음`으로만 열린다는 것, 상충 판정이 순서 무관이라는 것, 모델이 다르면 중복이 아니라는 것은 맞다.

### D1. 모델 조건이 섞이면 fail-open (재현됨)

```text
none_local + X_unsupported, adapter model=X     executable=True   <- 모델 X가 명시적 unsupported인데 통과
none_local + X_unsupported, available_models=[X] executable=True
model vs model_id 혼용 (X unsupported / X local, model_id) executable=True <- 같은 모델의 상충을 못 잡음
only X_local, 어댑터에 모델 식별 정보 없음         executable=True   <- 모델 조건을 검증하지 못했는데 통과
none_unsupported + X_local, adapter model=X     executable=True   (더 구체적인 선언이 이긴다고 보면 허용 가능)
```

원인: 상충 판정 키가 `(hardware, runtime, model, model_id)`라서 `model=None`과 `model=X`, `model`과 `model_id`가 서로 다른 그룹이 된다(`:216-220`). 그 뒤 `:230`이 그룹과 무관하게 local_executable이 하나라도 있으면 그것을 고른다. 모델 필터(`:207-212`)는 어댑터에 모델 식별 정보가 있을 때만 걸리므로, 정보가 없으면 모델 조건 tuple이 그대로 local_executable을 준다.

범위: 현재 seed 조합 23개에는 `model`/`model_id` 키가 없다(전수 스크립트로 확인, model-keyed 0). 그러나 `MethodIn.compatibility`는 임의 dict를 받으므로(`api/research.py:164`) 사용자 방법으로 도달 가능하다. 즉 seed 결함이 아니라 판정 함수의 잠복 결함이다. 기존 테스트는 `none+X` 혼재와 `model_id` 혼용 음성 대조를 갖고 있지 않다(`test_model_conditioned_tuples_are_not_duplicate_hardware_runtime_tuples`는 서로 다른 모델 두 개만 본다).

재현법: `/tmp/rv_probe/test_probe_readiness.py`의 CASES를 `compute_execution_status`에 넣고 `is_executable`을 본다. 최소 입력은 `combinations=[{hw,rt,status:local_executable},{hw,rt,model:"X",status:unsupported}]`, 어댑터 `details.model="X"`다.

권장 수정 방향(구현은 하지 않음): 같은 hardware/runtime 안에서 어댑터가 쓰는 모델에 해당하는 tuple만 모으고, 그중 local_executable 아닌 선언이 하나라도 있으면 fail-closed. 모델 조건 tuple인데 어댑터가 모델을 식별하지 못하면 fail-closed. `model`과 `model_id`는 같은 키로 정규화.

## 2. planner/runner

코드: `src/ttakkari/research/planner.py:412-427`(`is_primary`와 error/warning 분기), `src/ttakkari/research/runner.py:272-295`.

프로브(`/tmp/rv_probe/test_probe_api.py`, 실제 ASGI+임시 PG, 시스템 seed `qlora`(hint=external_report), `flash_attention`(local)):

```text
S3 qlora + llm_inference /run          422 experiment_plan_not_executable..., new_runs 0, batch_calls []
S4 qlora + llm_inference auto_start    422, new_exps 0, new_runs 0     (Experiment 행도 롤백됨)
S7 validate qlora + external_report    valid=True, warnings 1 (로컬 자동 실행 미지원)
S7b validate qlora + llm_inference     valid=False, errors 1
S5 primary flash + compared qlora, llm_inference /run   200 (compared는 warning만)
```

작업자 주장(external-report 설계는 warning, 로컬 어댑터 주 방법은 error, `/run`은 Run 행 생성 전 422)은 위 대조에서 맞다. `start_experiment_runs`의 호출자는 `api/research.py:533`(auto_start)과 `:994`(`/run`) 둘뿐이고, 둘 다 같은 검사를 거친다. MCP/agent 경로는 같은 endpoint 함수를 호출한다(정적 확인).

### D2. `external_report` 어댑터 실험을 `/run`으로 부르면 local_measured로 기록됨 (재현됨)

`runner.py:274`가 `external_report`를 검사에서 뺀다. 그 뒤 `runner.py:319`가 `execution_origin="local_measured"`로 Run을 만들고 배치를 돌린다. 어댑터 `execute_run`은 config의 measurements를 그대로 성공 결과로 돌려준다.

```text
$ (probe S1real) qlora + adapter_id=external_report, fixed_inputs={provenance, measurements:[decode_tps=99.0]}, POST /experiments/{id}/run
S1real run: succeeded local_measured external_report [('decode_tps', 99.0)]
S8 flash_attention + external_report /run: 200, new_runs 1, execution_origin=local_measured
```

즉 설계를 warning으로 유효하게 만든 뒤, 사용자가 입력한 값이 `local_measured` 출처로 둔갑한다. 프론트 `canPromote`가 `execution_origin === 'local_measured'`를 보므로 이 Run은 승격 UI 조건도 통과한다. `runner.py`의 해당 줄은 582bc58에도 있었으므로 이번 diff가 만든 회귀는 아니지만, 이번 변경이 `external_report` 설계를 유효로 열면서 이 경로를 실제로 닿게 만들었고 작업자가 말한 `/run` 차단 범위 밖에 남아 있다.

권장: `external_report` 실험의 `/run`은 422로 거부하거나(`/external-report` 사용 안내), 최소한 `execution_origin="external_report"`로 기록.

### D3. method_id 없이 compared만 쓰면 로컬 검사를 우회 (재현됨)

`planner.py:412`의 `is_primary`가 `method_id is not None`을 요구하므로 `method_id=None`이면 모든 방법이 warning이다.

```text
S2 method_id=None, compared_method_ids=[qlora], adapter_id=llm_inference, POST /run
200, new_runs 1, batch_calls [(exp, 1)]   (Run 행 생성+배치 dispatch)
```

`ExperimentIn.method_ids`(Node 참조 목록)는 planner가 아예 검증하지 않는다. 원래부터 method 없는 실험은 검사 대상이 없었지만, 비실행 방법을 compared에 넣기만 하면 로컬 어댑터 dispatch를 막지 못한다는 점은 주장과 다르다. 심각도 낮음(프로브는 batch를 stub으로 막았다. 실제 llama 실행은 하지 않았다).

### D4. legacy `/experiments/{id}/execute`는 readiness 검사가 없음 (재현됨)

`api/research.py:558-617`은 `row.adapter == "retrieval_eval"`만 보고 `benchmark.evaluate`를 직접 돌린다.

```text
S6 qlora + adapter retrieval_eval: POST /execute -> 200, benchmark_called=[1]
S6b 같은 실험 POST /run -> 422 experiment_plan_not_executable
```

변경되지 않은 경로이며 qlora 같은 external-only 방법이 retrieval 벤치마크 결과를 `locally_measured` 관측 노드로 남길 수 있다. 이번 diff의 회귀는 아니다. 같은 검사를 넣을지는 별도 판단이 필요하다.

## 3. system seed 채택과 승격

코드: `src/ttakkari/research/service.py:776-808`(승격 결과 계산·실행), `:829`(`conditions.method_promotion`), `src/ttakkari/api/research.py:1280-1284`(응답).

프로브(`/tmp/rv_probe/test_probe_best.py`):

```text
SYS adopt: 201 adopted {'requested': True, 'status': 'skipped', 'reason': 'system_seed_method'} method=None counts(dec,best)=(1,1)
SYS study.best_result_id set: True
USER adopt: 201 {'status': 'promoted', ..., 'lifecycle_status': 'candidate'} method.lifecycle=candidate counts(dec,best)=(2,1)
FAIL-INJECT (best_result_adopted 이벤트에서 RuntimeError): 500, counts before/after (0,0)/(0,0), method lifecycle hypothesis, promoted_from None
```

- system seed는 `promote_method=true`여도 201과 BestResult/Decision 저장, 승격은 skipped 기록. 직접 `/methods/{id}/promote` 422는 지정 테스트(`test_system_seed_best_result_is_adopted_while_method_promotion_is_skipped`)가 확인한다.
- 사용자 방법은 승격되고(결정 노드 2개: 승격 1+채택 1) sync 후 보존은 `test_system_seed_promotion_rejected_and_user_scope_promotion_preserved_across_sync`가 확인한다.
- 승격이 BestResult/Decision 저장보다 먼저 일어나도(`service.py:793` 호출이 `:812` 노드 생성 앞) 같은 세션 트랜잭션이라, 중간 실패 시 승격·결정 노드·BestResult가 함께 롤백된다. 부분 저장 없음.
- 프론트 형태: `front-research-v2/src/pages/research/api.ts:187` `adoptBestResult`는 `Data & { status?: string }`을 받고 `BestResultPanel.tsx` `onSuccess`는 `r.status`만 본다. 응답에 `method_promotion` 필드를 추가한 것은 호환된다(깨지지 않음).

### D5. (비차단) UI가 skipped를 알리지 않음

프론트 `canPromote`는 `method_id && local_measured && adopted`라서 system seed 방법의 local_measured Run에서도 승격 체크박스가 켜진다. 체크 후 채택하면 서버는 skipped로 기록하지만 UI는 `method_promotion`을 읽지 않아 사용자는 승격이 된 것으로 오해할 수 있다. 이전에는 422 오류로 알렸던 부분이다. 기능은 깨지지 않으므로 판정에 넣지 않고, front 후속 작업으로 남긴다. front 파일은 수정하지 않았다.

## 4. 테스트 strategy 수정

`planner.expand_configurations`는 알 수 없는 strategy에 오류를 추가한다(`planner.py` `else` 분기, "알 수 없는 실험 탐색 전략"). 따라서 구 테스트의 `strategy="manual"`은 실행 불가가 아니라 무효 strategy 때문에 invalid가 됐을 수 있었다. `single`로 바꾼 3e2fb4d 테스트는 같은 입력에서 설계 유효(valid=True, warning)와 로컬 어댑터 대조군(`plan_local`, valid=False, 로컬 자동 실행 미지원 error)을 함께 단언하므로 원인 분리가 된다.

의미가 약해졌는지 검증하려고 582bc58 소스를 `git archive`로 `/tmp/old_src`(별도 임시 git)에 풀어 같은 신규 테스트를 돌렸다(`readiness.__file__`이 `/tmp/old_src/src/...`임을 확인).

```text
$ PYTHONPATH=/tmp/old_src/src ./.venv/bin/python -m pytest tests/test_catalog_seed_readiness_path.py tests/test_research_v2_api_contract.py -q
12 failed, 32 passed   (readiness 대조군·충돌 중복·system seed 채택·validate/dispatch 테스트가 구 src에서 실패)
```

신규 테스트가 구 src에서 실패하므로 회귀를 잡는 힘이 있다. 반대로 D1·D2·D3은 이 테스트들이 잡지 못하는 입력이다(위 해당 절 참조).

## 미검증 범위

- agent 라우트 실제 호출(오케스트레이터 토큰 필요, 401 확인). 정적으로만 같은 함수 호출임을 확인.
- 브라우저 UI 동작(D5는 코드 판독).
- 실제 llm_inference/host GPU 실행(프로브는 batch stub). Metal 오프로드는 이번 범위가 아니다.
- seed 출처 문서 재검증, TTS LUFS 항목은 이번 대상이 아니다.

## 요약

- 작업자 4개 주장은 명시된 반례 기준으로 재현 해소됐다. 144 passed, `-k research` 109 passed, route inventory 7 passed.
- 그러나 FAILED. 결함: D1 모델 조건 혼재 fail-open, D2 `external_report` `/run`이 local_measured로 기록, D3 method_id 없는 compared 우회, D4 legacy `/execute` 무검사(기존). D5는 비차단 UI 갭.
