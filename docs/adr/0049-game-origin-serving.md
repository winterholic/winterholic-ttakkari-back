# ADR 0049: 게임 빌드의 별도 origin 서빙 (TASK-G07)

- **상태**: 구현됨(로컬·ASGI·헤드리스 Chrome 검증), 운영 hostname/터널 라우트는 미설정 (2026-10-19)
- **설계 정본**: [`docs/design/game-studio.md`](../design/game-studio.md) §2.3, §6.2
- **구현**: `src/ttakkari/games/origin.py`, `POST /api/games/{id}/play`(`api/games.py`), `ttakkari serve-games`(`cli.py`), 검증 `tests/test_game_serving_security.py`

## 결정

불변 빌드 스냅샷(`data_dir/game-builds/<game>/<build>/bundle`)은 따까리 API와 다른 origin에서, 쿠키·bearer 없이 정적으로만 내려준다.

1. **접근 = 경로 속 서명 토큰.** 인증된 사용자가 `POST /api/games/{id}/play`로 `https://<게임 host>/p/<token>/index.html` URL을 받는다. 토큰은 JWT(aud `game-play`, 기본 1시간)이고 빌드 ID와 번들 상대 경로를 담는다. 게임 origin은 DB·세션·쿠키·인증 헤더를 읽지 않는다(코드 검사 테스트가 이를 고정). API bearer(aud `api`)는 게임 origin에서 403, 게임 토큰은 API에서 401이다. 번들 해시가 기록과 다르면 토큰을 발급하지 않는다(409).
2. **토큰이 경로에 있는 이유.** vite `base: './'` 빌드의 상대 자산 요청이 같은 토큰을 그대로 가져가므로 쿠키 없이 동작한다. `Referrer-Policy: no-referrer`로 새지 않게 하고, uvicorn access log의 토큰은 `PlayTokenAccessFilter`가 지운다.
3. **헤더.** CSP는 `default-src 'none'`에서 시작해 `script-src 'self' 'wasm-unsafe-eval'`, `connect-src 'self' data: blob:`(Phaser 로더 XHR), `form-action 'none'`, `base-uri 'none'`, `object-src 'none'`, `frame-src 'none'`, `frame-ancestors <PWA origin>`, `sandbox allow-scripts allow-pointer-lock allow-orientation-lock`이다. CORS는 `Access-Control-Allow-Origin: *`만(opaque origin의 module script에 필요) 내고 `Allow-Credentials`와 쿠키 설정 헤더는 내지 않는다. PWA의 iframe은 `sandbox`에 `allow-same-origin`을 넣지 않는다(API 응답의 `sandbox` 값 사용).
4. **경로 이탈.** `..`·빈 구간·`\`·NUL·숨김 파일·심볼릭 링크·번들 밖 실경로를 모두 거부한다. 토큰의 번들 경로가 `game_builds_dir` 밖이면 404.
5. **서빙 방식 둘.** (a) `GameOriginMiddleware`: `TTAKKARI_GAME_ORIGIN_HOST`가 설정되면 Host가 그 값인 요청을 가장 바깥에서 게임 앱으로만 보낸다. API 라우트는 그 host로 도달할 수 없고, 다른 host에는 `/p/` 경로가 없다. 기존 터널(→Caddy 8787→슬롯)을 그대로 쓴다. (b) `uv run ttakkari serve-games --port N`: 게임 origin만 별도 프로세스로 띄운다(로컬 검증·별도 포트용).
6. **postMessage 계약.** play 응답의 `origin`이 비교 대상이다. 부모(PWA)는 `event.origin === origin && event.source === iframe.contentWindow`와 스키마·크기를 검증한 메시지만 받고, 캡처 이미지는 HTML로 해석하지 않는다. 게임 쪽 bridge는 `#ttk-parent=<PWA origin>` 힌트(요청 `Origin`이 허용 목록일 때만 부여)와 `event.source === window.parent`를 모두 확인한 뒤 그 origin으로만 응답한다. 게임 쪽 bridge는 Phaser 템플릿 `src/bridge.ts`에 구현했다(캡처 요청 `{type:'ttakkari:capture', id}`에 `{type:'ttakkari:capture:result', id, width, height, dataUrl}`로 응답, 와일드카드 targetOrigin 없음, 검증 로직은 `tests/test_game_bridge.py`가 node로 실행). 부모(PWA) 쪽 수신 검증과 UI는 프론트 단위(G09~G10)에서 한다. 실제 iframe 안 캡처 왕복은 미검증이다.

## 설정

| 환경 변수 | 용도 |
|---|---|
| `TTAKKARI_GAME_ORIGIN_HOST` | 게임 hostname(포트 포함 가능). 비면 게임 서빙·play 발급이 모두 꺼진다. |
| `TTAKKARI_GAME_ORIGIN_URL` | 공개 기준 주소(`https://host`, 경로 없음). 운영은 https. |
| `TTAKKARI_GAME_FRAME_ANCESTORS` | iframe으로 끼울 PWA origin 목록(JSON). 비면 `cors_origins`. |
| `TTAKKARI_GAME_PLAY_TOKEN_TTL_SECONDS` | 토큰 수명(기본 3600). |

`origin_problem()`은 게임 host가 API/PWA/공유 origin과 같거나 URL이 host와 다르면 play 발급을 503으로 막는다.

## 운영 경로 조사와 미해결 점

- 현재 인프라: Cloudflare Tunnel `ttakkari-api.winterholic.net` → `127.0.0.1:8787`(Caddy, 세대 프록시) → 슬롯 8791~8793(`deploy/cloudflared-ttakkari.yml.example`, `generation_deployment.proxy_config`). Caddy `reverse_proxy`와 cloudflared는 Host를 바꾸지 않으므로 (a) 방식은 터널 ingress에 hostname 한 줄을 더하는 것만으로 동작해야 한다(코드로는 Host를 바꾼 요청으로 검증했고 실제 터널은 미검증).
- **사용자 결정 필요:** 게임 hostname. PWA(`ttakkari.winterholic.net`)·API(`ttakkari-api.winterholic.net`)와 같은 registrable domain(`winterholic.net`)의 하위 도메인은 same-site다. refresh 쿠키는 host-only이고 위 CSP(`connect-src 'self'`, `form-action 'none'`, opaque-origin sandbox)가 게임 JS의 API 호출을 막으므로 즉시 위험하지는 않지만, 설계서가 요구한 "다른 registrable domain"은 별도 도메인을 Cloudflare에 등록해야 충족된다.
- 이 Run에서 하지 않은 것: `~/.cloudflared`·DNS·`~/.ttakkari/config.env` 수정. 운영 적용 절차는 도메인 결정 → DNS/ingress 추가 → config.env에 위 변수 3개 → 배포 → `POST /api/games/{id}/play`로 URL 발급 → 모바일 실기기 확인이다.
- 미검증: iOS Safari, 모바일 터치, 실제 터널을 거친 지연·Range 동작. sandbox(opaque origin) 때문에 게임은 `localStorage`/IndexedDB를 쓸 수 없다(저장은 bridge/서버 경유로 별도 설계).
- 검증됨(로컬): 실제 Phaser 빌드를 별도 프로세스(`serve-games`)로 서빙해 curl로 index.html·JS 200, 쿠키·인증 헤더를 보내도 응답에 반영되지 않음, 같은 origin에서 `/api/games` 404. 헤드리스 Chrome(top-level)에서 CSP+sandbox 상태로 Phaser가 부팅해 `<canvas>`를 만들었다(iframe 안 동작은 G13).
