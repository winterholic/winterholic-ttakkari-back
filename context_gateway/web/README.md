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

1. 키 입력 → `POST /api/v1/auth/exchange` (`Authorization: Bearer <키>`)를 **한 번** 호출해 세션 토큰을 받는다.
2. 세션으로 `GET /api/v1/projects`부터 조회한다. 프로젝트 메타데이터·스냅샷 선택·전체 맥락·개별 문서·검색·내보내기(MD/JSON/ZIP)가 나머지 REST 경로다.
3. 키와 세션은 메모리(`SessionStore`와 `GatewayClient`의 `#token` 필드)에만 있다. localStorage·sessionStorage·쿠키·URL·콘솔·분석 도구로 내보내지 않는다. 새로고침하면 사라지고 다시 키를 넣어야 한다.
4. 만료 시각(교환 응답의 `expires_at`)이 되거나 데이터 요청이 401을 받거나 로그아웃하면 토큰과 화면의 문서를 지운다.
5. 호출 경로는 `src/api/paths.ts`의 허용 목록(교환 + 공개 REST 7개)뿐이다. Private API·로그인·refresh 경로는 만들 수 없다. 모든 요청은 `credentials: "omit"`, `referrerPolicy: "no-referrer"`, 상대 경로(같은 origin)다.
6. 다운로드는 Authorization을 붙인 `fetch` → `Blob` → `blob:` URL 앵커로 저장한다. URL에는 자격이 없다.
7. Markdown은 `react-markdown` + `remark-gfm`으로 그린다. 원시 HTML은 요소가 되지 않고 글자로 보이며, 링크는 `http(s):`·`mailto:`만 새 탭(`noopener noreferrer nofollow`)으로 열고, 이미지는 외부를 불러오지 않고 대체 텍스트만 보인다.
8. 문서별 `source_refs`·`source_revision`·`generated_at`·`verified_at`을 보인다. `verified_at`이 null이면 "미검증" 배지로 표시한다. 문서 단건 응답에는 출처 필드가 없어 스냅샷의 값을 상속한다(`mergeProvenance`).

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

**정적 서빙 통합 상태.** 현재 `create_public_app()`는 MCP 앱을 `app.mount("/", mcp_app)`로 루트에 mount한다. Core working tree에는 이를 감싸는 `web_public.py` 초안이 생겼지만, 가져오는 `context_gateway.web_serving` 모듈은 아직 없다. Dockerfile/배포 설정의 `web/dist` 복사도 현재 확인되지 않았다. core 소유 범위라 수정하지 않았다.

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

core 초안 `web_public.py`는 `CONTEXT_GATEWAY_WEB_DIR`(기본 `/app/web-dist`)를 참조한다. 실제 정적 서빙 경로는 `WebServingApp` 구현과 배포 설정이 완성된 뒤 확인해야 한다.

## 연결 안내(curl·MCP)

화면의 "curl·MCP로 쓰려면"은 환경변수 이름(`$CONTEXT_GATEWAY_URL`, `$CONTEXT_GATEWAY_KEY`, `$CONTEXT_GATEWAY_SESSION`)만 쓴다. 키는 `read -rs`로 받고 교환 직후 `unset`한다. 화면이 쓰는 키·세션 값은 이 문자열 생성 코드(`src/guide.ts`)로 들어가지 않는다.

## 디자인 토큰

`src/styles/tokens.css`는 front 저장소 `src/vendor/ttakkari/tokens.css`(ttakkari 1.3.3, f9d78eb)에서 `app.css`가 쓰는 토큰만 뽑은 생성물이다. 손으로 고치지 않고 아래로 다시 만든다. 외부 폰트·CDN·분석기는 없다(Pretendard·JetBrains Mono는 기기에 설치돼 있으면 쓰고 없으면 시스템 글꼴).

```bash
node scripts/extract-tokens.mjs <front>/src/vendor/ttakkari/tokens.css
```

## 검증 상태

실행·통과 (2026-11-16, 이 checkout):

| 검증 | 대상 | 결과 |
|---|---|---|
| `npm test` | 합성 Gateway(`src/test/fakeGateway.ts`) | 94 통과, live 5개는 환경변수 없으면 skip |
| `npm run typecheck`, `npm run build` | | 오류 없음 |
| **실제 Gateway 대조** | `apps.py` 실서버(PostgreSQL 17 + uvicorn)에 `GatewayClient` 어댑터 | `src/api/live.test.ts` 4개 통과 |
| **실제 Gateway + UI 전체 흐름** | 로그인→읽기→스냅샷→검색→ZIP→로그아웃(jsdom) | `src/live.app.test.tsx` 1개 통과 |
| MCP 경로 | 실서버 `POST /mcp` + 세션 Bearer | `initialize`·`tools/list` 200, 도구 4개. `/mcp/mcp`는 404 → `MCP_PATH="/mcp"` 확정 |

실제 Gateway 응답과 어댑터를 대조한 결과(불일치는 없었고 차이는 아래 둘뿐이다).

- `expires_at`·`published_at`은 `2026-11-16T02:21:24.472494Z`처럼 **마이크로초 + Z**다. V8(Node)에서 `Date.parse`가 읽는다. Safari·Firefox의 마이크로초 파싱은 **미검증**이다.
- 문서 메타에 `extension`(`md`/`txt`/`json`)이 추가돼 있다. 어댑터는 모르는 필드를 무시하므로 그대로 동작한다(저장 파일 이름은 content_type으로 정한다).
- 문서 단건 응답에는 출처 필드가 없다 → 스냅샷의 `source_refs/source_revision/generated_at/verified_at`을 상속한다(실측 일치).
- 재사용 키 401, 다른 프로젝트·scope 부족 404, 잘못된 Accept 406, 없는 snapshot_id 404는 `Retry-After` 헤더 429를 제외하고 실제로 관측했다. **429 응답은 실서버에서 유발해 보지 않았다**(합성 서버 시나리오 `ratelimit`와 단위 테스트로만 확인).

현재 working tree의 core 변경도 대조했다. `verified_at`은 nullable/optional이고 문서별 `source_refs`가 추가됐다. 어댑터는 null을 미검증으로 표시하며, 문서별 출처가 비어 있으면 스냅샷 provenance를 상속하므로 응답 모양 변화와 호환된다. 계약 문서에는 아직 두 변경이 반영되지 않았다: `verified_at`을 필수로 적고 문서별 `source_refs`를 설명하지 않는다. core 소유 문서라 여기서는 수정하지 않았다.

**Core 통합 계약 한 줄:** `web/dist`를 `CONTEXT_GATEWAY_WEB_DIR`에서 `/app/`으로 제공하고 `/api/*`·`/mcp` 요청은 현재 public ASGI 앱에 그대로 전달하며, Docker Node 단계에서 빌드한 `dist`만 Python 이미지로 복사한다.

### 실제 Gateway 대조 재현

```bash
cd context_gateway/web
scripts/live/start-gateway.sh          # 격리 PostgreSQL 17 + public :18787 + control :18789 (macOS, /opt/homebrew/opt/postgresql@17 필요)
python3 scripts/live/seed.py           # 합성 스냅샷 발행 + 1회용 키 발급 → /tmp/ctx-gw-live/keys.json (키는 출력하지 않음)
GATEWAY_LIVE_URL=http://127.0.0.1:18787 npx vitest run src/api/live.test.ts   # 키가 1회용이라 실행 전마다 seed.py를 다시 돌린다
scripts/live/stop-gateway.sh
```

`scripts/live/control_launch.py`는 **코어 결함을 우회하는 하네스**다. 현재 코어 `publish_snapshot`은 stage 디렉터리를 `0o555`로 만든 뒤 다른 부모로 `os.replace`해서 macOS에서 `PermissionError`(`POST /control/v1/snapshots` 500)가 난다. 코어가 고쳐지면 하네스 없이 `context_gateway.control:app`을 쓴다. 이 결함은 코어 소유라 수정하지 않고 작업 보고에만 남겼다.

### 브라우저 검증 (2026-11-16)

설정된 `ttakkari_browser`로 합성 Gateway UI를 확인했다. 실제 Gateway는 이 검증 중 실행하지 않았으므로, 아래 결과는 UI 동작의 브라우저 확인이며 실 API 런타임 확인과 구분한다.

| 확인 | 결과 |
|---|---|
| 390×844 로그인 | 로그인 폼·키 입력이 표시됨. 키 입력 후 Enter로 교환 성공 |
| 390px 정상 상태 | 프로젝트·스냅샷·문서 목록과 검색·Markdown/JSON/ZIP 동작 표시. 본문 provenance에 source refs, revision, generated/verified 시각 표시 |
| 데스크톱 1280×800 | 좌측 탐색과 본문·출처 영역이 나란히 표시됨 |
| 390px / 데스크톱 오류 상태 | 합성 스냅샷 목록 503이 목록 안의 오류 및 다시 시도 버튼으로 표시되고 프로젝트 헤더·내보내기는 유지됨 |
| 키보드 | 로그인 폼에서 Enter로 접속, Tab으로 문서 목록에 포커스 이동 후 Enter로 문서 열기 |
| Markdown 문서 다운로드 | 브라우저가 `overview.md` 다운로드 완료 이벤트를 보고함 |
| ZIP 다운로드 | 브라우저가 `synthetic-alpha-…zip` 다운로드 완료 이벤트를 보고함 |
| Markdown 안전 렌더링 | script·onerror는 텍스트로 표시, javascript 링크는 비활성, 외부 이미지는 로드하지 않음 |

로그에 남은 404는 브라우저 자동 요청 `/favicon.ico`이고, 503은 위 오류 상태를 확인하기 위해 합성 서버가 반환한 응답이다. 그 외 콘솔 경고는 없었다.

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

- 실제 Gateway 런타임을 이번 브라우저 검증과 동시에 연결한 화면 흐름. 실제 REST 어댑터·MCP smoke의 앞선 실행 결과는 위 표에 적었지만, 이번 Run에서는 Gateway를 재기동하지 않았다.
- 키보드로 모든 화면 요소를 전수 탐색하는 접근성 검증, 실서버 429 응답, Safari·Firefox의 마이크로초 시각 파싱, `.mcp.json`의 `${VAR}` 헤더 확장(MCP 클라이언트 구현에 달림).
