# Research Lab v2 frontend compare-series 재검증 (5d5719a + 20e69de)

- 판정: **PASSED** (이전 FAIL 2건 해소, 새 P1/P2 결함 없음. P3 관찰 3건은 아래에 따로 적음)
- 대상: frontend branch `research-v2`, 소스 `5d5719a1531dbe5b4a0a9e960f812b48fbbaf5ac`, 테스트 `20e69ded1dfb46d51326ff165dbecd4ec31e9294`
- 기준: `c3fbf36`, 이전 보고서 `compare-series-review-c3fbf36.md`
- 검증자: Claude (구현은 GPT 계열). 읽기와 테스트 실행만 했고 프론트 소스는 수정하지 않았다. 임시 반례 파일은 실행 후 삭제했고 `git status` 가 비어 있음을 확인했다.
- 판정 기준: 이전 보고서의 "pending 중 h2 부재"는 수정된 계약(실험 detail 헤더는 비교와 독립 표시)과 맞지 않아 쓰지 않았다. 기준은 "pending 중 비교 table/chart/delta/Pareto 가 로컬 계산으로 먼저 표시되지 않는가"이다.

## 항목별 결과

### 1. pending(undefined)과 legacy 성공(null) 구분 — PASS

- `src/pages/research/views/ResultAnalysisView.tsx:65-66`
  `hasSuccessfulLegacyResponse = compareQ.data === null && compareQ.dataUpdatedAt > 0`, `series = compareQ.data ?? (hasSuccessfulLegacyResponse && detail ? deriveSeries(...) : null)`.
  pending·disabled·최초 오류는 `data === undefined` 라 로컬 계산에 닿지 않는다. `queryFn` 이 null 을 돌려주는 경우(`api.ts:173-176`, `normalizeSeries` 가 구형 모양에서 null)만 성공 응답으로 센다.
- 렌더 게이트 `ResultAnalysisView.tsx:121 {series ? <>…표/차트/Pareto…</> : compareQ.isPending ? <Skeleton/> : null}`. 설정별 결과 표, Baseline 대비 변화, 지표별 비교, 반복 분포, Pareto, 설정 차이가 모두 이 블록 안에 있다.
- 동적 확인(반례 A): 응답 지연 중 렌더된 heading 은 `["AI Research Lab","BM25 k1 스윕","비교 전에 확인하세요","실패·취소한 실행","최선 결과 채택"]` 뿐이다. 설정별 결과·Baseline 대비 변화·지표별 비교·반복 분포·Pareto heading, `<table>`, `role=img` 차트, `▲/▼ 수치` 가 모두 없었다.
- 반례 D(legacy): 첫 응답 지연 중 로컬 결과 없음 → `{}` 응답 성공 뒤에야 `▲ +25.0% 개선` 표시 → 이후 갱신 실패에도 로컬 결과가 유지되고 안내가 뜬다.

### 2. query key 전환 시 이전 key 데이터 노출 — PASS

- `ResultAnalysisView.tsx:56-60` key = `[...rk(), 'compare-series', expId, baselineId, px, py]`. `ResultAnalysisView.tsx` 의 `compareQ`·`detailQ` 에는 `placeholderData`/`keepPreviousData` 가 없다(`grep -rn "placeholderData\|keepPreviousData" src/pages/research/` 일치 3건은 `MethodLibraryView.tsx:40`, `ExperimentDesignerView.tsx:2,70` 로 모두 다른 화면).
  새 key 는 캐시가 없으면 `data === undefined` 이고 `dataUpdatedAt === 0` 이라 이전 key 데이터와 로컬 결과가 모두 안 보인다.
- 실험 전환: `detailQ` 도 key 에 expId 가 있고 `placeholderData` 가 없다. 반례 E 에서 새 detail 대기 중 이전 실험 heading `BM25 k1 스윕` 이 사라졌다.
- 반례 B(A→B→A, 운영과 같은 `retry:false`, gcTime 기본 5분 QueryClient): B 대기 중 비교 결과 없음 → B 최초 오류에 A 의 `+77.7%` 가 없고 "이전 비교 결과" 안내도 없음 → A 로 복귀해 재조회가 실패하면 A 의 캐시 결과, "이전 비교 결과" 안내, 재시도 버튼이 함께 나온다.
- 작업자 신규 테스트(`research.test.tsx` 의 "baseline key가 바뀌면…", "축과 실험 key가 바뀔 때마다…")도 baseline·축·실험 전환을 모두 다룬다. 단 `mount()` 가 `gcTime:0` 이라 캐시 복귀 경로는 못 본다. 위 반례 B 가 그 공백을 메웠다.

### 3. 오류 격리 — PASS

- 이전 `{detail && series && <>…}` 를 `{detail && <>…}` 로 풀었다(`ResultAnalysisView.tsx:107-118`). 실패·취소 실행, 결과물(`ArtifactList`), 최선 결과 채택(`BestResultPanel`), 분석 스냅샷 갱신 버튼은 비교 응답과 무관하게 렌더된다.
- 반례 B/C 에서 B 최초 오류 상태에 `최선 결과 채택`·`실패·취소한 실행` heading 이 있고 `설정별 결과` 등 비교 결과는 없었다. 작업자 테스트는 결과물 `원본 측정 표` 와 `이 설정 채택` 버튼까지 확인한다.
- 반례 C: 최초 오류 → "비교 데이터 다시 불러오기" 클릭 → 재요청 대기 중 로컬 결과 없음 → 성공하면 서버 결과, 오류 문구 제거.

### 4. cached 결과 표시 시 안내 — PASS

- `ResultAnalysisView.tsx:102-105` `compareQ.dataUpdatedAt > 0` 이면 제목 "이전 비교 결과"와 "아래 비교는 마지막으로 성공한 응답입니다. 최신 결과를 받지 못했습니다." 를 오류 원문과 재시도 버튼 앞뒤로 표시한다.
- 동적 확인: 반례 B(A 복귀), 반례 D(legacy 뒤 갱신 실패), 작업자 테스트 "동일 key 재조회 실패…" 모두 통과.
- 같은 key 를 오류 없이 백그라운드 재조회하는 동안에는 이전 값이 안내 없이 남는다(반례 F). 이는 일반적인 stale-while-revalidate 이고 오류가 아니므로 결함으로 보지 않는다.

### 5. analyze 후 invalidate — PASS (P3 관찰 있음)

- `ResultAnalysisView.tsx:70-76` onSuccess 에서 `KEY` 전체와 `compare-series` prefix 를 invalidate 하고 detail·compare 를 refetch 한다.
- 동적 확인: 작업자 테스트 "분석 mutation 성공 뒤…" 가 `POST /experiments/e1/analyze` 이후 `▲ +42.4%` 갱신을 본다. 내 반례 F 도 두 번째 응답 `+13.3%` 로 갱신됨을 확인했다.
- 이전 보고서가 "미검증"으로 남긴 analyze 갱신 경로가 이제 테스트로 덮인다.

### 6. Pareto unavailable 사유 원문 표시, 서버 delta 유지 — PASS (P3 관찰 있음)

- `ResultAnalysisView.tsx:157-167` 서버 시리즈이면 `pareto_status === 'unavailable'` 에서 `series.pareto_unavailable_reason` 원문을 그대로 `Callout` 에 넣고, 사유가 비었을 때만 기본 문구를 쓴다. `series.ts:28,36` 가 사유를 `pareto_unavailable_reason` 또는 `pareto.unavailable_reason` 에서 옮긴다.
- 동적 확인: 반례 G 에서 임시 사유 문자열 `서버 원문 사유: 측정된 설정이 1개뿐입니다 (임시)` 가 그대로 표시됐고 delta 차트는 서버 값 `▲ +77.7%` 를 썼다. 작업자 테스트는 같은 축 선택 사유 `Pareto 분석은 서로 다른 두 지표를…` 도 본다.

## 직접 실행한 명령

```text
$ node_modules/.bin/vitest run src/pages/research/
 Test Files  1 passed (1)
      Tests  31 passed (31)
$ node_modules/.bin/tsc -b          -> exit 0, 출력 없음
$ node_modules/.bin/vite build      -> precache 93 entries (3619.13 KiB) / files generated dist/sw.js
$ node_modules/.bin/eslint .        -> exit 0, 출력 없음
```

반례 임시 파일 실행(삭제됨):

```text
$ node_modules/.bin/vitest run src/pages/research/zz_counter.temp.test.tsx --reporter=verbose
 ✓ A 지연 응답 중에는 table/chart/delta/Pareto 어느 것도 없다
 ✓ B A→B→A 키 전환 … 이전 비교 결과 안내
 ✓ C 최초 오류 후 재시도 …
 ✓ D legacy …
 ✓ E 실험 전환 …
 ✓ F analyze 1회 뒤 …
 ✓ G Pareto unavailable 사유 원문 + 서버 delta 사용
 Tests  7 passed (7)
```

참고: `vite build` 와 `eslint` 는 `dist/` 를 갱신할 수 있으나 `git status` 는 깨끗했다(`dist` 는 추적 대상이 아님).

## 반례 재현법

임시 파일을 `src/pages/research/` 에 두고 `vitest run <파일>` 로 실행했다. 공통 구성은 아래와 같다.

- `testkit.ts` 의 `setup(handler)` 로 `researchRequest` 를 가짜로 바꾸고, `/compare-series` 경로만 `deferred` promise 나 throw 로 대체한다.
- 기존 `mount()` 는 `gcTime:0` 이라 key 복귀 캐시를 못 보므로, 운영 `main.tsx` 와 같은 `new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } })` 로 직접 렌더했다.
- 서버 응답 fixture 는 `research.test.tsx` 의 `fakeServerCompare`(delta +77.7%, 로컬 계산 +25.0% 와 일부러 다름)를 복사해 썼다.
- A 는 `/compare-series` 를 미해결 promise 로 두고 heading·table·`role=img`·`▲/▼ 수치` 부재를 검사한다. B 는 baseline 을 c2 로 바꿔 reject, 다시 c1 로 바꿔 두 번째 A 요청에서 throw 한다. D 는 `{}` 로 resolve 한 뒤 2번째 요청에서 throw 한다. F 는 analyze 클릭 뒤 두 번째 compare 를 지연시킨다.

## P3 관찰 (판정에 영향 없음, 교정 불요 또는 선택)

1. **설정별 결과 표의 delta 칸은 서버 값이 아니다.** 서버 시리즈가 도착한 뒤에도 표 셀의 `▲ +25.0% 개선` 은 `detail` 로컬 계산(`ResultAnalysisView.tsx` 의 `rowsStat`/`rel`)이다. 내 fixture 에서 서버는 +77.7% 이고 같은 화면 표에 로컬 +25.0% 가 1회 남았다. Baseline 대비 변화 차트는 서버 `delta_bars` 를 쓴다. 이 표는 이번 diff 가 건드리지 않은 기존 동작이고, 실제로 서버와 로컬이 같은 입력이면 값이 일치할 것이다. 다만 "서버 delta 사용" 을 표까지 넓게 해석하면 불일치 가능 지점이라 후속 정리를 권한다. 확인 필요: 실제 backend 값과의 일치.
2. **pending 중에도 `비교 전에 확인하세요`(comparabilityCaveats)와 `자동 수치 요약`(detail.latest_analysis)이 먼저 표시된다.** 전자는 `detail` 로 로컬 계산한 비교 가능성 경고이고 후자는 서버 detail 값이다. 둘 다 table/chart/delta/Pareto 가 아니므로 이번 기준에는 맞고, 경고가 먼저 보이는 것이 오히려 안전하다.
3. **analyze 1회에 compare 요청이 3번 추가로 나간다.** `invalidateQueries(KEY)`, `invalidateQueries(compare-series prefix)`, `compareQ.refetch()` 가 겹친다(반례 F 에서 전체 4회, 초기 1회 포함). 결과 값에는 영향이 없고 낭비만 있다. 마지막 응답이 최종 상태가 되는지는 반례 F 에서 확인했다.

## 미검증 범위

- 브라우저 실제 렌더·네트워크 지연·모바일 레이아웃은 보지 않았다. jsdom + 가짜 `researchRequest` 의 component 검증이다.
- 실제 backend HTTP 응답으로 끝까지 잇는 경로는 이번에 실행하지 않았다. backend payload 는 이전 보고서(`compare-series-review-c3fbf36.md`)의 ASGI 검증에 의존하며, 현재 백엔드 HEAD 에서 재검증하지 않았다.
- 요청 중복 취소·경쟁 상태(응답 순서 뒤바뀜)는 TanStack Query 기본 동작에 맡겼고 별도 시험하지 않았다.
- 접근성(스크린리더)·키보드 순서는 범위 밖이다.
