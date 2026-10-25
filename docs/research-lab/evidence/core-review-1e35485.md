# Research Lab v2 backend core independent review

- 대상 커밋: `1e354853c5dba836c344ec441878feed3065f5a3` (`research-v2`)
- 판정: **FAILED**
- 범위: 코어·마이그레이션·API·runner·MCP 및 지정 계약. 어댑터 자체 구현은 별도 검토라 결함 판정에서 제외했다.
- 작업트리 시작 시 adapter preset 파일이 변경 상태였고 검토 도중 어댑터 구현 파일도 변경 상태가 됐다. 현재 확인된 어댑터 변경 파일들은 모두 별도 작업물로 취급해 수정·스테이징하지 않았다.
- `git fetch origin && git log origin/main --oneline -- alembic/versions`: 원격 migration 목록은 `0071_library_platform_v2_merge`까지이며 `0072_research_lab_v2`는 없다. 0072 충돌은 확인되지 않았다. 로컬 branch는 당시 `origin/main` 대비 ahead 9 / behind 4였다.

## 지금 조치할 결함

### 1. generation accepted-work gate 결과를 무시함 — 중간

- 위치: [runner.py](../../../src/ttakkari/research/runner.py#L188)
- 재현 경로: `generation.enter_accepted_work()`는 generation이 active/draining이 아니면 `False`를 yield한다([generation.py](../../../generation.py#L317)). runner는 결과를 `as acquired`로 받지 않고 본문을 계속 실행한다.
- 영향: 정지/유효하지 않은 generation에서 작업 보호를 받지 못한 채 DB 상태 변경·실험 실행까지 진행될 수 있다. 요청을 받아 대기 중인 background task도 gate가 거부한 경우 실행된다.
- 권장 수정: `acquired`가 false이면 실행을 시작하지 말고 queued run을 명시적인 취소/실패 상태로 정리한다. accepted-work 진입과 run 수락 사이의 생명주기도 회귀 테스트로 고정한다.
- 확인 근거: `runner.py:188-195`의 gate 진입과 바로 이어지는 실행 흐름, `generation.py:322-325`의 거부 시 `yield False`.

### 2. 외부 결과 provenance 필드를 저장하지 않음 — 중간

- 위치: [research.py](../../../src/ttakkari/api/research.py#L239), [research.py](../../../src/ttakkari/api/research.py#L1053)
- 재현 명령: `nl -ba src/ttakkari/api/research.py | sed -n '239,246p;1053,1067p'`
- 관측: 요청 모델은 `provenance`를 받지만 Run 생성 시 `raw_output=body.raw_output`만 저장하고 `body.provenance`는 사용하지 않는다. `execution_origin='external_report'` 구분은 유지되나 `external_benchmark | manual_evaluation | imported_log` 출처 종류는 유실된다.
- 영향: 외부 측정 출처를 재현·검토할 수 없고 서로 다른 외부 근거가 같은 출처로 보인다.
- 권장 수정: 제한된 provenance enum을 검증하고 Run의 영속 원본 또는 Measurement metadata에 포함한다. 입력값과 조회값을 대조하는 회귀를 추가한다.

### 3. best result의 rejected 요청을 adopted로 기록하고 구성 일관성을 검사하지 않음 — 중간

- 위치: [research.py](../../../src/ttakkari/api/research.py#L255), [service.py](../../../src/ttakkari/research/service.py#L652)
- 재현 명령: `rg -n 'status: Literal|status="adopted"|exp = await db.get|cfg = await db.get|run = await db.get' src/ttakkari/api/research.py src/ttakkari/research/service.py`
- 관측: `BestResultIn.status`는 `adopted | rejected`를 허용하지만 endpoint가 status를 service로 넘기지 않고 service는 항상 `status="adopted"`를 저장한다. service는 세 객체 존재와 Run 성공만 검사하고 study/experiment/configuration/run 간 소속 일치는 검사하지 않는다.
- 영향: 거부한 결과가 채택 결정으로 잘못 기록될 수 있고, 서로 다른 실험·연구의 성공 Run과 Configuration을 조합한 잘못된 BestResult가 생성될 수 있다.
- 권장 수정: status를 보존해 rejected/adopted 별 Decision 의미를 저장하고, `exp.study_id == study.id`, `cfg.experiment_id == exp.id`, `run.experiment_id/configuration_id/study_id` 일치를 flush 전에 검증한다.

### 4. 단일 지표에서 자기 자신을 Pareto 두 축으로 사용 — 중간

- 위치: [analyzer.py](../../../src/ttakkari/research/analyzer.py#L258), [analyzer.py](../../../src/ttakkari/research/analyzer.py#L270)
- 재현 명령: `nl -ba src/ttakkari/research/analyzer.py | sed -n '258,275p'`
- 관측: secondary metric이 없고 다른 측정 지표도 없으면 `sec_metric = p_metric`이 되어 같은 지표를 x/y 양축으로 넘긴다. 두 번째 지표가 없는 조건을 비교 불가로 표시하지 않는다. 분석 입력의 `pareto_x_metric`도 [analyze_experiment](../../../src/ttakkari/research/analyzer.py#L359)에서 사용되지 않는다(`pareto_y_metric`만 primary metric으로 전달).
- 영향: 차트에 Pareto 결과가 있어도 두 독립 목표의 trade-off가 아니며 사용자에게 다목적 비교로 오해를 줄 수 있다.
- 권장 수정: 서로 다른 유효 지표가 없으면 frontier를 계산하지 않고 비교 불가 사유를 반환한다. 요청된 x/y 지표를 실제 비교 함수에 전달한다.

## 확인 결과

### 마이그레이션 R13

격리 테스트 DB에서 v1 표본을 넣어 upgrade/downgrade를 실행했다. 전체 JSON 필드와 측정값을 직접 비교했고, immutable trigger의 UPDATE/DELETE도 각각 시도했다. 운영 DB 접속은 하지 않았다.

- 실행: `UV_CACHE_DIR=/Users/winterholic/development/.cache/uv-research-v2-review uv run python -c 'exec(open("/tmp/research_v2_migration_probe.py").read())'`
- 출력:
  - `upgrade: exact v1 config/environment/budget/receipt/method_ids preserved`
  - `migration: run origin/status and 2 metric keys/values matched`
  - `immutability: UPDATE and DELETE rejected`
  - `downgrade: original v1 fields matched; v2 table absent`
- 기존 `tests/test_research_v2_migration.py` 단독은 `1 passed`. 이 테스트는 테스트용 ORM 전체 스키마에서 0072를 stamp한 뒤 downgrade하므로, 그 통과만으로 빈 0071 DB에서의 실 migration을 입증하지 않는다. 별도 probe는 실제 0071 revision 상태에서 표본을 넣고 0072 upgrade/downgrade를 확인했다.

### Generation 계약·API·runner·MCP

- Generation 계약 revision은 `0072_research_lab_v2`; 온라인 허용 목록에 0071 및 선행 migration 체인이 포함되어 있다. 배포 계약·단일 head 검사 포함 지정 contract 테스트 통과.
- 지정 API 경로 존재와 구 v1 research API 호환성은 실행 테스트로 확인했다. 카탈로그 검색/필터/facets, conflict·budget 초과·Bayesian 미지원, 방법 등록/버전/승격, 실험 실행/취소, 비교 시리즈, best result, artifact 흐름의 기존 관련 테스트 통과.
- `compute_distribution`은 n<2에서 `std: null`과 `변동성 미상 (n < 2)`를 반환한다. catalog/readiness는 seed의 실행 상태를 그대로 읽지 않고 `compute_execution_status`에서 adapter registry와 `check_environment(scope=...)`를 호출한다. 실제 운영 호스트 GPU/TTS 준비도는 이 리뷰에서 측정하지 않았다.
- runner는 별도 daemon 없이 현재 프로세스 `asyncio.create_task`와 generation accepted-work context를 사용한다. adapter가 반환한 failed 결과와 예외는 Run의 failed 상태·오류를 저장한다. gate boolean 무시는 위 결함 1.
- MCP `manage_research` 신규 operation과 기존 tool catalog가 일치한다. `tests/test_context.py::test_tools_listing_matches_mcp_server`가 포함된 계약 묶음 통과.

## 실행한 테스트

초기 uv 기본 캐시 경로는 sandbox에서 열 수 없어 첫 실행이 시작되지 않았다(`Operation not permitted`). 캐시 위치만 개발 디렉터리로 지정해 재실행했다.

1. `UV_CACHE_DIR=/Users/winterholic/development/.cache/uv-research-v2-review ./scripts/pytest_slots.py --timeout 300 -- uv run pytest -q tests/test_research_v2_migration.py`
   - `1 passed in 0.69s`
2. `UV_CACHE_DIR=/Users/winterholic/development/.cache/uv-research-v2-review ./scripts/pytest_slots.py --timeout 900 -- uv run pytest -q tests/test_research_lab.py tests/test_research_v2_catalog.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_adapters.py tests/test_catalog_seed_contract.py tests/test_context.py tests/test_generation_deployment.py tests/test_migration_single_head.py tests/test_api_artifacts.py tests/test_api_artifact_resolve.py unit_tests/test_research_benchmark.py unit_tests/test_research_rules.py`
   - `144 passed in 17.16s`
   - 전체 테스트 스위트는 실행하지 않았다.

## 미검증

- live 운영 API/DB, 배포 draining 중 실제 Run 완료와 프로세스 종료 시나리오.
- 실호스트 GPU/TTS adapter readiness 및 측정 결과. 어댑터 자체 코드는 별도 검증 범위다.
- API 실패 지적 2–4에 대한 자동 회귀 테스트는 현재 테스트 묶음에 없으며, 위 근거는 입력/저장 코드 경로 대조다.
- work-history 디렉터리는 git repository가 아니어서 안내된 `git pull --ff-only`가 적용되지 않았다. 프로젝트 요구와 커밋 기준으로 검증했다.
