# Research Lab v2 frontend compare-series 독립 검증

- 판정: **FAIL — 서버 연결은 추가됐으나 pending 중 로컬 결과를 서버 결과처럼 먼저 노출함. 서버 오류 시 비교와 무관한 기능도 숨겨짐.**
- 대상 frontend full SHA: `c3fbf3613ee699092279bdad27e363eaa9ef00a3`
- 비교 기준: `9521cc171ce555332c48c9e50acdf5fcef86eee3`
- 대상 backend checkout HEAD: `c121c01f27b16c69529cc88344d9f543190956be`
- 구현자 UUID: `d595eb1f-0aa2-4709-ac8d-309a14acaaa5` (직접 재실행 요청/배정 없음)
- 검사 범위: frontend `ResultAnalysisView.tsx`, `research.test.tsx` 및 compare-series 호출·정규화, backend API payload와 기존 계약 정본. 브라우저 A~D 인수 및 배포는 범위 밖.

## 지금 교정할 항목

### P2 — compare-series 응답 전 서버 값이 아직 없는데 로컬 계산 결과를 즉시 표시

`ResultAnalysisView.tsx`의 `series`는 `compareQ.data`가 비어 있고 오류도 없으면 바로 `deriveSeries(detail, baselineId)`를 사용한다. 이는 pending을 서버의 구형 shape/null 응답처럼 취급한다. 그 결과 `compareQ.isPending && !series` 로딩 조건이 false가 되고, 실제 서버 응답 전부터 결과 분석 차트가 나타난다. 지시된 fallback 조건은 실제 응답이 null/구형 shape일 때다. 서버 지연 중 로컬 계산 결과가 서버 응답과 다르면 잘못된 값이 잠시 사용자에게 제시된다.

독립 재현: 기존 테스트 harness의 API handler만 deferred promise로 대체하고 component를 실제 렌더했다. 의도한 검사는 pending 동안 결과 heading과 본문이 없어야 한다는 것이었으나, `BM25 k1 스윕` heading이 이미 렌더되어 기대 검사가 실패했다.

```text
$ node_modules/.bin/vitest run src/pages/research/compare_pending.temp.test.tsx
FAIL ... does not render provisional local series before server response
AssertionError: expected <h2></h2> to be null
Received: <h2>BM25 k1 스윕</h2>
1 failed
```

이는 테스트용 지연 API를 사용한 component 재현이며 브라우저/실제 네트워크 지연 측정은 아니다. Baseline/축 변경으로 query key가 바뀔 때도 새 응답 전 fallback이 계산되는 코드는 같지만, 별도의 화면 전환 재현은 하지 않았다.

### P2 — compare 요청 실패가 비교에 의존하지 않는 결과물·채택 영역까지 가림

HTTP 오류 시 테스트에서 오류 메시지와 재시도 버튼은 렌더된다. 그러나 본문 header, 최신 분석, 표/차트, `ArtifactList`, `BestResultPanel`은 모두 `{detail && series && <> ... </>}` 안에 있다. 첫 compare 요청이 실패하거나 query key가 바뀐 뒤 실패하여 `series`가 null이면, 상세 요청은 이미 성공했어도 이 영역들이 전부 빠진다. 실패/취소 실행 섹션은 조건부 블록 밖에 남는다. 결과물 조회와 최선 결과 채택은 compare-series 계산 자체에 필요하지 않으므로 오류 격리가 과도하다.

TanStack Query가 같은 key background refetch 실패 때 보유 중인 data를 유지하는 경우에는 기존 서버 차트도 계속 보이고 `compareQ.error` 안내도 함께 보인다. 따라서 오류 자체를 숨기지는 않는다. 다만 화면에 보이는 차트가 stale임을 직접 표시하는 문구는 없으며 이 조합의 사용자 표시를 동적으로 확인하지 못했다.

## 검증 결과

### PASS — 서버 compare-series가 실제 component chart/Pareto에 도달

신규 `research.test.tsx`의 fake server 응답은 detail의 로컬 계산 결과와 의도적으로 다른 delta(+77.7%)와 Pareto 1개 점(로컬 detail에는 두 설정)을 준다. 실제 `ResultAnalysisView` 렌더에서 `▲ +77.7%` 및 산점도 1개 점을 검사하고, `/experiments/e1/compare-series` 호출도 확인한다. unavailable 사유 원문과 단일 지표 문구도 사용자 화면에서 검사한다. 이는 component/API stub 검증이지 backend HTTP 통신 검증은 아니다.

```text
$ node_modules/.bin/vitest run src/pages/research/research.test.tsx
Test Files  1 passed (1)
Tests       25 passed (25)
```

신규 일곱 테스트가 단순 helper 반환만 검사하는 것은 아니다. 실제 화면에서 텍스트·차트 접근성 라벨을 확인하는 테스트가 포함되어 있다. 다만 pending, 성공 후 analyze invalidation/refetch, background refetch 오류 시 stale 표시는 이 suite에 없다.

### PASS — baseline/axis query key와 API URL query 연결 코드 및 축 변경 component test

컴포넌트 query key는 `[..., 'compare-series', expId, baselineId, px, py]`; API 함수는 각각 `baseline_config_id`, `pareto_x_metric`, `pareto_y_metric`를 URL query로 직렬화한다. 테스트에서 가로축 변경 후 `pareto_x_metric=recall_at_k`, baseline 변경 후 `baseline_config_id=c2`가 들어간 요청을 확인한다. `expId`는 URL path의 experiment id다. 브라우저 주소창의 `px`/`py` 상태 동기화는 별도 요구/검증하지 않았다.

### PASS — real backend API payload를 격리 PostgreSQL + ASGI에서 확인

정본 `refactor-v2.md` §7.1의 compare-series 경로와 analyzer 구현을 기준으로 직접 ASGI 요청했다. Python/import 경로는 검증 checkout 내부다. 임시 PostgreSQL은 테스트 fixture가 만든 loopback 포트/`ttakkari_test` DB이며 운영 DB·GPU·유료 서비스는 사용하지 않았다.

```text
$ ./.venv/bin/python - <<'PY'  # PYTHONPATH=tests:.; ASGITransport + fixture temporary PostgreSQL
runtime: .../back-research-v2/.venv/bin/python
imports: .../back-research-v2/src/ttakkari/__init__.py; .../back-research-v2/src/ttakkari/research/__init__.py
GET /api/research/experiments/{id}/compare-series?baseline_config_id={baseline}&pareto_x_metric=latency_ms&pareto_y_metric=decode_tps
exit 0
```

실제 응답에서 `delta_bars`는 속도 `+15 tok/s, +50%`, 지연 `−10 ms, −20%`; `unit`, `direction`, `is_proxy`가 각 항목에 있었다. treatment에 빠진 proxy 지표는 `value/abs_delta/rel_delta_pct=null`, `n=0`으로 돌아왔다. `metric_bars`도 해당 점을 `value=null,n=0`으로 표현했다. Pareto는 요청 축 `latency_ms × decode_tps`, `ms/tok/s`, 각 방향, 서버 점의 좌표를 반환했다.

별도로 backend payload와 같은 wire shape를 프론트 `normalizeSeries`에 넣은 임시 component-independent 검사를 실행했다. delta, proxy/unit/direction, null/n=0 및 Pareto 메타데이터가 정규화 결과에서 유지됨을 확인했다. 이는 실제 HTTP 응답 객체를 프로세스 간 전달한 end-to-end 검사가 아니며 실제 payload 값으로 UI normalization까지 한 번에 잇는 경로는 미검증이다.

```text
$ node_modules/.bin/vitest run src/pages/research/normalize_payload.temp.test.ts
Test Files  1 passed (1)
Tests       1 passed (1)
```

### PASS — 오류 안내와 fallback 경로의 좁은 component 검사

신규 테스트는 compare HTTP 실패에서 오류 원문과 재시도 문구를 확인한다. 구형/빈 shape `{}`는 `normalizeSeries`가 null을 반환하는 stub 응답으로 로컬 fallback delta(+25%)를 확인한다. API 요청 자체가 실패한 경로는 로컬 계산으로 성공 표시하지 않는 코드이며 오류 UI도 테스트됐다.

### 코드상 갱신 연결은 있으나 동적 미검증

analyze 성공 handler가 `KEY`와 compare-series prefix를 invalidate하고 detail 및 compare query를 refetch하도록 구현되어 있다. 다만 신규 suite에 analyze mutation 이후 호출/새 응답을 검사하는 회귀 테스트는 없다. 별도 임시 refetch 시험은 mock 응답의 repeat-distribution label을 빼서 `RepeatDistributionPlot/ChartFrame` 렌더에서 `text.length` 오류가 나기 전 중단됐다. 그 실패는 product behavior의 증거가 아니므로 갱신 통과로 계산하지 않는다.

## 명령과 환경 경계

- Frontend direct commands: `node_modules/.bin/vitest run src/pages/research/research.test.tsx`; `node_modules/.bin/tsc -b`; `node_modules/.bin/vite build` — 모두 exit 0. Build는 chunk >500 kB 경고가 있으나 계약 판정과 무관.
- Frontend diff integrity: `git diff --check 9521cc171ce555332c48c9e50acdf5fcef86eee3 c3fbf3613ee699092279bdad27e363eaa9ef00a3` — 출력 없음. checkout은 대상 SHA, working tree clean.
- Backend payload interpreter/import 일치 확인 및 실제 ASGI payload probe — 위 참조. 전체 backend 99-test 계약 묶음은 교정 전 기존 보고서의 결과이므로 이번 교정 검증으로 재사용하거나 반복하지 않았다.
- 한 임시 pytest harness를 `/private/tmp`에서 수집하려 한 시도는 pytest collection 중 sandbox가 `/private/tmp/codex-daemon-501` stat을 `PermissionError: [Errno 1] Operation not permitted`로 거부해 exit 4였다. 이는 해당 pytest discovery 단계의 격리 환경 오류다. 동일 discovery를 우회하려고 재시도하지 않았고, 별도 stdin ASGI probe는 성공했다.
- analyze/refetch 임시 렌더 probe는 불완전 mock fixture의 ChartFrame 오류로 실패, 미검증. browser, deployed frontend/backend, production DB, 전체 A~D 인수는 실행하지 않았다.

## 소스 변경 및 커밋 범위

Frontend/backend source 변경은 없다. 임시 프론트 테스트 파일은 제거했다. 저장·커밋 대상은 이 보고서 하나다. 검증 대상 frontend를 수정하거나 push/deploy하지 않았다.
