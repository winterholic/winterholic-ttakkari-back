# Context Gateway v2 공개 Reader 작업 기록

- 날짜: 2026-11-19 (Asia/Seoul)
- 위치: `context_gateway/web/`만 수정
- 기준: [v2 계약](../../../docs/context-sharing-v2-contract.md), [core 작업 기록](../../../docs/work-history/2026-11-19-context-gateway-v2-core.md)
- 소유 경계: core `src/tests`, Private, 관리 front는 수정하지 않음

## 구현

- `/s#token=...`은 module 진입 때와 같은 문서 안의 `hashchange` 때 token을 메모리로 읽고 `history.replaceState`로 주소에서 즉시 제거한다. `/s`는 기존 API Key 로그인 UI를 렌더하지 않는다. 같은 문서에서 같은 fragment를 다시 여는 경우에도 visit sequence를 증가시켜 매번 exchange와 context를 요청한다. cookie만 있는 재방문은 context를 직접 다시 읽는다.
- `src/share/ShareReader.tsx`에 교환·context·export·logout만 허용하는 전용 fetch allowlist를 추가했다. same-origin relative URL, `credentials: same-origin`, `no-store`, `no-referrer`, `redirect: error`를 고정한다. 브라우저 cookie를 사용하며 token/session을 storage, query, 일반 Gateway client/cache, error text에 두지 않는다.
- context의 project, link, snapshot, document 모양을 계약 기반으로 확인하고, snapshot/document 출처와 생성·검증 시각, 게시시각, 공유 만료·session 만료를 표시한다. 문서 렌더는 `SafeMarkdown`과 `ProvenanceBlock`을 재사용한다.
- 전체 맥락을 Markdown으로 복사한다. `export` scope가 있을 때만 Markdown 버튼을 보이고, share export fetch 응답을 Blob으로 저장한다. 서버 403/404도 일반 오류로 안전하게 처리한다.
- logout은 먼저 Reader 화면·메모리 token을 비운다. 서버 logout 2xx를 받은 경우에만 서버 세션 종료를 안내한다. 실패하면 화면 비움과 서버 세션 미종료/쿠키 잔존 가능성을 구분해 안내하고 서버 logout 재시도 버튼을 제공한다. 로그아웃 상태에서는 token을 재교환하지 않고 원 링크 재열기를 안내한다. 401/503/network 응답은 원문을 표시하지 않는다.
- 기존 `/` 키 교환 Reader를 유지했다. 연결 안내에서 `reusable_api_v2` raw Bearer REST/MCP 사용과 기존 `single_use_v1` exchange 흐름을 구분하고, `share_link_v2` 원문은 REST/MCP Bearer가 아님을 명시했다.
- `scripts/serve-web.mjs`는 테스트 전용 합성 share API를 제공한다: 반복 교환, read/export scope, 쿠키 flags, context, export 403, logout, revoked 401, 일회성 503 뒤 재시도 성공. core 구현은 수정하지 않았다.

## TDD와 자동 검증

- RED: `npm test -- --run src/share/ShareReader.test.tsx` → 신규 `ShareReader` 모듈이 없어 import resolution 실패.
- RED 재확인: exchange 경로를 allowlist에서 임시 제거하고 같은 테스트를 실행 → `10 tests | 6 failed`; 인증 context/export 및 same-tab 재교환 assertion 실패. allowlist를 복구한 뒤 같은 명령 → `10 passed`.
- GREEN: 최종 `npm test` → `Test Files 9 passed | 2 skipped (11)`, `Tests 104 passed | 5 skipped (109)`. 기존 live 전용 5건은 Gateway live 환경 변수가 없어 기존 설정대로 skip.
- `npm run typecheck` → exit 0.
- `npm run build` → TypeScript 및 Vite 성공, JS 430.07 kB / CSS 26.02 kB.
- `python3 /Users/winterholic/.agents/skills/frontend-design/scripts/design-lint.py src/main.tsx src/share src/styles/app.css src/guide.ts src/ui/ConnectGuide.tsx index.html` → `검사 7개 파일 — 검출 없음.`
- `node --check scripts/serve-web.mjs` → exit 0. `git diff --check` → exit 0.
- `src/share/ShareReader.test.tsx` 10건: fragment 정리와 같은 탭 재열기, same-origin/cookie fetch 옵션, key/session storage 및 error 누출 부재, SafeMarkdown의 외부 이미지·HTML 차단, export scope/403, read-only 버튼 숨김, 쿠키 재방문·logout token clear, revoked 401, malformed fragment, 네트워크 재시도.

## 독립 재검수 후 교정 (2026-11-19)

- 이 기록의 앞선 브라우저 검수는 링크 재방문과 서버 logout 성공 응답을 관찰한 기록이다. 같은 Reader 문서에서 동일 fragment를 다시 설정하는 React 상태 경로와 logout 서버 요청 실패 처리는 입증하지 못했다. 독립 자동 검수에서 교환 호출이 두 번째 방문에 생략되고 logout 네트워크 실패가 종료 성공 문구로 표시되는 반례가 확인됐다. 앞선 증거를 이 두 경로의 보장 근거로 확대하지 않는다.
- 작업 카드가 지정한 `2026-11-19-context-sharing-v2-public-reader.md` 파일은 이 작업 트리에 없었다. 실제 기존 web 기록인 이 파일만 갱신했다. 별도 `docs/work-history/2026-11-19-context-sharing-v2-public-verification.md`는 범위 밖이라 수정하지 않았다.
- 같은 링크 재개방은 token 값과 별개인 visit sequence로 effect를 다시 실행하고, 새 링크 진입 때 이전 Reader 내용을 즉시 비운다. 이전 exchange가 새 링크 exchange보다 늦게 완료되면 현재 token을 다시 교환하고, 이미 logout된 상태라면 logout을 다시 요청해 뒤늦은 Set-Cookie가 세션을 되살리지 않게 한다. 이전 context 응답은 현재 요청 세대가 아닐 때 화면에 적용하지 않는다.
- export는 응답 Blob까지 받은 뒤 요청 세대를 확인한다. 링크 전환 또는 logout 뒤 완료된 응답은 `save`에 전달하지 않고, 늦은 오류도 현재 화면을 덮지 않는다.
- logout 실패는 로컬 화면 비움만 성공한 상태로 표시한다. 서버 세션 종료는 2xx 이후에만 표시하며, 실패 화면의 서버 쿠키 잔존 가능성과 `서버 로그아웃 다시 시도` 경로를 자동 회귀로 확인했다.
- 기존 독립 반례의 요구 assertion은 삭제하거나 완화하지 않았다. 서버 logout 성공 문구만 새 계약에 맞게 갱신했고, 실패 시 본문 비움·쿠키 구분·재시도 assertion을 추가했다.

### 재현과 회귀 증거

```text
$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx --reporter=dot --silent   # 교정 전
Test Files 1 failed | 1 passed (2)
Tests 2 failed | 11 passed (13)

$ npm test -- --run src/share/ShareReader.independent.test.tsx -t "logout 뒤 완료된 지연 exchange" --reporter=dot --silent   # 쿠키 정렬 보상 임시 제거 (RED)
FAIL: expected logout calls 2, received 1

$ npm test -- --run src/share/ShareReader.independent.test.tsx -t "logout 뒤 완료된 지연 exchange" --reporter=dot --silent   # 원 코드 복구 (GREEN)
Test Files 1 passed (1)
Tests 1 passed | 6 skipped (7)

$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx --reporter=dot --silent
Test Files 2 passed (2)
Tests 17 passed (17)

$ npm test
Test Files 10 passed | 2 skipped (12)
Tests 111 passed | 5 skipped (116)

$ npm run typecheck
tsc -p tsconfig.json --noEmit  # exit 0

$ npm run build
✓ 290 modules transformed.
✓ built in 107ms; JS 431.46 kB / CSS 26.02 kB

$ git diff --check
exit 0 (tracked diff)

$ git diff --no-index --check /dev/null context_gateway/web/src/share/ShareReader.tsx
$ git diff --no-index --check /dev/null context_gateway/web/src/share/ShareReader.test.tsx
$ git diff --no-index --check /dev/null context_gateway/web/src/share/ShareReader.independent.test.tsx
$ git diff --no-index --check /dev/null context_gateway/web/work-history/2026-11-19-context-gateway-v2-web.md
(각 명령은 출력 없음, exit 1은 신규 파일 diff 상태)
```

### 변경 식별자

- 검수 기준 HEAD (커밋하지 않음): `978cb5ace5115743b32f375ce065120e8d068d82`. 커밋과 push는 통합자가 수행한다.
- `src/share/ShareReader.tsx` SHA-256: `358ea48e898ccb2845f8edebb88589d441fc88087bbcfeaa127976ddcaa927a2`
- `src/share/ShareReader.test.tsx` SHA-256: `54c718c48e33ed8982d8b4add3ecf87672322d8283d169b54fa12a1d94b738bb`
- `src/share/ShareReader.independent.test.tsx` SHA-256: `a078706c898cf5ec63e1a6e6bfb9207f57b0334274094b608c5387c55b39047b`

## 실제 브라우저 + 합성 API

- 같은 origin에서 production `dist`와 `scripts/serve-web.mjs` 합성 API를 실행했다. 브라우저 URL은 `/s#token=synthetic-share-…` 형식으로 열었고 모든 입력은 합성 key다. 화면 완료 후 최종 주소는 `http://127.0.0.1:43173/s`였으며 fragment는 제거됐다.
- 390×844: 승인 프로젝트·요약·문서·출처/미검증·만료 및 액션을 표시했다. Accessibility snapshot의 본문 폭은 390px이고 콘텐츠 박스는 x=16..374에 들어왔다. 외부 이미지는 텍스트 대체 표시이며 `<script>`·`onerror`는 실행 가능한 요소가 되지 않았다.
- 1280×800: 문서 탐색과 본문 2열, provenance, 전체 복사와 scope export를 확인했다.
- 당시 브라우저에서는 같은 탭의 원 링크 재방문 뒤 exchange 204/context 200을 관찰했다. 이는 동일하게 열린 Reader 문서의 hashchange에 같은 token 값을 다시 넣는 자동 반례가 아니며, 독립 회귀에서 그 경로가 누락됨을 확인했다. export 가능 합성 링크에서 Markdown Blob 다운로드(`shared-context.md`), read-only 링크의 버튼 숨김, revoked 401 안내, 503 뒤 재시도 성공도 관찰했다. logout은 성공 응답 경로를 확인한 결과이며 실패 안내는 검증하지 않았다.
- 성공 흐름의 `browser_network_requests(static=false)` 출력은 share endpoint만 포함했다: `POST /api/v1/share/exchange => 204`, `GET /api/v1/share/context => 200`, export를 누른 때만 `GET /api/v1/share/export?format=markdown => 200`. 외부 이미지 요청과 일반 project/snapshot/MCP 요청은 없었다.
- 최신 built UI의 전체 화면 캡처:
  - 390px: `/Volumes/Backup/ttakkari-data/outbox/f49f6f7d-a225-4c9f-bca7-7e22581b80e2/browser/page-2026-11-19T09-58-28-641Z.png`
  - 1280px: `/Volumes/Backup/ttakkari-data/outbox/f49f6f7d-a225-4c9f-bca7-7e22581b80e2/browser/page-2026-11-19T09-58-21-708Z.png`
- 검증 서버는 종료했고 configured browser도 닫았다. 초기 포트 4173 bind는 `EADDRINUSE`로 실패해, 새 포트 43173에서 이 Run이 띄운 서버를 종료했다. 이미 사용 중이던 4173 프로세스는 건드리지 않았다.

## 범위와 미실행

- 합성 UI·합성 API만 검증했다. 실제 Gateway HTTP/API/DB, Private 발급 흐름, production cookie 설정, 운영 데이터, 공개 활성화, push, 배포는 미실행이다. 배포 revision도 검증하지 않았다.
- 브라우저의 401/503 시나리오에서 DevTools는 의도된 HTTP 상태 오류를 기록한다. 앱 코드가 응답 detail/token을 console에 남기지는 않는다.
- full tab-order audit, Safari/Firefox, 실운영 TLS cookie, Gateway 전체 integration은 미검증이다. 이 기록은 core의 40-test 보고나 관리 front 검증을 대신하지 않는다.

## 공개 Reader 인증 완료 순서 재교정 (2026-11-19)

### 코드 변경 전 호출·상태 추적과 근본 원인

- `hashchange` handler는 fragment를 즉시 소비·제거하고 `desiredToken`을 바꾼 뒤 요청 epoch와 visit를 증가시키며 기존 bundle을 비운다. 같은 token 재방문도 visit 변경으로 load effect를 다시 실행한다.
- load effect는 token이 있으면 exchange를 기다린 후 context를 요청한다. 같은 Reader에서 서로 다른 load effect의 exchange 요청은 병렬이므로, 더 오래된 exchange 응답의 쿠키 변경이 최신 exchange/context 뒤에 도착할 수 있다. stale effect는 자기 context 응답은 버리지만, 늦은 exchange 뒤 `restoreSessionIntent`를 fire-and-forget으로 시작했다.
- `restoreSessionIntent`는 현재 token 또는 stopped 의도로 exchange/logout을 한 번 요청한다. 실패 `catch`는 반환해 호출자에게 성공/실패를 전달하지 않고 intent 재확인도 수행하지 않았다. 호출자가 완료를 기다리지 않아 최신 UI의 성공·실패 상태와 서버 쿠키 복구 결과가 분리됐다.
- logout handler는 화면과 token을 먼저 비우고 epoch를 증가시킨 뒤 서버 응답을 기다린다. 응답 적용 시 logout을 시작한 epoch/stopped 상태를 확인하지 않아, 새 링크 방문 후 돌아온 늦은 logout이 쿠키를 지우고 새 화면을 종료 성공 화면으로 덮을 수 있었다.
- 세부 완료 순서와 cookie 변경은 운영 요청이 아닌 `ShareReader.final-acceptance.test.tsx`의 합성 fetcher fixture에서 확인했다. fixture가 응답 완료 시점을 제어해 exchange Set-Cookie 및 logout 삭제 순서를 모사한다. 운영 장애 관측이나 실제 브라우저 동시 쿠키 동작으로 확대하지 않는다.
- 근본 원인: 인증 쿠키를 바꾸는 비동기 응답마다 현재 방문 세대와 최신 의도를 확인하지 않았고, 복구 요청 실패를 상태 전이로 전달하지 않아 UI가 확인되지 않은 쿠키 상태를 성공으로 표현할 수 있었다.

### 교정

- exchange/logout 완료가 이미 낡은 요청 세대라면 현재 token/stopped 의도를 최대 3회까지 다시 맞춘다. 실패하거나 제한 안에 안정된 의도를 얻지 못하면 조용히 종료하지 않고 fail-closed 오류와 사용자 재시도를 표시한다.
- stale logout 응답 뒤에는 새 화면을 즉시 비우고 최신 token 교환과 context를 다시 완료한 뒤 보여준다. logout 복구 실패면 본문을 비우고 쿠키가 남았을 수 있다는 안내와 logout 재시도를 제공한다.
- stale exchange 복구 실패도 현재 context와 기존 cookie가 일치한다고 주장하지 않는다. 기존 context를 비운 오류 화면에서 현재 링크 재시도를 제공한다. context 응답 적용과 export 저장은 요청 epoch를 계속 확인한다.
- 기존 `ShareReader.test.tsx`, `ShareReader.independent.test.tsx`, 독립 소유 `ShareReader.final-acceptance.test.tsx`는 수정하지 않았다. 신규 `ShareReader.race-recovery.test.tsx`에 지연 logout 후 링크 복구, 늦은 구 exchange 뒤 현재 exchange 복구 실패/수동 재시도, 일반 exchange 실패/재시도를 추가했다.
- 기존 독립 테스트는 context 호출 2회를 기대하지만 새 교정은 재인증 성공 뒤 context를 한 번 더 읽어 호출이 3회가 된다. 이는 화면에 기존 context를 재사용하지 않고 재인증된 cookie로 다시 읽어 expiry/provenance를 정합하게 하는 구현 선택이다. 보안 assertion을 낮추지 않았다. 호출 횟수 차이와 근거를 기획 검토 항목으로 남긴다.

### 검증

```text
$ npm test -- --run src/share/ShareReader.race-recovery.test.tsx --reporter=dot --silent
Test Files 1 passed (1)
Tests 3 passed (3)

$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx src/share/ShareReader.race-recovery.test.tsx --reporter=dot --silent
Test Files 1 failed | 3 passed (4)
Tests 1 failed | 25 passed (26)
```

- 남은 단일 실패는 `ShareReader.independent.test.tsx`의 context 호출 횟수 2 기대/실제 3이다. 구 exchange 응답 뒤 현재 링크 재교환 성공 및 최신 화면 표시는 통과한다. 독립 검수 파일을 소유 경계에 따라 고치지 않았다.
- 기준 재현: 집중 세 파일 실행은 `3 failed | 17 passed (20)`였다. 신규 회귀는 변경 전에 2개 실패/1개 통과(지연 logout 중 낡은 화면 유지, 보상 exchange 실패 미표시)한 뒤 수정 후 `3 passed`다.
- 이 검증들은 jsdom의 합성 fetcher/cookie 모델이며 운영 HTTP, 실제 TLS 쿠키, 브라우저 viewport를 재검증한 것이 아니다.

최종 실행 (2026-11-19):

```text
$ npm test -- --run src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files 1 passed (1)
Tests 6 passed (6)

$ npm test
Test Files 1 failed | 11 passed | 2 skipped (14)
Tests 1 failed | 119 passed | 5 skipped (125)

$ npm run typecheck
tsc -p tsconfig.json --noEmit  # exit 0

$ npm run build
✓ 290 modules transformed.
✓ built in 115ms; JS 432.34 kB / CSS 26.02 kB

$ git diff --check
exit 0
```

- final acceptance는 소유 검수자가 갱신한 최신 파일을 읽은 뒤 그대로 실행했다. 파일은 수정하지 않았다.
- 전체 suite 유일 실패는 위 context 요청 호출 횟수 assertion이다. 전체 보안/상태 검사 119건과 2개 suite의 5 skip은 해당 결과다.

## 최종 제품 상태 확인 (연속 Run, 2026-11-19)

- 기존 독립 테스트 파일은 계속 수정하지 않았다. SHA-256은 기준 기록과 동일한 `a078706c898cf5ec63e1a6e6bfb9207f57b0334274094b608c5387c55b39047b`이다.
- 현재 Reader SHA-256: `59d34ac583c6d4f552e9e7f8568c98c7b3fd52e235ceb3cea8c79786850c0e79`.
- 신규 race 회귀 SHA-256: `1cd43dbe36dc7413a145098180de095b251413c0ec7de7e4d0a7867175d39bfb`.
- 검수 기준 Git HEAD: `978cb5ace5115743b32f375ce065120e8d068d82` (제품 변경은 이 commit 위의 작업 트리에 있으며 commit/push 안 함).

```text
$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files 1 failed | 2 passed (3)
Tests 1 failed | 22 passed (23)
FAIL src/share/ShareReader.independent.test.tsx:135 — expected context calls 2, received 3

$ npm test -- --run src/share/ShareReader.race-recovery.test.tsx --reporter=dot --silent
Test Files 1 passed (1)
Tests 3 passed (3)

$ npm run typecheck
tsc -p tsconfig.json --noEmit  # exit 0

$ npm run build
✓ 290 modules transformed.
✓ built in 89ms; JS 432.34 kB / CSS 26.02 kB
```

- 잔여 실패 원인/판정: 늦은 구 exchange 뒤 현재 token 재교환이 끝난 다음 context를 다시 요청한다. 따라서 기존의 고정 횟수 2와 다르게 3회가 된다. 재인증 이후 현재 쿠키에 권한이 맞는 context를 다시 얻는 상태 복구를 유지했고, 횟수 기대와 본래 보안 의도는 독립 검수자가 별도로 상태 기반 재검토한다.

## 독립 검수 갱신 후 재실행

- 검수자가 `ShareReader.independent.test.tsx`를 상태/cookie 기준으로 갱신한 것을 확인했다. 최신 SHA-256: `447d652c6d4b9e3b1bf26c57fcc90bc4a4fc169922e0f5bf53b5ccf2e5d08bf0`. 본 작업자는 이 파일을 수정하지 않았다.
- 갱신 뒤 집중 세 파일은 기존 독립 검사의 2회/3회 불일치 없이 진행됐고, 유일 실패가 최신 final acceptance의 166행으로 이동했다. 해당 fixture는 복구 exchange를 reject하도록 설정하지만, 테스트는 그 뒤에도 `contextCookies`에 세 번째 `session-new` 요청이 있어야 한다고 기대한다. 실제 구현은 복구 exchange 실패를 받고 context 요청을 중단해 본문을 숨기고 재시도를 제공한다. 요청 완료 상태 기준으로는 fail-closed 동작이며, 세 번째 context 기대는 실패 fixture와 맞지 않는다.
- 소유 경계에 따라 독립 및 final acceptance 파일 모두 수정하지 않고 실패 위치·fixture 조건을 보고했다.

```text
$ npm test -- --run src/share/ShareReader.test.tsx src/share/ShareReader.independent.test.tsx src/share/ShareReader.final-acceptance.test.tsx --reporter=dot --silent
Test Files 1 failed | 2 passed (3)
Tests 1 failed | 22 passed (23)
FAIL ShareReader.final-acceptance.test.tsx:166 — expected contextCookies 3 entries, received 2
```
