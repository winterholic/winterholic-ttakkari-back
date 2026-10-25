# Research Lab v2 코어 교정 독립 검증

- 판정: **PASSED**
- 대상: `c531dfb` (코어 결함 4건), `92c4850` (readiness whitelist 및 카탈로그 검증)
- 검증 checkout: branch `research-v2`, HEAD `476521dd5859c88e75febdbae167559ac9f488c0`; 시작 시 작업 트리 clean. 수정 파일은 이 보고서뿐이다.
- 원 기준: `core-review-1e35485.md`, `core-review-2cea922.md`.

## 결함 재현 및 판정

### 1. Accepted-work gate 거부

- 재현: `./.venv/bin/python -m pytest -q tests/test_research_v2_core.py::test_regression_runner_gate_rejection`
- 출력: `1 passed` (별도 결함 재실행 묶음은 아래에 기록).
- 결과: `runner.py:188-211`은 gate의 `acquired=False`에서 adapter 실행 루프 전에 반환한다. `Experiment.state=cancelled`, receipt에 `GENERATION_REJECTED`와 사유, queued Run에 `status=cancelled`, `error_code=GENERATION_REJECTED`, `error_message`, `finished_at`을 저장한다. 회귀 테스트는 실제 DB 행을 만든 뒤 runner를 호출하고 이 필드를 확인한다 (`tests/test_research_v2_core.py:461-512`).
- 자원: 거부 분기는 `execute_run_instance`/adapter 호출 전에 끝나며 `runner.py:188-211` 안에 lease 또는 실행 자원 할당이 없다. 따라서 이 경로에서 새 lease가 남지 않는 것은 코드 경로상 확인된다. 테스트는 `RuntimeLease` 테이블을 직접 전후 비교하지 않았고 기존에 존재하던 외부 lease의 정리는 이 검사 범위가 아니다.
- 결함 재현법(교정 전): 동일 실험에 queued Run을 만든 뒤 `generation.enter_accepted_work()`가 `False`를 yield하게 하고 `run_experiment_batch()`를 호출한다. 기존 동작은 계속 실행 흐름으로 진입했으며, 현재 회귀는 취소 상태·사유 저장으로 이를 차단한다.

### 2. 외부 결과 provenance 왕복

- 재현: `./.venv/bin/python -m pytest -q tests/test_research_v2_core.py::test_regression_external_report_provenance_roundtrip`
- 출력: `1 passed`.
- 결과: 유효하지 않은 provenance는 422, `manual_evaluation`은 Run `raw_output`과 측정값 `meta`에 저장되며 GET `/api/research/runs/{id}` 응답에도 보존된다. 구현 위치는 `api/research.py:239-246,1000-1017,1030-1103`; 요청/조회 왕복 assertion은 `tests/test_research_v2_core.py:515-557`.
- 결함 재현법(교정 전): `external-report`에 `manual_evaluation`을 전달하고 생성 응답 및 Run 상세 조회의 `raw_output.provenance`와 `measurements[].meta.provenance`를 비교한다. 이전에는 입력 provenance가 영속 결과에 반영되지 않았다.

### 3. Best result 상태 및 소속 일관성

- 재현: `./.venv/bin/python -m pytest -q tests/test_research_v2_core.py::test_regression_best_result_status_and_consistency`
- 출력: `1 passed`.
- 결과: API 엔드포인트가 `body.status`를 service에 전달한다 (`api/research.py:1153-1176`). service는 `exp.study_id`, `cfg.experiment_id`, `run.experiment_id`, `run.configuration_id`, `run.study_id`를 모두 검사해 불일치면 400을 낸다 (`research/service.py:652-683`). rejected 요청은 status와 Decision epistemic status/event를 `rejected`로 기록하며 `study.best_result_id`를 설정하지 않는다 (`service.py:685-735`). 테스트는 다른 연구의 Experiment를 섞은 요청 거절과 rejected 저장을 실제 API/DB로 확인한다 (`tests/test_research_v2_core.py:559-630`).
- 추가 호출 경로: `/api/agent/research`의 `adopt_best_result` dispatch는 같은 `adopt_best_result_endpoint`를 호출한다 (`api/research.py:1279-1285,1359-1366`). MCP 도구 `manage_research`는 operation·study_id·data를 이 agent API로 전달한다 (`orchestrator/mcp_server.py:60-88`). 따라서 두 경로도 같은 service 소속 검사를 거친다. 이 두 별도 전송 경로의 런타임 호출은 이번 pytest에서 실행하지 않고 라우팅을 코드로 추적했다.
- 결함 재현법(교정 전): 성공 Run과 Configuration은 유지하고 다른 Study 소속 Experiment를 제출한 뒤, 같은 구성으로 `status=rejected`를 제출한다. 첫 요청은 거절돼야 하고 둘째 결과는 adopted가 아닌 rejected여야 한다.

### 4. Pareto 지표 및 프론트 계약

- 재현: `./.venv/bin/python -m pytest -q tests/test_research_v2_core.py::test_regression_pareto_single_metric_and_x_metric_passthrough`
- 출력: `1 passed`.
- 결과: 한 지표만 있을 때 `pareto_frontier=[]`, `pareto_status="unavailable"`, 불가 사유가 반환된다. 명시한 `pareto_x_metric="vram"`은 분석 함수의 보조 지표로 전달되어 summary의 `secondary_metric`이 `vram`이 된다 (`research/analyzer.py:259-304,389-403`; assertion `tests/test_research_v2_core.py:633-719`).
- API 계약: 기존 compare-series 키와 타입을 유지하고 `pareto_status`, `pareto_unavailable_reason` 두 키를 추가한다 (`research/analyzer.py:367-386`). 교정 전에는 이 두 키가 없었다 (`git show c531dfb^:src/ttakkari/research/analyzer.py`, 같은 반환 블록). 확인한 변경은 additive이며 기존 필드 제거/이름 변경은 없다. 프론트엔드 저장소는 이번 checkout에 없어 실제 소비 코드의 런타임 호환은 별도 확인하지 않았다.
- 결함 재현법(교정 전): 모든 측정값의 metric_key를 하나로 맞춰 compare-series를 호출하고 Pareto 축 두 개가 생기지 않는지 검사한다. 둘 이상의 지표에서는 analyze에 서로 다른 x/y를 보내 선택된 x 지표가 summary에 반영되는지 확인한다.

### 5. 지원 방법 whitelist 및 카탈로그 assertion 정밀도

- 재현: `./.venv/bin/python -m pytest -q tests/test_research_v2_catalog.py::test_readiness_fail_closed_when_supported_methods_undeclared_or_empty`
- 출력: `1 passed`.
- 결과: `supported_methods` 미선언 또는 빈 목록이면 지원하지 않는 method는 `partially_supported`로 분류된다 (`research/readiness.py:145-180`; 테스트 `test_research_v2_catalog.py:238-289`). 테스트는 ready 응답만 어댑터 stub으로 제공하고 `compute_execution_status()`는 실제 구현을 호출하므로 예상 판정을 미리 주입하지 않는다.
- seed 대조: `test_research_v2_catalog.py:116-183`가 DB 적재 후 112 method 각각의 parameter/targets/goals/layers를 seed와 비교하고, 관계 `(from_slug,to_slug,type)` tuple 집합도 seed와 대조한다. 관계 총수도 seed와 비교한다. 이는 기준 리뷰의 “개수만 비교” 공백을 닫는다. tuple 비교가 set 기반이라 동일 tuple의 중복 개수 자체는 구분하지 않는 작은 범위 한계가 있다.
- 결함 재현법(교정 전): `check_environment()`만 ready인 adapter 객체에 `supported_methods` 속성을 빼고 unlisted slug, 빈 parameters, score metric을 넣어 `compute_execution_status()`를 호출한다. 이전 구현은 `immediately_executable`로 분류했으며, 현재는 `partially_supported`다.

## 회귀 테스트가 결론을 주입하는지

- gate 테스트는 gate 반환값만 `False`로 고정해 거부 분기를 재현한다. Run/Experiment 저장 결과와 실행 거부는 실제 `run_experiment_batch()` 및 DB에서 읽어 assertion한다 (`tests/test_research_v2_core.py:461-512`).
- provenance, best result, Pareto 테스트는 실제 API 요청·DB 저장·조회·분석 함수를 사용한다 (`tests/test_research_v2_core.py:515-719`).
- whitelist 테스트만 ready 환경 응답을 stub한다. method 분류 결과는 실제 readiness 구현의 계산값이다 (`tests/test_research_v2_catalog.py:238-289`). 따라서 대상 결함의 핵심 결론을 테스트 mock으로 주입한 사례는 확인되지 않았다.

## 테스트 실행

사용자 지정 우선 경로인 `./.venv/bin/python -m pytest`를 사용했다. UV fallback은 필요하지 않았다.

1. `./.venv/bin/python -m pytest -q tests/test_research_v2_core.py tests/test_research_v2_migration.py tests/test_research_v2_catalog.py tests/test_catalog_seed_contract.py tests/test_research_lab.py tests/test_research_v2_e2e.py tests/test_research_adapters.py unit_tests/test_research_benchmark.py unit_tests/test_research_rules.py`
   - `89 passed in 6.11s`
2. `./.venv/bin/python -m pytest -q tests -k research`
   - `74 passed, 3042 deselected in 5.16s`
3. 결함별 재현: 위 4개 core 회귀와 readiness fail-closed 테스트를 한 명령으로 재실행.
   - `5 passed in 1.10s`

테스트 DB 요약은 각 실행에서 `db reset: selective ...; 0 forced full, 0 fell back to full`이었다. 운영 DB·실제 MCP 전송·프론트 소비 코드는 이 검증에 포함하지 않았다.
