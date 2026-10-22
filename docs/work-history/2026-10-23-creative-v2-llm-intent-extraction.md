# Creative Studio v2 — Run-backed intent review (2026-10-23)

범위: 첫 수직 슬라이스 이후 필수 누락을 해소하는 다음 기능 단위. main 병합·배포·영상 렌더는 하지 않았다. 작업을 시작할 때 first-slice 기준 SHA는 backend `99a9e084a4716bce21adbc310d0e25f9694872b5`, frontend `02dd278e39b6cea5851283e00a3166de48bf6fe1`로 고정되어 독립 검수 대기 상태였다. 후속 검수 결과는 아래의 추가 증거 기록에 반영한다. 모든 후속 변경은 해당 브랜치 위의 별도 커밋이다.

## 구현

- 이전 구현의 직접 desk-Qwen 호출을 제거하고 기존 Ttakkari Run lifecycle로 변경했다. `/api/creative/v2/intent/runs`는 기존 Qwen local worker를 personal worker session으로 큐잉하고, 기존 activation/readiness·model allowlist·Run 저장·취소·resume 경로를 사용한다. 결과는 `/intent/runs/{id}`에서 저장된 Run 결과를 파싱한다.
- worker task type `creative_intent`는 MCP 서버 없이 실행하며 Run manager에서 쓰기 경로를 해당 Run의 임시 outbox로 제한한다. 프롬프트는 브리프를 JSON 데이터로 구분하고 지시문·도구 요청으로 따르지 않게 한다.
- 모델 출력은 strict JSON IntentSpec으로 파싱한다. 모델이 주장한 `user_explicit`, 필수(hard), essential 값은 각각 `llm_inferred`, soft, false로 강제한다. 선택·제약·전달 항목의 인용은 브리프와 대조하고, 누락·환각된 인용 및 schema mismatch는 초안으로 내보내지 않는다. LLM 출처는 질문이 비어 있어도 검토가 필요하다.
- UI는 기존 단순 확인 checkbox를 구조화 편집기로 교체했다. 카탈로그 선택, 선호/필수, 근거, 제약 유형·내용, 전달 항목·중요도·필수 여부, 미결 질문, 음악 길이를 편집한다. 직접 구조화 입력도 가능하다. 검증 후 사용자가 확인한 항목만 `user_structured`로 계획 생성 요청에 포함된다. 길이·화면 비율은 기존 선택 UI에서 적용하고 기존 계획 편집·재계산·승인 흐름을 쓴다.
- Run 시작·재개 요청 키를 URL에 보존해 응답이 유실된 경우 중복 Run 대신 동일 키로 기존 요청을 확인한다. 상태 조회 오류, 취소, 실패, 취소·실패 후 같은 session 재개 및 schema 불일치 표시를 연결했다.
- Style DNA 귀속 정정은 별도 docs-only commit `2073ec26a85f620f7269003caedea8248bad477f`에 남겼다: 사용자 원문은 15개 필드이고 `version` 추가는 구현 설계 선택이다.

## 검증과 실행 증거

- Backend command: `PYTHONPATH=src /Users/winterholic/development/projects/winterholic-ttakkari/back/.venv/bin/python -m pytest -q tests/test_creative_v2_api.py tests/test_creative_v2_extraction.py tests/test_creative_v2_run_extraction.py tests/test_route_inventory.py unit_tests/test_creative_v2_duration.py unit_tests/test_creative_v2_matching.py unit_tests/test_creative_v2_plans.py unit_tests/test_creative_v2_registry.py unit_tests/test_creative_v2_run_scope.py unit_tests/test_creative_v2_style_dna.py` → `118 passed in 3.77s`. Raw output: `outbox/logs/backend-pytest-intent-review.log`.
- Backend Ruff command covered changed API, extraction, intent, run service, manager, route and tests → `All checks passed!`; `git diff --check` 통과.
- Frontend `pnpm test -- --run` → `137 passed | 2 skipped` test files; `1305 passed | 10 skipped` tests. Raw output: `outbox/logs/frontend-vitest-intent-review.log`.
- Frontend `pnpm exec tsc --noEmit`, `pnpm lint` (ESLint + UI lint; 기준선 138, 신규 위반 0), and `pnpm build` 통과. Vite는 기존 500 kB 이상 chunk 안내를 출력했다.
- Backend integration used the existing Run service with a temporary fake Qwen worker. It verifies queue → persisted Run → parsed review-only IntentSpec, disabled-engine failure, idempotent retry and personal worker task tagging. This is Run lifecycle integration evidence, **not** a real model response.
- Configured Ttakkari browser opened local Vite successfully, but the isolated browser profile redirected `/creative/plan` to `/login?next=%2Fchat`; refresh returned 401 and service worker loaded HTML with an unsupported MIME type. Authenticated feature page could not be rendered there. Browser was closed; no shell browser fallback was attempted.

## 미검증·미완료

- 실제 설치 모델 응답은 실행하지 않았다. live backend `/api/version`은 `75a12d690ad2458a56ea10675588539afb82791f`로 이 후속 라우트가 없는 revision이다. Qwen status lookup은 기존에 확인한 대로 한 경로는 404, worker 상태는 인증 없이 401이었으므로 운영 activation과 readiness는 미확인이다. worker binary 존재나 host llama-server 관측은 이 intent 경로의 실제 응답을 증명하지 않는다.
- 실제 MCP tool 실행 0회, 실제 model conversation resume 0회, 모델 usage metering은 미검증이다. 테스트 fake worker의 결과를 실제 모델 결과로 세지 않는다.
- first-slice exact SHA는 이후 독립 검수에서 FAIL 보고를 받았다. 추가 수정 후보의 결과와 혼동하지 않도록 고정 SHA 판정은 계속 별도로 보존한다.
- WBS 8.3/8.6은 아직 done 처리하지 않았다. 실제 Qwen 응답·UI 브라우저 검증 및 잔여 범위가 남아 있다. 전문 레시피 실행 가능성을 새로 주장하지 않았고, 실제 영상 생성은 범위 밖이다.

## Review branch commits

- Backend implementation: `22486d2efb2bc53bad107f3cf31663b29c0e2840` (`feat(creative): run-backed intent review and editing gate`).
- Backend feature history: `d7b594e` (`docs(work-history): record Run-backed creative intent review`).
- Frontend implementation: `efbf1520e96d6b0da948f9f9bd0215fb71e08b90` (`feat(creative): add reviewed intent editing and run recovery UI`).
- These commits were pushed to `feat/creative-multidimensional-v2` and `feat/creative-intent-duration`. The separate Style DNA correction commit `2073ec26a85f620f7269003caedea8248bad477f` was pushed before them.

## Follow-up intent hardening (2026-10-23)

범위는 Run 기반 의도 초안의 제약 승격, 검토 UI의 실제 요청 값, 뒤늦은 응답 보호다. 고정 검수 target인 backend `99a9e084a4716bce21adbc310d0e25f9694872b5`와 frontend `02dd278e39b6cea5851283e00a3166de48bf6fe1`은 변경하지 않았다.

- Backend follow-up: `acdb3565de2eb481ab065d67b27dd8decaab3137` (`fix(creative): ground inferred intent constraints`). LLM이 `exact`를 내도 브리프에 같은 시간과 명시적 정확 표현이 함께 없으면 `preferred`로 낮춘다. `platform_cap`은 알려진 YouTube Shorts 항목, 브리프의 플랫폼 표기와 상한 표현이 모두 있을 때만 허용하고 출처·확인일은 카탈로그 값으로 고정한다. 주 역할(primary)만으로 능력 우선순위가 `must`가 되지 않으며 `strength=hard`만 `must`가 된다.
- Frontend follow-up: `e2895691d400544efde4cf9999aa508f07a54b99` (`fix(creative): preserve reviewed intent on create`). AI가 낸 희망 시간·화면비를 사용자가 직접 바꾸지 않았으면 생성 API까지 유지한다. 사용자가 명시적으로 바꾸면 그 값이 적용된다. 검토 버튼은 선택·제약의 기존 출처를 일괄 `user_explicit`으로 승격하지 않는다. 브리프/의도 revision이 달라진 뒤 start·resume·validate 응답이 도착하면 현재 초안에 적용하지 않고 늦게 접수된 Run은 취소한다.

### 후속 검증

- Backend: `tests/test_creative_v2_extraction.py tests/test_creative_v2_run_extraction.py tests/test_creative_v2_api.py tests/test_qwen_local_run.py unit_tests/test_creative_v2_run_scope.py unit_tests/test_creative_v2_matching.py` → `51 passed in 7.22s`. Ruff 및 `git diff --check` 통과.
- Frontend: `pnpm exec vitest run` → `137 passed | 2 skipped` files, `1307 passed | 10 skipped` tests. `pnpm lint` 통과(UI lint 기준선 138건 유지). `pnpm build` 성공; Vite가 기존 500 kB 초과 chunk 안내를 냈다. 로그는 Run outbox `creative-v2-logs/`에 보존했다.
- TDD 재현: 희망 3분·9:16이 자동·16:9로 덮이는 요청, 편집 뒤 이전 intent Run이 현재 브리프에 붙는 요청, LLM primary가 능력을 `must`로 올리는 요청, 근거 없는 `exact`/`platform_cap` 요청을 먼저 실패시킨 뒤 고쳤다.

### 실제 managed Qwen 응답 smoke: 미실행

- 2026-10-23 KST 00:39경 `curl -sS -i --max-time 8 https://ttakkari-api.winterholic.net/api/version`은 HTTP 200, revision `75a12d690ad2458a56ea10675588539afb82791f`, PID `77581`을 반환했다. `/api/health`는 200 `{"ok":true}`였다.
- 로컬 배포 release `/Users/winterholic/.ttakkari/deploy/releases/75a12d690ad2458a56ea10675588539afb82791f`가 존재하고 `.release.json`도 같은 revision을 가리켰다. 그 release 소스의 `src/ttakkari/api/creative.py`에는 현재 branch의 `/api/creative/v2/intent/runs` 경로가 없다(소스 대조 결과이며, 외부 응답 자체로 경로 부재를 단정한 것은 아님). `/api/version` 응답은 프로세스 PID만 노출하고 active process의 절대 runtime path는 외부에서 확인되지 않았다.
- readiness는 확인되지 않았다: `GET /api/ready`는 HTTP 404, `GET /api/runs/account-status`와 `/api/runs/worker-status`는 HTTP 401 Bearer 인증 요구였다. 사용량 endpoint 후보 `/api/usage`는 HTTP 404였다. 따라서 기존 개인 Qwen worker의 activation/readiness, model 응답·토큰량·레이턴시·자원량을 관측하지 않았다. usage lookup은 실제 추론 증거가 아니며, 이번 작업에서 usage lookup도 인증 때문에 미실행이다.
- 실제 모델 호출 0회, 원문 응답/IntentSpec/schema 검증 0건, live intent Run resume 0회, 실제 MCP 실행 0회다. fake worker 회귀는 Run lifecycle만 검증하며 실모델 결과로 세지 않는다. private desk-Qwen API 직접 호출은 사용하지 않았고 로컬·host GPU 서버도 시작하지 않았다. 미실행 원인은 deployed revision이 현재 Run route보다 앞서 있고 인증된 readiness 조회도 401로 차단된 상태다. live backend가 변경 불가하다는 일반 결론은 아니다.

### 브라우저·남은 범위

- Configured Ttakkari browser로 local Vite `http://localhost:5173/creative/plan`을 열었다. 앱이 `/chat`, 이후 `/login?next=%2Fchat`로 보냈고 콘솔에 `http://127.0.0.1:8787/api/auth/refresh` HTTP 401 및 `sw.js` unsupported MIME 오류가 남았다. 페이지 도구는 닫았다. 인증된 제작 폼은 렌더되지 않아 UI 시각 검수는 미검증이며 shell browser fallback은 하지 않았다. 자동 저장된 snapshot/console은 Run outbox `browser/`에 있다.
- 8.3/8.6은 실제 Run 모델 응답과 인증된 UI 검증 전이므로 done 처리하지 않았다. 렌더 8.7 미연결, 실제 Recipe Studio 실행 0회, 실제 영상 0편 상태를 유지한다. main merge/배포는 하지 않았다.
- 설계·요구사항·두 날짜의 진행 기록에서 Style DNA 귀속을 재검색했다. 사용자 원문 15개 콘텐츠 필드와 시스템 `version` 메타데이터의 구분은 이미 반영되어 있고 “사용자 지시가 16개 필드”라는 오귀속은 남아 있지 않다.

## 독립 검수 FAIL 보완 (2026-10-23)

- 범위: baseline backend `99a9e084a4716bce21adbc310d0e25f9694872b5`, frontend `02dd278e39b6cea5851283e00a3166de48bf6fe1` 검수 항목을 현재 격리 브랜치에서 수정. main 병합·배포는 하지 않는다.
- F1: 승인 시 서버가 revision·plan hash·필수 capability snapshot을 저장한다. 조회/렌더 진입용 `assert_renderable`은 저장 계획과 승인 snapshot, 현재 registry 기반 capability 결과를 다시 비교한다. `studio_runtime` + `ready` + must capability executed/creative_runtime 조건만 gate를 열며 lab_manual·ready_unverified·blocked·snapshot 변경은 false다. v2 렌더러 자체는 아직 연결되지 않아 실제 렌더 실행 검증은 없다.
- R19: 요구 정의에서 필수 능력을 명시하고 lipsync/character animation은 선호, dance는 필수나 사례 증거가 아니라고 정정했다. 연결된 음악비디오/보컬 사례를 partial로 낮추고 conditional/unreviewed 판정과 “완성 레시피·품질 통과 증거가 아님”을 catalog·generated matrix에 표시했다.
- Style DNA: 사용자 콘텐츠 15필드와 시스템 `version` 필드를 코드 상수·요구사항·설계 문서에서 분리했다.
- 600초 희망 + 발화가 거의 없는 짧은 정보 3항목은 24초 상한에서 `deviated`가 되는 정책임을 명시했다. 내용 삭제·반복 방지가 이유이며 확장 선택은 사용자 승인 대상이다. 계획 UI가 server reasons를 보여주는 jsdom 회귀 테스트를 추가했다. 인증된 실제 브라우저 UI는 이번에도 미검증이다.
- 검증: backend 계획/API·registry·Style DNA·duration 집중 suite `97 passed`; 변경 Python `ruff check` 통과. frontend `CreativePlanPage.test.tsx` `14 passed`; `pnpm lint` 통과(기존 UI lint 기준선 138); `pnpm build` 성공(기존 500 kB 초과 chunk 안내). 실제 Qwen 모델 응답과 인증 UI 시각 검증은 미실행 상태를 유지한다.
- Backend 구현·문서 커밋 `9a250834415095b6ac99439218345e0e6605033a`를 `feat/creative-multidimensional-v2`에 push했다. Frontend UI 설명 테스트 커밋 `d8257c668f5b78e70a745ba4325832a5295f6bdf`를 `feat/creative-intent-duration`에 push했다. 원격 ref와 로컬 HEAD가 모두 일치하고 두 worktree는 clean이다.
- main 병합과 배포는 하지 않았다. branch push만 수행했다.

## 추가 독립 증거 반영 (2026-10-23)

- 독립 증분 감사 원문 `outbox/0d1b56fc-f376-4aab-a817-7fb64e6f7a1c/2026-10-23-audit-creative-studio-v2-llm-extraction-increment.html`과 별도 jsdom 재현 `CreativePlanPage.review.test.tsx`를 읽었다. baseline `99a9e08/02dd278`의 FAIL은 별도 판정으로 유지했고, G34 통합 검수 worker 및 공유 source는 건드리지 않았다.
- 검수된 `quote`의 브리프 내 존재 여부와 의미적 지지는 서로 다른 사실이다. validator 응답에 `semantic_support_unverified` 경고를 추가해 자동 의미 판정을 주장하지 않는다. LLM 원문은 editor 화면에서 유지하고, `extraction.method=llm`인 채 계획 API/계산에 전달하면 422로 거부한다. 사용자가 draft 내용을 직접 검토하고 확인한 구조화 제출만 기존 계획 경로에 들어간다. duration·platform cap·aspect·music·info fields 전체가 이 확인 경계를 공유한다.
- API 계획 생성 검사는 unreviewed exact/preferred, aspect, music, info item을 함께 넣은 입력으로 추가했다. validator의 misleading quote가 의미 검증으로 승격되지 않는 회귀도 추가했다. 기존 희망 시간 보존 및 필드 편집·검토 후 실제 계획 요청 테스트는 그대로 통과한다.
- frontend는 Run 응답의 의미 검증 한계 warning을 editable draft에 표시한다. A 요청 뒤 brief를 수정해 B 요청을 시작하고 B 응답 후 A 응답이 오는 jsdom 경합을 추가해, 현재 구현이 A를 취소하고 B만 연결하는 것을 검증했다. 9:16/180초 제안과 사용자 실제 override를 나누는 기존 테스트도 포함된다.
- Qwen 모델 smoke: `host_gpu` 경로에서 `qwen3.5-4b-q4_k_m`, 4096 context, parallel 1, 1회 inference를 실행했다. 응답 180 token, prefill 134.3 tok/s, generation 120.6 tok/s. 응답 preview는 `<think>` 시작부에서 token limit로 잘려 JSON/IntentSpec schema 검증은 하지 않았다. host GPU child는 정상 종료(exit 0), swap growth 0 bytes.
- 별도 managed Run/model check: live `/api/version`은 revision `75a12d690ad2458a56ea10675588539afb82791f`; `/api/ready`는 404. `/Users/winterholic/.ttakkari/deploy/releases/75a12d690ad2458a56ea10675588539afb82791f/.release.json`은 같은 revision이다. 해당 release source의 `src/ttakkari/api` 검색에서 `creative/v2`·`intent/runs` route는 발견되지 않았다. active process 절대 runtime path는 확인하지 못했으므로 실제 실행 중 adapter 부재라고 단정하지 않는다. authenticated worker readiness, managed Qwen intent Run 응답·usage receipt·resume도 확인되지 않아 실행하지 않았다. host GPU inference는 managed Run 기능 증거가 아니다.
- UI 브라우저 검증은 configured browser에서 local `/creative/plan` 진입 후 `/chat`→`/login?next=%2Fchat`로 리디렉션되어 인증 화면까지다. 제작 UI 시각 검증은 미실행, browser는 닫았다. 새 Chrome 프로세스를 띄우지 않았다.
- 실행 증거: backend `tests/test_creative_v2_extraction.py tests/test_creative_v2_api.py tests/test_creative_v2_run_extraction.py unit_tests/test_creative_v2_plans.py` → `44 passed in 6.35s`; Ruff `All checks passed!`. Frontend `pnpm test -- --run` → `137 passed | 2 skipped` files, `1309 passed | 10 skipped` tests; `pnpm lint` passed with UI baseline 138; `pnpm build` passed (existing >500 kB chunk warning).
- 기존 baseline 보완 사항은 이 증분에서 재검수 범위 밖이며 유지된다: `lab_manual` render gate의 revision/hash/capability snapshot, R19 partial/conditional 사례, Style DNA 15 user fields + system `version`, 600초 희망에서 24초·deviated가 되는 이유 및 계획 UI 사유 표시는 이전 변경과 테스트에 남아 있다. main merge/deploy 금지.

## 추가 독립 증거 대조 (2026-10-23)

- 사용자가 제공한 `outbox/1319fbb3-85d5-4e79-ad7f-b78b34287b8b/intent-increment-review.md`와 첨부 재현물을 읽었다. 검수 기준 snapshot은 backend `c630c9b0f27dd000f96dc0c936af5f8838a20396`, frontend `efbf1520e96d6b0da948f9f9bd0215fb71e08b90`이다. 해당 snapshot에서 늦은 검증 응답 안전성 회귀 1건이 실패했고, lab_manual 승인 후 gate가 열리는 반례도 기록됐다. 의미와 무관한 인용은 `valid=true`였으나 `review_required=true`가 유지됐다. 이 세 결과는 고정 snapshot에 대한 증거이며 현재 HEAD에 대한 판정과 분리한다.
- 현재 격리 frontend HEAD `64889553d098f8643865db800be92c9bd693f663`에 같은 `CreativePlanPage.late-review.test.tsx`를 추가해 실행했다. 출력 `LATE_VALIDATION_OBSERVED {"evidence":"기술 설명 영상","confirmed":false,"createEnabled":false}` 및 `1 passed`로 현재 응답이 새 근거를 덮지 않고 확인/생성을 열지 않는 것을 확인했다. 기존의 start/resume 경합 회귀와 달리 이 테스트는 검증 요청이 진행 중인 동안 근거 문구를 편집한 경우를 직접 다룬다.
- 현재 격리 backend HEAD `6697e66d66079d44d750cb10116f96fefffcc1f2`에서 같은 `reproduce_backend.py`를 기존 release venv로 실행했다. 결과: `lab_manual_approval`의 `before=false`, `after=false`; 승인 snapshot에 `snapshot` 키가 있다. 현재 코드의 기존 `test_lab_manual_plan_never_opens_render_gate_after_approval` 및 `test_approval_persists_immutable_plan_and_capability_snapshot`도 포함해 API·계획·추출 집중 실행은 `42 passed in 3.10s`였다.
- 같은 backend 재현의 인용 사례는 현재도 `valid=true`, `review_required=true`, `music_seconds=899.0`을 출력했다. 이는 인용의 존재 검사만으로 의미 지지를 보장하지 않는다는 사례다. 시스템은 `semantic_support_unverified` 경고를 유지하고 LLM 출처 intent를 사용자 검토 전 계획 입력에서 거부한다. 따라서 의미 검증기가 무관한 인용을 판별한다고 주장하지 않으며, 이 실행에서 확인된 것은 미확인 표시와 소비 차단이다.
- 프론트 회귀 test를 추가했으며, backend code는 바꾸지 않았다. 대상 baseline의 `FAIL`은 역사적 판정으로 남고, 현재 격리 브랜치에서 위 두 안전성 반례는 통과로 재검증됐다. R19 partial/conditional 사례, Style DNA 사용자 15 필드와 시스템 version 분리, 600초 희망/24초 deviated 이유는 기존 수정과 기록을 유지한다.
- 이 증분 실행에서는 actual managed Qwen 응답, 모델 usage receipt, authenticated readiness, MCP 실행, 실제 conversation resume, 인증된 브라우저 UI를 실행하지 않았다. 기존 work-history의 범위별 미검증 상태를 그대로 유지한다. 실제 렌더러는 미연결이며 실제 영상은 0건이다. main merge/deploy 금지.

## 고정 후보 후속 검수와 현재 수정 후보 구분 (2026-10-23)

- 추가 독립 보고서 `outbox/37ddf92f-7d91-42d6-a35c-2842a0d96e45/followup-review.md` 및 evidence zip을 대조했다. 그 실행은 backend `c37309c7e797d6e93fe5a8e601996fdf116cab22`, frontend `e2895691d400544efde4cf9999aa508f07a54b99`에서 했다. 보고서의 backend 123 tests 및 frontend 14 tests는 이 고정 checkout의 결과다.
- 검수 결과는 부분 수정·병합 보류다. frontend의 늦은 validation 응답 경합은 현재 근거를 유지하고 `confirmed=false`, `createEnabled=false`를 보여 통과했다. 반면 같은 고정 backend에서 `lab_manual` 승인 뒤 `render_gate=true`가 재현되어 FAIL이다. 의미와 무관한 evidence의 validator 값 `valid=true` 및 `music_seconds=899`도 남지만 `review_required=true`가 유지되므로 확정 우회로 확대 해석하지 않는다.
- 이 c373 backend는 현재 격리 backend HEAD의 조상이다. 뒤의 커밋 `9a250834415095b6ac99439218345e0e6605033a`에서 approval revision/hash/capability snapshot과 `studio_runtime` 한정 gate가 구현됐다. 따라서 c373 검수 FAIL은 그 고정 SHA의 유효한 판정이며, 후속 커밋이 그 기록을 소급 변경하지 않는다.
- 현재 후보 backend `62890c5b77dc04d2c6b6ad85cd6eb01cb23f2d1b`, frontend `f4287acc90ace7c232dd1077aaf1c24b703a16f9`는 독립 전체 검수 PASS 판정을 받은 것은 아니다. 현재 checkout에서 동일 재현은 `lab_manual_approval` `before=false/after=false`와 승인 snapshot 존재를 보였다. 독립 후속 검수와 같은 범위의 API·추출·Run·route·duration·matching·plans·registry·run-scope·Style DNA suite는 `132 passed in 4.42s`였다. frontend 계획 및 late-validation tests는 `16 passed`; 관측값 `evidence=기술 설명 영상`, `confirmed=false`, `createEnabled=false`다. 최신 후보의 결과는 고정 c373/e289 판정과 분리한다.
- 인용 의미 추론은 여전히 미보장이다. 현재 재현 결과도 `valid=true`, `review_required=true`, `music_seconds=899.0`이다. 실제 모델·MCP·conversation resume·authenticated readiness/browser·렌더는 이번 재검수에서 수행하지 않았다. 이 범위의 미검증은 유지한다.
- 앞서 쓴 “first-slice 검수 queued”는 당시 상태였다. 그 기준선 backend `99a9e084a4716bce21adbc310d0e25f9694872b5` / frontend `02dd278e39b6cea5851283e00a3166de48bf6fe1`은 이미 FAIL 보고를 받았고, 별도의 증분 target backend `b7855d09e78982dfd8a8b4ba948e62f4ac0b05e1` / frontend `a78dede6ae81696e7c9c29dd4556d62d2abfea1c`도 FAIL 보고를 받았다. 두 판정은 후속 수정 후보의 회귀 통과와 별개의 고정-target 상태로 보존한다.
