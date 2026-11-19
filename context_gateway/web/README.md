# Context Gateway 외부 웹 UI

승인된 프로젝트 맥락을 VPN 없이 **읽기 전용**으로 열람하는 React·TypeScript 화면이다. Context Gateway(FastAPI)가 같은 origin에서 정적 파일로 배포한다. 따까리 Private 앱·기존 front 저장소와 코드를 공유하지 않으며, 디자인 토큰 일부만 복사해 쓴다.

핵심 과업은 문서를 골라 **읽고, 복사하고, 내려받는 것**이다.

## 명령

```bash
cd context_gateway/web
npm ci              # package-lock.json 기준 설치
npm test            # vitest run (jsdom)
npm run typecheck   # tsc --noEmit
npm run build       # typecheck 후 dist/ 생성
npm run dev         # 개발 서버. API는 같은 origin이어야 하므로 아래 프록시 참고
```

`npm run dev`는 Gateway와 origin이 달라 API가 닿지 않는다. 화면만 볼 때는 `vite.config.ts`에 `server.proxy: { "/api": "<gateway>" }`를 임시로 추가한다(커밋하지 않는다). 앱 코드는 상대 경로만 쓴다.

## 동작 요약

1. 기존 `/` Reader는 키 입력 → `POST /api/v1/auth/exchange` → 공개 프로젝트·문서 API를 호출한다. `single_use_v1`은 한 번 교환하고, `reusable_api_v2`는 raw Bearer 또는 반복 교환을 지원한다.
2. `/s#token=SECRET`은 최초 렌더 전에 fragment를 메모리로 옮기고 `history.replaceState`로 지운다. `/s`는 API Key 입력 화면을 렌더하지 않으며 `POST /api/v1/share/exchange` JSON으로 쿠키 세션을 만든다.
3. share Reader는 `/api/v1/share/context`, `/api/v1/share/export?format=markdown`, `/api/v1/share/logout`만 같은 origin으로 요청한다. 쿠키는 브라우저가 붙이고 설정한 경로 allowlist, `credentials: "same-origin"`, `cache: "no-store"`, `referrerPolicy: "no-referrer"`, `redirect: "error"`를 사용한다. raw 링크 토큰·세션 본문을 localStorage, sessionStorage, query, 오류 문구, 로그, 일반 REST Query cache에 저장하지 않는다.
4. 공유 세션은 브라우저 쿠키를 사용해 재방문할 수 있다. 원 링크 재열기는 같은 링크 재교환을 허용한다. 만료·폐기·401은 원 링크 재열기 또는 소유자 문의를 안내한다. 5xx/네트워크 오류에는 비밀 없는 재시도 안내가 나온다. 로그아웃은 공유 세션만 폐기한다.
5. 공개 Reader는 API가 허용한 프로젝트 하나와 승인 snapshot만 표시한다. 이름·요약·문서 목록/본문·snapshot 생성/게시 시각·출처/검증 상태·링크 만료를 보여 준다. 전체 복사는 read scope에서도 가능하고 Markdown 다운로드는 export scope에서만 보인다. 서버 403/404도 처리한다. 일반 REST 프로젝트 목록, 다른 snapshot, MCP로 권한을 넓히지 않는다.
6. 두 Reader의 Markdown은 `SafeMarkdown`을 재사용한다. raw HTML은 요소로 만들지 않고, 외부 이미지는 네트워크 요청 없이 대체 텍스트로, 링크는 `http(s):`·`mailto:`만 새 탭(`noopener noreferrer nofollow`)으로 연다.
7. 기존 Reader의 문서별 `source_refs`·`source_revision`·`generated_at`·`verified_at` 표시도 유지한다. `verified_at`이 null이거나 응답에서 빠지면 "미검증"으로 표시한다. 빈 `source_refs`를 snapshot 값으로 채우지 않는다.

   과거 UI 기록에는 문서 단건 응답에 출처 필드가 없다는 구 계약을 전제로 `mergeProvenance`가 스냅샷 값을 상속한다고 적혀 있었다. 그 fallback은 현재 계약과 맞지 않아 제거했다. 단건 응답과 전체 context 응답 모두 각 문서가 돌려준 provenance만 표시한다.

## 오류 처리

| 상황 | 화면 |
|---|---|
| 교환 401 | "키가 받아들여지지 않았습니다". 서버가 만료·재사용·폐기·오타를 구분하지 않으므로 가능한 이유를 모두 적고 입력을 비운다 |
| 교환 중 네트워크 오류 | 키가 사용됐는지 알 수 없다고 알리고 같은 키로 재시도하게 입력을 남긴다 |
| 데이터 요청 401 | 세션 초기화 후 로그인 화면, "만료" 안내 |
| 404 | 없음과 권한 없음을 구분하지 않는다(서버도 구분하지 않음). 검색·내보내기는 scope 부족 가능성을 안내한다 |
| 429 | `Retry-After` 초를 보여 주고 "다시 시도" 버튼. 자동 재시도·폴링은 하지 않는다 |
| 5xx·409 | 서버 오류 안내와 재시도 |
| 응답 모양 불일치·다른 프로젝트 데이터 혼입 | 화면에 올리지 않고 "표시하지 않았습니다" 안내 |
| 프로젝트 0건·스냅샷 0건 | 빈 상태와 다음 행동 |
| 부분 실패(메타데이터·스냅샷 목록·문서 하나·검색) | 그 영역에만 오류와 재시도, 나머지는 유지 |

## 배포 통합 (core app 담당)

이 디렉터리는 core 코드를 고치지 않는다. 아래는 통합 때 core에서 적용할 방법이다.

**정적 서빙 통합 상태.** Core working tree의 `web_public.py`와 `WebServingApp`이 이번 실 Gateway 확인에 사용됐다. `/`, `/index.html`, `/assets/*`는 빌드 UI를 서빙하고 `/healthz`, `/api/v1/*`, `/mcp*`는 public ASGI 앱으로 전달한다. 이번 검증은 로컬 ASGI wrapper까지다. Docker 이미지 빌드·배포 연결은 확인하지 않았다. 이 core 파일들은 현재 다른 worker의 미커밋 변경이므로 여기서 수정하지 않았다.

```python
from fastapi.staticfiles import StaticFiles

# 라우트 등록 뒤, app.mount("/", mcp_app) 앞
app.mount("/app", StaticFiles(directory=settings.web_dir, html=True), name="web")  # 예: https://<gateway>/app/
```

- 자산 경로는 상대 경로(`base: "./"`)라서 `/`뿐 아니라 `/app/` 같은 하위 경로에 mount해도 동작한다. 하위 경로에 mount하면 `/app`이 `/app/`로 리다이렉트되도록 `html=True`를 쓴다.
- 라우터가 없는 단일 화면이라 SPA fallback은 필요 없다.
- 권장 응답 헤더(정적 응답과 API 모두): `Cache-Control: no-store`(API), `Referrer-Policy: no-referrer`, `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'`. 빌드된 `index.html`에도 같은 CSP가 `<meta>`로 들어가지만 `frame-ancestors`는 meta로 효력이 없어 헤더가 필요하다.

**Docker 빌드.** Node 단계에서 빌드하고 Python 이미지로 `dist`만 복사한다(런타임에 node 불필요).

```dockerfile
FROM node:22-alpine AS web
WORKDIR /web
COPY context_gateway/web/package.json context_gateway/web/package-lock.json ./
RUN npm ci
COPY context_gateway/web/ ./
RUN npm test && npm run build

FROM python:3.13-slim
# ... 기존 core 단계 ...
COPY --from=web /web/dist /app/web-dist
ENV CONTEXT_GATEWAY_WEB_DIR=/app/web-dist
```

core working tree의 `web_public.py`는 `CONTEXT_GATEWAY_WEB_DIR`(기본 `/app/web-dist`)를 참조한다. 이 래퍼의 실제 컨테이너 배포 연결은 아직 확인되지 않았다.

## 연결 안내(curl·MCP)

화면의 "curl·MCP로 쓰려면"은 별도로 발급한 `reusable_api_v2` 키를 raw Bearer로 REST·MCP에 연결하는 예시를 먼저 제공한다. 키는 `read -rs`로 받고 사용 뒤 `unset`한다. 기존 `single_use_v1` 키는 exchange 후 세션 Bearer를 쓰는 예시로 보존했다. `share_link_v2`는 REST·MCP Bearer가 아니며 이 공개 Reader의 cookie 교환만 사용한다. 실제 키·세션 값은 문자열 생성 코드(`src/guide.ts`)에 전달되지 않는다.

## 공개 공유 Reader 구현 경계

- `/s` 라우트는 core의 정적 shell과 v2 share API를 사용한다. API가 발급하는 Secure·HttpOnly·SameSite=Strict 쿠키의 생성/검증은 Gateway 소유다. 웹은 `Secure`를 낮추거나 쿠키를 직접 읽지 않는다.
- share 전용 fetch allowlist는 세 share endpoint뿐이다. 응답은 React 메모리 상태에서만 보관하고 브라우저 일반 Query 캐시를 사용하지 않는다.
- 이 checkout의 `scripts/serve-web.mjs` share API는 브라우저 검증 전용 합성 응답이다. 실제 Gateway 연동·운영 링크 발급·공개 활성화는 이 UI 검증에서 수행하지 않는다.

## 디자인 토큰

`src/styles/tokens.css`는 front 저장소 `src/vendor/ttakkari/tokens.css`(ttakkari 1.3.3, f9d78eb)에서 `app.css`가 쓰는 토큰만 뽑은 생성물이다. 손으로 고치지 않고 아래로 다시 만든다. 외부 폰트·CDN·분석기는 없다(Pretendard·JetBrains Mono는 기기에 설치돼 있으면 쓰고 없으면 시스템 글꼴).

```bash
node scripts/extract-tokens.mjs <front>/src/vendor/ttakkari/tokens.css
```

## v2 공개 Reader 검증 (2026-11-19)

`/s#token=...` 공개 Reader와 합성 API 브라우저 확인을 추가했다. `npm test`는 104 passed / 5 skipped(기존 live 전용), `npm run typecheck`, `npm run build`, 변경 파일 `design-lint.py`가 통과했다. 브라우저에서 390×844·1280×800, fragment 정리·같은 탭 재열기·복사·scope export 다운로드·read-only 버튼 숨김·revoked 401·일시 503 후 재시도·logout을 합성 key/API로 확인했다. 성공 네트워크는 share exchange/context뿐이며, export scope에서만 share export가 호출됐다. 외부 이미지 요청은 없었다.

재현 명령·세부 출력·캡처 경로는 [v2 웹 작업 기록](work-history/2026-11-19-context-gateway-v2-web.md)에 있다. 이는 실제 Gateway/Private 연동이나 운영 공개·TLS cookie 검증이 아니다. 운영 데이터 발급·공개 활성화·push·배포는 실행하지 않았다.

## 검증 상태

실행·통과 (2026-11-16, 이 checkout):

| 검증 | 대상 | 결과 |
|---|---|---|
| `npm test` | 합성 Gateway(`src/test/fakeGateway.ts`) | 93 통과, live 5개 skip |
| 문서 provenance 회귀 | 단건·전체 context의 A/B 표시, null·미지정, snapshot 값 비상속 | `src/App.test.tsx`, `src/api/adapters.test.ts` 통과 |
| `npm run typecheck`, `npm run build` | | 오류 없음 |
| **실제 Gateway 대조** | `apps.py` 실서버(PostgreSQL 17 + uvicorn)에 `GatewayClient` 어댑터 | `src/api/live.test.ts` 4개 통과(앞선 실행 기록) |
| **실제 Gateway + UI 전체 흐름** | 로그인→읽기→스냅샷→검색→ZIP→로그아웃(jsdom) | `src/live.app.test.tsx` 1개 통과(앞선 실행 기록) |
| **실제 Gateway + 빌드 UI** | 새 전용 DB에 control/public 앱, `web_public:app`에서 `web/dist` 서빙, configured browser | 390×844·1280×800, 문서별 provenance, 검색, MD/JSON/ZIP 브라우저 다운로드 완료, 만료·재사용 401, 실제 429 확인(아래 기록) |
| MCP 경로 | 실서버 `POST /mcp` + 세션 Bearer | `initialize`·`tools/list` 200, 도구 4개. `/mcp/mcp`는 404 → `MCP_PATH="/mcp"` 확정 |

이번 control/public 응답을 확인한 범위에서 실제 응답 모양은 다음과 같았다.

- `expires_at`·`published_at`은 `2026-11-16T02:21:24.472494Z`처럼 **마이크로초 + Z**다. V8(Node)에서 `Date.parse`가 읽는다. Safari·Firefox의 마이크로초 파싱은 **미검증**이다.
- 문서 메타에 `extension`(`md`/`txt`/`json`)이 추가돼 있다. 어댑터는 모르는 필드를 무시하므로 그대로 동작한다(저장 파일 이름은 content_type으로 정한다).
- **과거 런타임 관측(당시 구 응답·구 UI):** snapshot 및 전체 context의 `verified_at`은 `null`이었고, 단건 문서 응답에는 `source_refs`, `extension`, `path`, `sha256`가 있었지만 revision·생성·검증 시각은 없었다. 당시 UI가 이 세 값을 snapshot에서 채웠다는 기록은 이전 동작의 증거이며 최신 계약을 설명하지 않는다. 현재 계약은 문서 metadata를 snapshot과 독립적으로 정의하고, 현재 UI는 생략·`null`을 미상으로 표시한다. 문서별 `source_refs`는 당시에도 서로 다른 값으로 관측됐다.
- control publish와 key issuance는 각각 201이었다. projects, project, snapshots, context(JSON), document(JSON), search는 200이었다. export Markdown/JSON/ZIP은 각각 200이며 `text/markdown`, `application/json`, `application/zip`이었다.
- 소비된 key의 재교환은 401이었다. 시험용 session row의 `expires_at`을 전용 DB에서 과거로 설정한 뒤 보호된 search 요청은 실제 401을 반환했고 UI가 토큰과 문서를 비운 뒤 `세션이 만료됐습니다` 화면으로 돌아왔다.
- 실제 429도 관측했다. 동일 test DB를 쓰는 별도 public 프로세스를 `CONTEXT_GATEWAY_RATE_LIMIT=3`으로 띄웠다. projects/project/snapshots가 200인 뒤 document 요청이 429였고 `Retry-After: 60`; 브라우저는 `60초 뒤에 다시 시도하세요`와 재시도 버튼을 표시했다.

서버 시작 시 HEAD는 `f654054f41de750a1679f61afa68cfedc11375a0`였고, `web_public.py`, `web_serving.py` 및 provenance/API 수정은 다른 worker의 working tree에 있었다. 검증 중 core 변경이 `87b87335`, `c429113c`, `bba4990b`, `185ea8bd`로 차례로 커밋됐다. 현재 README 갱신 시 HEAD는 `185ea8bd`; 실행 중인 서버는 시작 때 로드한 working-tree 코드로 계속 동작했다. 따라서 runtime 결과를 단일 SHA나 배포 빌드 결과로 단정하지 않는다. 계약 문서는 nullable `verified_at`과 manifest의 문서별 provenance를 규정하지만 public document 응답의 모든 필드를 열거하지는 않는다. 실제 응답 필드는 위에서 관측한 그대로 기록했다.

**Core 통합 계약 한 줄:** Docker는 `web/dist`를 `/app/web-dist`로 복사하고 `CONTEXT_GATEWAY_WEB_DIR`를 설정해 `context_gateway.web_public:app`을 실행하며, `/healthz`·`/api/v1/*`·`/mcp*`는 기존 public ASGI 앱으로 전달한다. 이 로컬 실행에서 wrapper/UI/API/MCP 경로는 확인했지만 Docker 배포는 미검증이다.

### 실제 Gateway 대조 재현

직전 live 스크립트는 자체 `initdb`와 고정 DB/포트를 가정하므로 이번에는 실행하지 않았다. 이미 준비된 host PostgreSQL의 전용 DB에 실제 앱을 붙이는 방식은 다음과 같다. DB는 지우지 말고 아래 cleanup 목록에 유지한다. CONTROL_TOKEN은 임의의 일회용 값으로 준비해 shell history·출력·access log에 넣지 않는다.

```bash
cd context_gateway
npm --prefix web run build
export CONTEXT_GATEWAY_DATABASE_URL='postgresql+asyncpg://winterholic@127.0.0.1:5432/test_cg_ui_d40c9adf'
export CONTEXT_GATEWAY_SNAPSHOT_DIR=/private/tmp/<run-dir>/snapshots
export CONTEXT_GATEWAY_WEB_DIR="$PWD/web/dist"
read -rs CONTEXT_GATEWAY_CONTROL_TOKEN; export CONTEXT_GATEWAY_CONTROL_TOKEN
../.venv/bin/python -m uvicorn context_gateway.web_public:app --host 127.0.0.1 --port <free-public-port> --no-access-log
../.venv/bin/python -m uvicorn context_gateway.control:app --host 127.0.0.1 --port <free-control-port> --no-access-log
```

Control API에 합성 snapshot을 게시하고 key를 발급한 뒤 raw key는 일회성 보호 입력으로 브라우저에 전달한다. MCP 검증은 별도 key/session을 사용하고 `POST <public-origin>/mcp`로 SDK initialize·tools/list·도구 호출을 수행한다. `/mcp/mcp`를 덧붙이지 않는다.

### 브라우저 검증 (2026-11-16)

설정된 `ttakkari_browser`로 실제 Gateway wrapper가 서빙하는 built UI를 확인했다. 기존 합성 fixture 기록은 이전 검증이고 이번 결과에는 포함하지 않았다.

| 확인 | 결과 |
|---|---|
| 390×844 | built UI에 새 key 교환 성공, 프로젝트·snapshot·문서 목록, overview 문서·출처/리비전/생성/미검증 표시 |
| 문서별 provenance(당시 동작) | overview, notes, config의 문서별 `source_refs`가 다르게 표시되고 snapshot-level 값을 함께 사용했다. timestamp/revision의 snapshot fallback은 과거 동작이며, 최신 문서별 provenance 동작은 현재 Vitest 회귀로 확인했다(이번 변경에서는 브라우저 재실행 안 함). |
| 1280×800 | 검색 결과·전체 맥락·세 문서가 좌측 탐색과 본문에 표시 |
| Search | `gateway-live-probe`가 실제 문서 3개를 반환 |
| 전체 export 다운로드 | 브라우저 완료 이벤트로 `context-…md`, `context-…json`, `live-ui-check-….zip` 확인 |
| 문서 다운로드 | `overview.md` 브라우저 완료 이벤트 확인 |
| 401 만료 | test DB에서 검증용 session `expires_at`만 과거로 설정; 다음 실제 search는 401, UI는 문서·토큰을 지우고 `세션이 만료됐습니다` 표시 |
| 401 재사용 | UI에서 한 번 성공 교환한 동일 key를 로그아웃 후 다시 입력; exchange 401 및 `키가 받아들여지지 않았습니다` 확인 |
| 429 | rate-limit 3인 실제 public 앱에서 document 429 / `Retry-After: 60`; UI가 재시도 버튼과 60초 안내 표시 |
| `/mcp` 안내 | UI 펼침 패널은 `${CONTEXT_GATEWAY_URL}/mcp`; 실제 SDK transport도 `/mcp`에 연결 |

브라우저의 자동 `/favicon.ico` 요청은 404였다. 인증 테스트에서 의도한 401 외에 페이지 이동 오류는 보지 않았다.

#### 브라우저 검증 재현 방법

```bash
cd context_gateway/web && npm ci && npm run build
node scripts/serve-web.mjs --port 4173                      # 합성 API. 실제 Gateway를 쓰려면: --upstream http://127.0.0.1:18787
```

- URL: `http://127.0.0.1:4173/` (같은 origin이 `/api`·`/mcp`까지 제공. 합성 모드는 `POST /__synthetic/reset`으로 키를 되살린다)
- 합성 키(1회용, 시나리오별 `synthetic-<시나리오>-<1..9>`): 값은 비밀이 아니다.

| 키 | 시나리오 | 볼 것 |
|---|---|---|
| `synthetic-alpha-1` | 프로젝트 1개, 전 scope | 정상 읽기·전체 맥락·검색·MD/JSON/ZIP, `overview`는 악성 Markdown(스크립트·onerror·javascript: 링크·외부 이미지·표·코드블록)이고 `window.__pwned`가 undefined여야 함. 스냅샷을 `rev-1`로 바꾸면 "미검증" 배지 |
| `synthetic-both-1` | 프로젝트 2개 | 프로젝트 전환, 이전 프로젝트 문서가 남지 않음 |
| `synthetic-empty-1` | 프로젝트 0개 | 빈 상태 |
| `synthetic-readonly-1` | read만 | 검색·내려받기가 "권한 없음" 안내 |
| `synthetic-ratelimit-1` | 문서·맥락·내보내기 429(Retry-After 20) | 429 안내 + 다시 시도 |
| `synthetic-short-1` | 세션 15초 | 15초 뒤 클릭하면 만료 안내와 로그인 화면, 문서 사라짐 |
| `synthetic-slow-1` | 응답 1.5초 지연 | 로딩 스켈레톤 |
| `synthetic-broken-1` | 스냅샷 목록 503 | 부분 오류(헤더·내보내기 유지, 목록에만 오류+재시도) |
| `synthetic-alpha-1` 두 번째 입력 | 재사용 | "키가 받아들여지지 않았습니다" |

- 확인 순서(도구 이름은 지시된 4개): `browser_navigate`로 URL 열기 → `browser_resize` 390×844 → `browser_snapshot`(로그인) → 키 입력·"키로 접속" → `browser_snapshot`(목록 뷰) → 문서 클릭(리더 뷰, "← 문서 목록") → `browser_take_screenshot` → `browser_resize` 1280×800 후 같은 과정 → Tab/↑↓/Enter로 문서 목록 이동, 다운로드 버튼으로 파일 저장 확인.
- 실제 Gateway로 검증하려면 위 live 절차로 Gateway를 띄우고 `--upstream`을 주며, 키는 `/tmp/ctx-gw-live/keys.json`에서 읽는다(키는 로그·채팅에 남기지 않는다).

### 미검증으로 남은 것

- 키보드로 모든 화면 요소를 전수 탐색하는 접근성 검증, Safari·Firefox의 마이크로초 시각 파싱, `.mcp.json`의 `${VAR}` 헤더 확장(MCP 클라이언트 구현에 달림), Docker 배포.
- 현재 관측 core 변경은 HEAD `f654054`에 커밋되지 않았다. 해당 core working tree가 다른 worker에 의해 바뀔 수 있으므로, 동일 SHA/working-tree 상태로 다시 띄운 별도 checkout의 재현은 미실행.

#### 실제 런타임·cleanup 기록 (2026-11-16)

- UI build: `npm run build` → TypeScript check 및 Vite build 성공 (`index-IyXtM50A.css`, `index-pldaL2gE.js`). 기존 94개 합성 테스트는 재실행하지 않았다.
- 앱 실행(working tree): `../.venv/bin/python -m uvicorn context_gateway.web_public:app --host 127.0.0.1 --port 19127 --no-access-log`; control은 같은 방식으로 `context_gateway.control:app --port 19129`. 두 `/healthz`가 200이었다. 429 UI는 실제 `web_public:app`의 별도 `:19131` 프로세스(`CONTEXT_GATEWAY_RATE_LIMIT=3`)에서 확인했다.
- DB: `test_cg_ui_d40c9adf`만 사용. 사용자 지시에 따라 유지하며 삭제하지 않았다. API가 게시한 snapshot bytes는 `/private/tmp/context-gateway-ui-qh8kajyy/snapshots`에 남아 있다.
- Cleanup 목록(의도적으로 보존): `test_cg_ui_d40c9adf` 데이터베이스 및 위 snapshot directory. 브라우저는 닫았다. 이 Run이 시작한 public/control/rate-limited-public PID는 shutdown을 시도했으나 Codex `exec_command` sandbox가 `ps` subprocess와 `os.kill(SIGTERM)`을 `PermissionError: [Errno 1] Operation not permitted`로 거부해 종료 여부를 확인하지 못했다. PID 파일은 run directory에 남아 있다. 같은 PID를 다른 경로로 재시도하지 않았다.
- 키 발급/교환은 합성 프로젝트에서만 수행. 임시 raw key/control token 파일은 삭제했다. 최초 브라우저 입력 도구의 결과가 합성 one-time key 하나를 action code에 원문 에코했다. 해당 키는 이미 사용된 뒤였고 재사용 검증에서 401을 받았다. 이후 키 입력 결과는 노출하지 않았다. 원문은 README나 Gateway access logs에 저장하지 않았다.
