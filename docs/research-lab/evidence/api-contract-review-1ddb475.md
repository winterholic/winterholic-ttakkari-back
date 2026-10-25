# Research Lab v2 API 계약 독립 검증

- 판정: **부분 실패 — 백엔드 계약 통과, 프론트 compare-series/Pareto 연결 미완료**
- 범위: 좁은 API 계약 검증. 전체 A~D 흐름 및 브라우저 인수는 포함하지 않음.
- 정본: docs/research-lab/refactor-v2-requirements.md, refactor-v2.md; 설계 커밋 bf5c407f0a606481a42241b7dced78819829ac6d.
- 대상 backend: 1ddb47577bca9d59037f282a5268834d11ea1083. 대상 frontend 실제 HEAD: 9521cc171ce555332c48c9e50acdf5fcef86eee3. 제시된 9521cc1e95fae4ca5627768564db5cb669cf00fa는 bad object라 사용하지 않음.
- 검증 당시 backend HEAD 3df79b9541fb502d418137a321ac756d399ff4cf (카탈로그 테스트/증거 추가 커밋 포함), frontend HEAD는 대상과 동일. 1ddb 구현부터 현재 검증 HEAD까지 src/ttakkari 변경 없음(git diff --stat 1ddb... HEAD -- src/ttakkari 출력 없음).
- 대상 두 worktree는 시작 시 모두 clean. backend research-v2는 origin/main 대비 ahead 29 / behind 8, frontend ahead 3 / behind 3. 별도 back/ 메인 체크아웃에는 다수 미커밋 변경이 있어 보존했고 back/ 및 front/ 메인 체크아웃에는 쓰지 않음.
- Import 증거: backend .venv Python은 해당 worktree의 .venv/bin/python, ttakkari.__file__은 back-research-v2/src/ttakkari/__init__.py, ttakkari.research.__file__은 같은 worktree의 research/__init__.py. 따라서 editable import 대상은 검증 backend checkout. pip 모듈은 없어 pip show는 불가했지만 직접 import 확인은 통과.
- API 실행 범위: httpx AsyncClient + FastAPI ASGITransport + tests/conftest.py의 격리 임시 PostgreSQL. 배포 HTTP 서버는 아님. 별도 0/누락 대조도 실제 API 및 DB 저장/조회 경로로 실행.
- 브라우저/배포 런타임은 실행하지 않음. 전체 브라우저 인수는 별도 다음 단계로 명시됨.

## 판정 요약

1. **PASS — 축 검색 및 facets**: 같은 축 comma/repeated query는 OR, 축 사이 AND. 실제 API로 만든 행 및 없는 값과 대조. singular/plural facets 일치. offset을 포함한 카탈로그 union 케이스 포함.
2. **PASS — experiment 및 provenance**: create/detail 래퍼와 flat 호환 필드, 최상위·run별 measurements, 외부 provenance 저장 및 조회 확인.
3. **PASS — backend 다중 지표 분석**: metric_bars/delta_bars/repeat_distributions/sweep_lines/ablation_impacts, baseline, 단위·방향, 실패/non-overall 제외, 기존 필드가 계약 테스트에서 통과. Pareto unavailable reason 반환. 0·누락 별도 대조는 아래 참조.
4. **PASS — dashboard/실행 API**: readiness·hardware/work-scope, rejected best result, active lock, run 오류 필드 테스트 통과. GENERATION_REJECTED runner 케이스는 generation gate를 mock으로 차단하므로 회귀 테스트일 뿐 실제 배포 runtime 증거는 아님.
5. **FAIL — 프론트 compare-series 계약 미소비**: 현재 분석 화면은 getExperiment의 measurements로 deriveSeries 및 paretoFor를 직접 계산하며 getCompareSeries/normalizeSeries를 호출하지 않음. 따라서 서버 pareto_status/pareto_unavailable_reason는 UI에 도달하지 않고 비교 불가 상태는 일반 안내문만 표시. detail 경로로 다중 지표를 그려 데이터 자체가 사라지는 현상은 확인하지 않았지만, API→UI 계약 정합은 미완료.

## 재현 및 실행 증거

백엔드 명령:

    ./.venv/bin/python -m pytest -q tests/test_research_v2_api_contract.py tests/test_research_v2_e2e.py tests/test_research_v2_core.py tests/test_research_adapters.py tests/test_research_lab.py

출력:

    ........................................................................ [ 72%]
    ...........................                                              [100%]
    99 passed in 4.37s

축 관련 실제 API 테스트: test_method_search_ors_values_within_an_axis_and_ands_across_axes, test_catalog_multi_target_equals_union_of_single_targets, test_facets_use_singular_axis_keys_and_keep_plural_aliases. 고유 target/layer 행을 API에 저장하고 comma vs repeated 결과 ID, AND 교차, 없는 값 대조를 확인. catalog union은 offset 100 단위로 끝까지 페이지 조회.

Experiment 관련 실제 API+DB 테스트: test_experiment_detail_wraps_experiment_and_includes_measurements는 생성/상세 wrapper, flat 필드, run별 및 최상위 measurements 확인. test_external_report_returns_measurements_and_provenance 및 test_runs_expose_provenance_wherever_they_are_listed는 외부 provenance와 measurement meta의 저장·재조회 확인. tests/test_research_v2_e2e.py::test_vertical_e2e_rag_research_flow는 실제 retrieval_eval adapter로 생성→실행→조회→compare-series→채택. mock 결과 주입 없음.

Analyzer 관련 통과 테스트:
- test_compare_series_covers_every_measured_metric_primary_first: 세 지표, 단위/방향/개선 판정, baseline, 기존 config_id 및 per-config 필드.
- test_compare_series_repeat_distribution_and_sweep_lines 및 test_compare_series_ablation_impacts_for_each_metric: 반복 통계, sweep, 방향별 ablation 영향.
- test_compare_series_ignores_failed_runs_and_non_overall_slices: 실패 run과 non-overall slice 제외.
- test_compare_series_pareto_axes_and_unavailable_reason 및 test_compare_series_single_metric_reports_pareto_unavailable_instead_of_empty: Pareto 축·단위·방향·unavailable 이유.
- dashboard/run 테스트: test_dashboard_items_match_run_and_best_result_shapes, test_dashboard_host_readiness_comes_from_adapters_not_constants, test_dashboard_executable_methods_counts_runnable_catalog_entries, test_runs_list_active_locks_follow_running_runs.

0/누락 별도 negative control은 테스트 소스 파일 변경 없이 inline Python으로 임시 PostgreSQL을 초기화하고 실제 API에 실험과 외부 보고값을 넣어 compare-series를 조회함. 출력:
- transport=FastAPI ASGITransport; db=ephemeral PostgreSQL test cluster
- baseline m=0: abs_delta=1 rel_delta_pct=null (finite, no divide-by-zero)
- missing treatment n: value=null n=0; Pareto status: available | reason: None
즉 0 baseline 상대 변화율은 null이고, 설정별 누락은 null/0으로 표현됨. 추가 대조에서 baseline에는 quality만, treatment에는 latency만 저장해 두 축이 함께 있는 configuration이 없을 때 출력:
- API status: unavailable
- reason: 두 지표(latency, quality)가 모두 측정된 설정이 없어 Pareto 분석을 생성할 수 없습니다.

프론트 연구 테스트:

    node_modules/.bin/vitest run src/pages/research/research.test.tsx

    Test Files  1 passed (1)
    Tests  18 passed (18)

타입체크 및 production build:

    node_modules/.bin/tsc -b && node_modules/.bin/vite build

출력 마지막: ✓ built in 328ms; PWA files generated. 500 kB 초과 minified chunk 경고가 있었으나 이번 계약 판정과는 별개. 프론트 테스트는 testkit/stub 기반이므로 실제 API 통신 증거로 취급하지 않음.

프론트 소비 경로 확인:
- src/pages/research/views/ResultAnalysisView.tsx:38-42에서 detail 측정값으로 deriveSeries를 호출. 140-143에서 별도 paretoFor 결과 또는 일반 안내를 그림.
- src/pages/research/api.ts:172-175의 getCompareSeries는 서버 endpoint와 normalizeSeries를 제공하지만 분석 화면에서 호출되지 않음. getCompareSeries/normalizeSeries/pareto_unavailable_reason 검색 결과는 api/types/helper 정의에만 존재.
- src/pages/research/series.ts:15-39의 normalization은 Pareto status와 reason을 읽을 수 있으나 실제 화면 호출자가 없음.

최소 재현: AB 실험 생성 후 baseline에 quality만, treatment에 latency만 external-report로 저장. GET /api/research/experiments/{id}/compare-series?pareto_x_metric=latency&pareto_y_metric=quality 는 위 unavailable reason 반환. 그러나 ResultAnalysisView는 해당 GET을 하지 않고 일반 안내를 보여 줌. 최소 보정 방향은 결과 분석 query를 getCompareSeries로 연결하고, normalizeSeries의 Pareto reason을 화면에서 소비하며 old-shape fallback은 deriveSeries로 유지하는 것.

## 미실행 및 경계

- 배포 앱에 HTTP 요청하거나 브라우저에서 렌더링하지 않음. ASGI + 격리 PostgreSQL 범위만 확인.
- generation 전환 실제 runtime 재현 미실행. GENERATION_REJECTED 테스트는 mock gate임. 화면 경로는 코드에서 확인했지만 브라우저 렌더 인수는 unverified.
- pip show는 venv에 pip 모듈이 없어 미실행. 직접 import 경로는 확인됨.
- 전체 A~D 및 브라우저 인수는 요청 범위 밖.

## 변경 및 git

검증 코드 변경 없음. 저장/커밋 대상은 이 보고서만. push 및 배포하지 않음.

## 후속 시점 확인 (2026-10-25 06:20 UTC)

- 재개 시점 backend HEAD는 `ac2e54f48533711bf353d7865bdc5650eaeb769a`이며, 앞선 검증 당시 backend HEAD `3df79b9541fb502d418137a321ac756d399ff4cf` 이후에는 이 API 보고서 커밋만 추가됐다. 그 검증 스냅샷에는 동료의 `catalog-review-1c41426.md`와 앞선 `1c41426` catalog test fix가 이미 포함돼 있었다. frontend HEAD는 계속 `9521cc171ce555332c48c9e50acdf5fcef86eee3`이다. 두 worktree의 `git status --porcelain=v1` 출력은 비어 있었다.
- `git diff --name-status 1ddb47577bca9d59037f282a5268834d11ea1083..HEAD -- src/ttakkari/research seed/readiness.py`는 출력이 없었다. 따라서 고정 대상 백엔드의 API 소스는 바뀌지 않았고 기존 API 계약 결과는 유효하다.
- 교정 알림의 short SHA `8730ff9d`는 이 checkout에서 `git cat-file -t 8730ff9d`로 조회했을 때 `fatal: Not a valid object name 8730ff9d`를 반환했다. 이는 현재 checkout에서 해당 교정 커밋을 확인하지 못했다는 뜻이다. 별도 worktree의 코드 상태는 읽거나 검증하지 않았다.
- 이전 backend 테스트 묶음(`tests/test_research_v2_api_contract.py`, `tests/test_research_v2_e2e.py`, `tests/test_research_v2_core.py`, `tests/test_research_adapters.py`, `tests/test_research_lab.py`, 99 passed)은 2026-10-25 06:17:35 UTC의 교정 배정 알림보다 먼저 실행됐다. 이 중 core/readiness 관련 결과도 교정 전 스냅샷 결과다. 교정 후 실행으로 계산하지 않는다.
- 이번 재개에서는 고정 대상 코드가 바뀌지 않은 점만 확인하고 테스트를 다시 실행하지 않았다. 교정 변경 후 promotion/readiness 테스트는 **미실행·미검증**이며, 고정 대상 SHA 검증과 혼합하지 않는다. 새 교정 커밋을 별도 검증하는 단계에서 해당 코드와 테스트 시점을 다시 기록해야 한다.
