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

**정적 mount 경로.** 빌드 산출물 `web/dist/`를 FastAPI에서 `StaticFiles`로 서빙한다. API(`/api/v1/*`)와 `/mcp`보다 **나중에** mount해 라우트가 가려지지 않게 한다.

```python
from fastapi.staticfiles import StaticFiles

# create_public_app() 끝, 다른 라우트와 /mcp mount 뒤
app.mount("/", StaticFiles(directory=settings.web_dir, html=True), name="web")
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

`settings.web_dir`/`CONTEXT_GATEWAY_WEB_DIR`는 예시 이름이다. 실제 설정 이름은 core에서 정한다.

## 연결 안내(curl·MCP)

화면의 "curl·MCP로 쓰려면"은 환경변수 이름(`$CONTEXT_GATEWAY_URL`, `$CONTEXT_GATEWAY_KEY`, `$CONTEXT_GATEWAY_SESSION`)만 쓴다. 키는 `read -rs`로 받고 교환 직후 `unset`한다. 화면이 쓰는 키·세션 값은 이 문자열 생성 코드(`src/guide.ts`)로 들어가지 않는다.

## 디자인 토큰

`src/styles/tokens.css`는 front 저장소 `src/vendor/ttakkari/tokens.css`(ttakkari 1.3.3, f9d78eb)에서 `app.css`가 쓰는 토큰만 뽑은 생성물이다. 손으로 고치지 않고 아래로 다시 만든다. 외부 폰트·CDN·분석기는 없다(Pretendard·JetBrains Mono는 기기에 설치돼 있으면 쓰고 없으면 시스템 글꼴).

```bash
node scripts/extract-tokens.mjs <front>/src/vendor/ttakkari/tokens.css
```

## 검증 상태와 미검증 계약

- 실행·통과: `npm test`(vitest, 합성 Gateway), `npm run typecheck`, `npm run build`.
- **응답 어댑터는 실서버로 검증하지 않았다.** `src/api/adapters.ts`는 같은 checkout의 `context_gateway/src/context_gateway/apps.py`(작성 시점의 코드)를 읽고 맞춘 것이며, `src/test/fakeGateway.ts`가 그 모양을 흉내 낸다. 작성 시점에 `docs/context-gateway-contract.md`는 없었다. 통합 검증에서 실제 응답과 대조해야 하는 항목:
  - 교환 응답 `{access_token, token_type, expires_at}`와 `expires_at`의 시간대 표기(없으면 UTC로 읽는다)
  - 프로젝트 `{id, slug, name, description, latest_snapshot_id}`, 스냅샷 `{id, source_refs, source_revision, generated_at, verified_at, published_at, manifest_sha256, documents[]}`
  - 문서 `{id, title, content_type, sha256, content}`(출처 필드 없음), 전체 맥락 `{project, snapshot, documents[]}`, 검색 `{project_id, snapshot_id, query, results[{id,title,snippet,matches}]}`
  - 내보내기 `format=markdown|json|zip`, 파일 이름은 `Content-Disposition`(zip만 현재 제공), 없으면 `context-<id8>.<ext>`
  - 401/404/429(`Retry-After`) 의미. 404가 scope 부족도 포함하는지
  - `Accept: application/json` 요청에 JSON을 주는지(문서·프로젝트·전체 맥락은 content negotiation)
  - MCP 경로: `src/guide.ts`의 `MCP_PATH`는 `/mcp`로 두었으나 코드상 `/mcp`에 mount한 앱의 `streamable_http_path`도 `/mcp`라서 실제는 `/mcp/mcp`일 수 있다
  - `.mcp.json`의 `${VAR}` 확장이 사용하는 MCP 클라이언트에서 헤더에도 적용되는지
- **브라우저 화면 검증은 하지 않았다.** 이 작업 세션에는 지정된 `ttakkari_browser` MCP가 없었고, 셸 Chrome/Playwright로 우회하지 않았다. 390px·데스크톱 레이아웃, 키보드 조작, 실제 파일 저장(blob 앵커), CSP meta의 실제 브라우저 동작은 미검증이다.
