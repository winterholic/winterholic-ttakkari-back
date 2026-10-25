# Backend Stage 2 독립 검증 — `c707aea`

- 검증일: 2026-10-25
- 대상 checkout: `back-research-v2`, HEAD `c707aea349905c78f11492b036341dc7a23bcdfa`
- frontend reference: `d574b6ee39d8456dd0a4b2d17bdc81fe3e7cc32b` (이 검증에서는 checkout/수정하지 않음)
- 기준: [요구사항](../refactor-v2-requirements.md), [설계](../refactor-v2.md), [통합 stage 2 review](integration-stage2-review-65de76a.md), 기존 [planner reproduction](repro-stage2-planner-validation.py), [external-source reproduction](repro-stage2-external-source.py)
- Python/import 경로: `PYTHONPATH=src .venv/bin/python`; 출력된 실제 module은 `.../back-research-v2/src/ttakkari/research/planner.py`, `.../src/ttakkari/research/adapters/external_report.py`.
- 변경 제한: 제품 소스 수정 없음. 이 증거 문서만 추가.

## 실행 요약

| 검증 | 결과 | 증거 범위 |
|---|---|---|
| DB identity / schema bootstrap | PASS | 표준 주입 `TTAKKARI_TEST_DB`; SQL `current_database()=ttakkari_test`, `current_user=ttk_test`. 기존 schema가 비어 있어 `CREATE EXTENSION IF NOT EXISTS`와 `Base.metadata.create_all`을 한 번 실행했다. `drop_all`/전체 테스트 fixture는 실행하지 않음. |
| Planner unknown adapter / validator exception | PASS | 기존 reproduction과 8개 직접 회귀 항목. In-memory DB/adapter doubles; DB 검증 아님. |
| `external_benchmark` title/locator 필수 | PASS | 기존 adapter reproduction, 실제 ASGI+Postgres에서 누락 요청 422 및 run/measurement 0건. |
| title+URL 저장 후 fresh read | PASS | 실제 ASGI POST → 커밋 → 별도 GET 요청/session. top-level, raw, environment, measurement meta의 provenance/title/URL 일치. |
| nested `source_locator` 입력 | PASS | 실제 ASGI+Postgres. raw `source_locator`를 받아 응답의 `source_url`로 네 위치에 round-trip. |
| `manual_evaluation` / `imported_log` 무출처 | PASS | 실제 ASGI+Postgres, 둘 다 201. |
| 상위와 nested 출처 충돌 | **FAIL** | 실제 ASGI+Postgres+fresh GET. run/raw에는 상위 출처가 남지만 environment와 measurement meta에는 각각 다른 nested 출처가 남음. |
| migrated v1 read | PASS (serializer) | 기존 legacy-shaped `migrated_v1` row에 대한 `run_out` 확인. 실제 v1 DB row/migration은 실행하지 않음. |

## 명령과 관측 출력

### DB 확인 및 bootstrap

DB identity는 URL을 연결하기 전에 `ttakkari_test`/`ttk_test`로 확인했고, 접속 후에도 같은 SQL identity를 확인했다. 비밀번호와 전체 URL은 출력하거나 보고서에 기록하지 않았다. 첫 읽기 결과는 `research_tables: []`, `public_table_count: 0`이었다. 앱 metadata를 비파괴 생성했다(테이블 삭제/초기화 없음).

실행 형태:

```sh
PYTHONPATH=src .venv/bin/python - <<'PY'
# URL database/user identity 검사 → SELECT current_database(), current_user
# → CREATE EXTENSION IF NOT EXISTS vector/pg_trgm
# → Base.metadata.create_all (drop_all 없음)
PY
```

출력:

```text
pre_bootstrap_identity: ttakkari_test ttk_test
bootstrap_ok: public_tables=115 research_tables=14 identity=('ttakkari_test', 'ttk_test')
```

별도 pytest fixture는 `tests/conftest.py`에서 `Base.metadata.drop_all`, 전체 create, 매 테스트 TRUNCATE를 수행하므로 사용하지 않았다.

### Planner 및 adapter regressions

```sh
PYTHONPATH=src .venv/bin/python docs/research-lab/evidence/repro-stage2-planner-validation.py
PYTHONPATH=src .venv/bin/python docs/research-lab/evidence/repro-stage2-external-source.py
PYTHONPATH=src .venv/bin/python tests/test_stage2_defects_fix.py
```

주요 출력:

```text
missing_required: valid=False
invalid_sweep_member: valid=False
validator_exception: valid=False
unknown_adapter: valid=False
validate_config_errors: [출처 제목 필요, 출처 위치 필요]
All 8 regression tests passed successfully!
```

마지막 명령은 이 파일의 `__main__`에서 직접 부르는 8개 항목이다. `StubDb`/직접 handler·adapter 실행이므로 PostgreSQL/HTTP 증거로 계산하지 않았다. planner reproduction은 실제 planner 함수를 부르지만 adapter registry double과 in-memory DB를 쓴다.

### 실제 ASGI + PostgreSQL probes

각 요청 실행 전 URL identity와 접속 identity를 검사했다. `httpx.ASGITransport(app=app)`로 실제 앱 라우트를 호출했고, auth는 테스트 전용 JWT로 통과했다. 설정은 해당 자식 프로세스에만 `TTAKKARI_ENV=dev`, `TTAKKARI_NO_ENV_FILE=1`, `TTAKKARI_DATABASE_URL=$TTAKKARI_TEST_DB`로 고정했다. 요청 사이의 DB 조회는 새 connection이며 run GET은 새 요청이므로 작성 세션과 분리되어 있다.

실행 형태:

```sh
PYTHONPATH=src .venv/bin/python - <<'PY'
# ASGI: study/experiment 생성
# external_benchmark 출처 누락 POST, POST 전후 SQL run/measurement count
# URL 또는 nested raw_output.source_locator를 포함해 POST, 새 요청 GET /runs/{id}
# top-level/nested 출처 충돌을 포함해 POST 후 새 요청 GET /runs/{id}
# manual_evaluation/imported_log 무출처 POST
PY
```

핵심 실제 출력:

```text
probe_identity: ttakkari_test ttk_test
missing_source: 422 rows_before= (0, 0) rows_after= (0, 0)
valid_source_status: 201
fresh_session_source_fields: {
  'top': ('Independent Probe Paper', 'https://example.invalid/paper', 'external_benchmark'),
  'raw': ('Independent Probe Paper', 'https://example.invalid/paper', 'external_benchmark'),
  'env': ('Independent Probe Paper', 'https://example.invalid/paper', 'external_benchmark'),
  'meta': ('Independent Probe Paper', 'https://example.invalid/paper', 'external_benchmark')}
run_execution_origin= external_report; env_execution_origin= external_report
locator_only_status: 201
locator_roundtrip: {'top': 'doi:10.1234/example', 'raw': 'doi:10.1234/example', 'env': 'doi:10.1234/example', 'meta': 'doi:10.1234/example'}
manual_evaluation_without_source_status: 201
imported_log_without_source_status: 201
```

`source_locator`는 `ExternalReportIn`의 top-level field가 아니라 nested raw/environment에서 허용되는 입력을 확인했다. 응답은 normalize된 `source_url`로 읽혔다.

충돌 probe는 top-level `Top Level Title`/`https://example.invalid/top`, raw `Nested Raw Title`, environment `Nested Env Title`, measurement meta `Nested Metric Title`을 보냈다.

```text
conflict_roundtrip: {
  'top': ('Top Level Title', 'https://example.invalid/top'),
  'raw': ('Top Level Title', 'https://example.invalid/top'),
  'env': ('Nested Env Title', 'https://example.invalid/env'),
  'meta': ('Nested Metric Title', 'https://example.invalid/meta')}
consistent= False
```

이는 실제 저장 후 API 재조회에서 확인한 일관성 결함이다. 동일 레코드 내 출처를 top-level, environment, measurement meta가 서로 다르게 설명한다. 관련 값 충돌 정책이 정해져 있지 않아 코드 변경이나 허용 기준은 제안하지 않는다.

## API contract helper 판정

`tests/test_research_v2_api_contract.py`의 `report()` helper는 `provenance="external_benchmark"`를 기본으로 보내면서 title/URL을 생략하고 201을 assert한다. 같은 payload를 실제 API에 보낸 결과는 위와 같이 422이고 DB row도 생기지 않았다. 이는 새 필수 출처 규칙과 모순되는 **기존 helper/test 입력의 요구값 부족**이다. 요구 규칙을 되돌리는 runtime 회귀 증거가 아니다. 실제 정상 payload와 legacy read는 별도 확인했다.

실제 외부 출처 자체의 진위는 이 테스트에서 검증하지 않았다. fixture에 있던 FlashInfer `185.4` 및 URL은 실제 논문 증거로 간주하지 않았고, 이번 API 값은 동작 검증용 synthetic payload다.

## 과거 read 호환성

legacy-shaped row에서 `run_out`이 `execution_origin='migrated_v1'`과 기존 note를 유지하고, 존재하지 않는 `source_title`을 새로 만들지 않는 것을 확인했다:

```text
legacy_read_compatibility: PASS migrated_v1; source_title_absent= True
```

확인 범위는 serializer의 backward-compatible shape다. 실제 v1 migration된 PostgreSQL row, front `d574b6e…` 동작은 이 검증에서 확인하지 않았다.

## 실행 오류 / 미검증

- 최초 DB probe 명령은 시스템 `python3`에서 실행되어 `ModuleNotFoundError: No module named 'sqlalchemy'`를 반환했다. 이는 기본 system Python 환경의 import 단계 실패였고, 프로젝트 `.venv/bin/python`으로 실행 환경을 바로잡아 DB에 연결했다. DB bootstrap 실패는 아니었다.
- 첫 ASGI probe는 API 응답의 `configurations`가 `experiment` wrapper 안에 있다고 가정해 probe 코드에서 `KeyError`가 났다. 응답 schema를 확인해 위치를 고친 후 새 고유 experiment로 재실행했다.
- 다음 probe 한 번은 잘못된 endpoint 문자열로 404가 났고, 수정 후 재실행했다. 또한 한 assertion이 `execution_origin`이 raw/meta에도 존재한다고 과하게 가정해 멈췄다. 계약된 provenance/source 필드와 run/environment execution origin을 분리해 확인했다. 이들은 probe 작성 오류이며 제품 오류 증거로 취급하지 않았다.
- DB bootstrap, 실제 `external_benchmark` 정상 저장, `source_locator`, `manual_evaluation`, `imported_log`, source conflict는 각각 성공한 scoped probes로 다시 확인했다. Bootstrap 자체는 재시도하지 않았다.
- 전체 suite, API contract 전체 파일, 브라우저, A~D workflow, frontend 동작, top_k는 실행하지 않았다. 직접 `test_stage2_defects_fix.py`의 handler 항목은 `StubDb` 기반이라 actual PG 검증을 대체하지 않는다.
- 현재 실행은 DB row를 남겼다. 테스트 DB를 초기화/삭제하지 않았다.

## checkout 및 변경 확인

```text
검증 대상 source SHA: c707aea349905c78f11492b036341dc7a23bcdfa
최초 checkout: ## research-v2...origin/main [ahead 49, behind 7]
처음 porcelain: (empty)
PYTHONPATH=src actual import: .../back-research-v2/src/ttakkari/...
product source 변경: 없음
frontend checkout/변경: 없음
```

작업 도중 branch HEAD가 `38147a948bf371061131b53f6c9e990ad547f673`로 이동했다. 해당 commit은 `tests/test_research_v2_api_contract.py`만 추가했으며 `git diff c707aea..38147a9 -- src`는 비어 있었다. 따라서 probe가 import한 product source는 지정된 `c707aea`와 동일했다. 최종 report commit은 이 test-only commit 위에 있으며 변경 파일은 이 보고서 하나다.

