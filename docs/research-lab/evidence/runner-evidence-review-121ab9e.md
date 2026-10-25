# Runner 독립 실행 증거 검토

- 대상 커밋: `121ab9e2bb960d8c7650dc1d27de69778e2db270` (parent `b670fbbd9ac4ee9ae895ab34dbec953c64d1feaf`)
- 검토일: 2026-10-25 (KST)
- 범위: 지정 backend worktree의 runner/artifact 변경과 core fixture 변경. 제품 코드는 수정하지 않음.
- DB: 실제 PostgreSQL 테스트 DB로 실행. identity 확인 결과 `database=ttakkari_test user=ttk_test host=127.0.0.1/32 port=51950`.
- 판정 요약: 원래 세 DB 반례와 테스트 묶음은 PASS. `register_file` 사후 예외는 FAIL (측정 rollback 및 orphan 복사 파일). 측정 변환 예외는 Run terminal failure와 adapter raw/env 보존 확인. 배포·push는 실행하지 않음.

## 항목별 판정

| 항목 | 판정 | 실행 결과 |
|---|---|---|
| 표준 DB identity | PASS | `ttakkari_test / ttk_test / 127.0.0.1` 일치. identity 검사에서만 연결 정보를 출력했고 DSN/비밀번호는 출력하지 않음. |
| 두 번째 선언 파일 누락 | PASS | PostgreSQL에서 `failed / artifact_file_missing`; raw/env 및 RTF 측정 1건 유지; `register_file` 호출 0, Artifact·ResearchArtifact 신규 row 0, 저장 파일 증가 0. |
| 잘못된 legacy UUID | PASS | `failed / artifact_invalid`; raw/env와 측정 1건 유지; 등록 호출/Artifact row/ResearchArtifact row/저장 파일 증가 모두 0. |
| 존재하지 않는 legacy UUID | PASS | `failed / artifact_not_found`; raw/env와 측정 1건 유지; 등록 호출/Artifact row/ResearchArtifact row/저장 파일 증가 모두 0. |
| 첫 등록 후 다음 `register_file` 예외 | **FAIL** | 두 번째 등록 함수가 복사 완료 직후 예외. Run은 `failed / RuntimeError`, raw/env는 유지되나 rollback으로 측정 0건이 됐다. 등록 함수가 반환한 두 Artifact ID는 DB에 없고 새 Artifact row 및 ResearchArtifact row도 0건인데, 물리 저장 파일 2개가 남아 orphan 발생. |
| `to_measurements` 예외 | PASS (failure handling) | Run `failed / ValueError`, raw/env 보존, Measurement/Artifact row 및 저장 파일 증가 0. 변환 자체가 실패했으므로 측정값은 없음. |
| 정상 실행 및 같은 파일 dedupe | PASS | Run `succeeded`, 측정 1건, Artifact 1행과 ResearchArtifact 1행, 저장 사본 1개 존재. |
| 요청한 회귀 테스트 묶음 | PASS | 아래 명령으로 124개 통과. |
| core fixture 출처 규칙 | PASS (의미 유지) | 원래 검증 assertion은 유지. 외부 benchmark fixture는 합성 제목과 `.invalid` URL로 source 필드를 명시하고, 수작업 생성 비교 수치는 `manual_evaluation`로 표시. provenance 허용/왕복 검증은 별도 테스트에 남아 있음. |
| Ruff 신규 진단 비교 | PASS | 변경 파일에서 새 진단 없음. 대상 3개 파일 현재 17건은 parent 기준 19건이던 기존 진단의 잔여이며, runner에서 기존 진단 2건이 사라짐. |
| 실패 Run 측정의 비교·최적 결과 유입 | PASS | analyzer는 `Run.status == "succeeded"` 행만 가져오며 그 run ID의 `overall` measurement만 집계. |

## PostgreSQL identity와 테스트 실행

Identity 명령:

```sh
.venv/bin/python - <<'PY'
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy import text
url = os.environ["TTAKKARI_TEST_DB"]
engine = create_async_engine(url)
async def main():
    async with engine.connect() as conn:
        row = (await conn.execute(text("select current_database(), current_user, inet_server_addr()::text, inet_server_port()"))).one()
        print(f"database={row[0]} user={row[1]} host={row[2]} port={row[3]}")
    await engine.dispose()
asyncio.run(main())
PY
```

핵심 출력:

```text
database=ttakkari_test user=ttk_test host=127.0.0.1/32 port=51950
```

요청된 회귀 실행:

```sh
.venv/bin/python -m pytest -q tests/test_scenario_b_artifacts.py tests/test_research_v2_core.py tests/test_research_v2_e2e.py tests/test_research_v2_api_contract.py tests/test_research_adapters.py
```

```text
........................................................................ [ 58%]
............................................                             [100%]
124 passed in 6.39s
```

공통 pytest fixture가 격리된 `TTAKKARI_HOME` 아래 artifact store를 사용했다. 별도 임시 pytest case로 DB row와 store 디렉터리 전후를 비교했고, 그 임시 파일은 실행 후 제거했다.

## 반례 상세

### 원래 세 반례

다음 원래 반례는 모두 실제 PostgreSQL에서 runner를 실행해 확인했다. 관측된 상태:

```text
missing_second_file: failed / artifact_file_missing; raw_output.transcript=kept;
  environment_snapshot={engine:test}; measurements=[0.2]; register_calls=0;
  new_db_artifacts=[]; new_stored_files=[]
malformed_legacy_id: failed / artifact_invalid; measurements=[0.2]; no registration/rows/files
unknown_legacy_id: failed / artifact_not_found; measurements=[0.2]; no registration/rows/files
```

이는 이전 증거의 두 번째 파일 rollback/orphan과 없는 legacy FK의 `running` 고착을 이 커밋에서 해결한 범위에 대해 반증하지 못했다. 현재 실행에서는 두 번째 파일과 ID가 모두 복사 전에 확인되어 failure evidence가 commit된다.

### 추가 반례: `register_file` 복사 뒤 예외

첫 번째 파일은 정상 등록하고, 두 번째 호출은 원래 `register_file`이 파일을 복사한 뒤 `RuntimeError`를 던지도록 monkeypatch했다. 실행 출력:

```text
register_file_throws_after_copy: status=failed, error_code=RuntimeError,
  raw_output={transcript:kept}, environment_snapshot={engine:test}, measurements=[],
  registered Artifact IDs present in DB=[], new Artifact IDs=[], ResearchArtifact=0,
  new_stored_files=[.../0.wav, .../1.wav], calls=2
```

**FAIL 반례:** SQL rollback은 두 Artifact row와 앞선 ResearchArtifact insert를 제거하지만 이미 복사된 파일 두 개를 되돌리지 않는다. adapter 측정도 rollback되어 사라진다. 따라서 사전 검증은 누락 파일에서 orphan을 막지만, 등록 도중 예외가 난 이후의 파일 시스템 side effect는 원자적이지 않다.

### 추가 반례: 측정 변환 예외

`to_measurements`가 `ValueError`를 던지게 주입했다.

```text
to_measurements_throws: status=failed, error_code=ValueError,
  raw_output={transcript:kept}, environment_snapshot={engine:test},
  measurements=[], ResearchArtifact=0, new_stored_files=[]
```

Run이 terminal failure로 끝나고 adapter 출력/환경 정보가 남는다. 변환 함수가 실패했으므로 DB에 저장할 measurement 값은 만들어지지 않았다.

### 정상 경로/dedupe

동일 경로의 파일 두 선언으로 정상 실행했다.

```text
normal_dedupe: status=succeeded, measurements=[0.2], Artifact rows=1,
  ResearchArtifact=1, stored_exists=[true], new_stored_files=[.../one.wav]
```

## core fixture 변경과 출처 의미

`tests/test_research_v2_core.py`의 diff는 fixture payload에 source metadata를 더하거나 synthetic metric의 provenance를 `manual_evaluation`로 명시한다. 기존 endpoint status, 응답 필드, 측정 immutability, compare/Pareto, adoption 및 consistency assertion을 삭제하거나 약화하지 않았다.

- `test_external_report_integrity_and_measurement_immutability`, `test_adopt_best_result_and_method_promote`: 기존 `external_benchmark` provenance를 유지하면서 `Synthetic core fixture`와 `https://example.invalid/research-core-fixture`를 추가했다. 이는 출처 진위를 검증하는 값이 아니라 테스트용 합성 출처이며, 필요한 필드 존재 규칙을 통과시키는 용도다.
- compare/Pareto 및 regression metric fixture는 직접 구성한 수치이므로 `manual_evaluation`로 구분했다. 비교 집계/Pareto/assertion 대상은 그대로다.
- `tests/test_research_adapters.py::test_external_report_adapter_lifecycle`는 실재하는 외부 보고서 제목·arXiv URL이 있는 정상 config와 invalid provenance를 계속 검사한다. 이번 실행 회귀 묶음에 포함돼 통과했다.

따라서 테스트 의미를 약화한 우회로 보지 않는다. 다만 synthetic `.invalid` fixture 자체는 외부 출처 신뢰성이나 URL 검증 테스트가 아니다.

## Ruff 비교

```sh
.venv/bin/ruff check --output-format concise src/ttakkari/research/runner.py tests/test_research_v2_core.py tests/test_scenario_b_artifacts.py
```

```text
Found 17 errors. (15 fixable)
```

동일 파일을 parent `b670fbb` 내용으로 `ruff check --stdin-filename` 비교한 결과: runner parent 6건 → target 4건, core parent 9건 → target 9건, unchanged artifact test parent/target 각 4건. 새 진단은 0건이며 기존 진단이 순감 2건이다. Ruff 진단은 전부 이번 범위에서 새로 생긴 것이 아니다.

## 분석 집계 경로

`src/ttakkari/research/analyzer.py`의 `build_compare_series`는 먼저 `Run.experiment_id == experiment_id`와 `Run.status == "succeeded"`로 Run을 제한하고, 이 성공 Run ID들에 속하면서 `slice_key == "overall"`인 measurement만 선택한다. 이번에 실패 처리된 Run의 보존 measurement는 analyzer의 비교 series, distribution, Pareto 입력에 포함되지 않는다. 이 판정은 query 경로 코드 점검이며 별도 실패/성공 혼합 DB 분석 실험은 실행하지 않았다.

## 변경 및 배포 경계

이 검토에서 제품 코드 수정, push, deploy는 하지 않았다. 보고서 외 파일은 stage하지 않았다. 최종 커밋 SHA는 작업 종료 시 git 출력으로 확인한다.
