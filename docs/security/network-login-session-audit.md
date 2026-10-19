# 네트워크 접근 통제·로그인 제한·세션 보안 감사 (2026-10-19)

SEC2 보안감사 1단계 중 "네트워크 접근 통제·로그인 제한·세션 보안" 파트의 산출물이다.
실행 항목은 [보안 WBS](wbs.md) 6절(NAC·LGN·SES)에 있고, 위협 모델은 [threat-model.md](threat-model.md)에 있다.
코드 기준점은 `back` `dfee8d4`(작업 트리에 다른 작업자의 미커밋 변경 있음, 이 문서는 건드리지 않음), `front` `630040b`다.

**개정 2 (2026-10-19, 독립 검증 반영).** 독립 검증(Codex/GPT-6 Luna)이 failed로 판정해 아래를 고쳤다. 검증 보고서 원문은 이 Run의 guard가 다른 Run의 outbox 읽기를 막아 열지 못했고(`[ttakkari-guard] ttakkari 보호 영역 접근 차단`), 검증 세션 기록(`read_session`)의 판정 요약으로 반영했다. 요약에 없는 세부 지적은 확인하지 못했다.

| 검증 지적 | 처리 |
|---|---|
| 주장 2: "408개 소유자 API, 방어선 비밀번호 하나"가 인벤토리와 불일치(422 HTTP + 1 WS, 소유자 301) | 2.2절에서 재산정하고 0절·2.1·3.3절 문구 정정. 408은 소유자 API가 아니라 **이전 시점의 HTTP 전체 수**였고, 이를 소유자 라우트 수로 쓴 것이 오류였다 |
| 주장 4: refresh_reuse 13건은 IP 2개, 동시 갱신 원인 미입증 | 6.1절에서 사실 정정(IP 2개), 같은 초 패턴 증거를 표로 추가하고 원인은 **추론**으로 표시, 확인 방법 계획 추가 |
| 로그인 제한 설계 결함(잠금 지속, 동시 정답·오답 레이스, 쿠키 예외 혼재, IP 회전, 감사) | 5절 전면 재작성(대안 비교, 레인 분리, 원자 예약, 단계 전환, 테스트) |
| 주장 6~9 미검증·추가(Serve 위조 가능성, 방화벽, bootstrap 공개 URL 호출, 쿠키) | 4.4절 P0~P5에 bootstrap 경로 반영, 미검증 항목은 확인 필요 유지 |
| Serve/Funnel 현재 상태 | 호스트에서 재확인(2026-10-19 21:56 KST, 둘 다 `No serve config`) |

표기 규칙은 기존 문서와 같다. **확인됨**은 이번 조사에서 명령 결과나 코드 줄로 직접 확인한 것, **추론**은 코드 구조로 보아 그럴 가능성이 높지만 재현하지 않은 것, **확인 필요**는 근거를 얻지 못한 것이다.

이번 단계는 분석과 계획만 했다. 운영 코드, 운영 설정, 포트, 방화벽, 터널, Tailscale 설정은 바꾸지 않았다. 비밀 값(`config.env`, 토큰, 키)은 읽거나 기록하지 않았다.

## 0. 결론

공개 도메인을 통한 **우회 접근은 확인되지 않았다**. 인증이 필요한 라우트는 전부 401이었고, 헤더 위조(Tailscale-User-*, X-Forwarded-For, CF-Connecting-IP, Host)는 인증을 우회하지 못했다.
다만 **공개 터널에 경로 필터가 없어 현재 라우트 전체(HTTP 422개 + WebSocket 1개, 소유자 범위 301개)가 인터넷에서 도달 가능**하다. 네트워크 계층 방어는 없고 애플리케이션 인증 계층만 있다.
그 계층은 소유자 라우트의 access JWT(서명키 필요), refresh 쿠키(HttpOnly, DB 해시, 회전·재사용 탐지), 에이전트 라우트의 세션 JWT(`aud`·`rid` 바인딩), 다운로드·업로드 토큰, 원격 데스크톱의 재인증·티켓이다.
외부인이 새 소유자 세션을 얻는 정상 경로는 **비밀번호 로그인 하나**이고 그 앞에는 메모리 안의 IP별 실패 카운터뿐이다. 서명키 유출·위조, 쿠키 도난, 프레임워크·인증 파서 결함은 별도 경로다. 소유자 세션을 얻으면 에이전트 실행(= 이 Mac의 명령 실행)까지 열린다.

| ID | 발견 | 위험도(L×I) | 상태 | 권고 | 사용자 승인 |
|---|---|---|---|---|---|
| F1 | 공개 터널에 경로 필터가 없어 전체 라우트(HTTP 422 + WS 1, 소유자 301)가 도달 가능. 네트워크 경계 없음 | **P0 6 (2×3)** | 확인됨 | Tailscale Serve를 내부 입구로 병행 도입 후, 공개 터널은 공개 경로 허용목록만 통과 | **필요** (방식, PWA origin 이전, 관리 콘솔 작업) |
| F2 | 로그인 실패 제한이 프로세스 메모리·IP 단위. 재시작·배포·세대 전환마다 초기화, 계정 단위 제동 없음 | P1 6 (2×3) | 확인됨(코드) | DB 영속 단계 지연. 출처·기기 키는 잠금, **계정 단위는 잠금이 아니라 속도 제한**, 신뢰 기기 레인 분리, 원자 예약, shadow→enforce | **필요** (정책값, 신뢰 기기 쿠키, 새 기기 등록 코드) |
| F3 | refresh 재사용으로 계열이 폐기된 사건 13건(10-13부터 10-19, IP 2개, 두 IP 모두 정상 로그인 이력 있음). 13건 중 8건이 직전 회전과 같은 초 안이라 동시 갱신 정황이지만 **원인은 추론**이고 도난 가능성을 배제하지 못함 | P1 4 (2×2) | 확인됨(DB), 원인 추론 | 원인 확정 계측(읽기 전용 확인 계획 6.1) → 동시 갱신 잠금(프런트) + 서버 유예창 | 불필요 |
| F4 | 개별 세션 폐기·목록 없음, access JWT에 세션 ID 없음 | P1 2 (1×2) | 확인됨(코드) | `sid` 클레임 + 서버 폐기 확인(캐시), 기기별 세션 API | 불필요 |
| F5 | Tailscale 도입 시 CF-Connecting-IP 헤더를 tailnet 클라이언트가 위조 가능(현재 인터넷 경로는 Cloudflare가 거부) | P1 2 (2×1) | 추론(코드 확인됨) | 프록시 이중 리스너로 헤더 신뢰 경로 분리 | 불필요 |
| F6 | macOS 방화벽 꺼짐, `mariadbd *:3306`·AirPlay 5000/7000·rapportd가 LAN(그리고 tailnet)에 열림. tailnet 정책(ACL) 미확인 | P1 4 (2×2) | 확인됨(관측) | tailnet Grants 최소화, 3306 루프백 바인딩, 앱 방화벽 | **필요** (따까리 밖 설정) |
| F7 | refresh 쿠키가 SameSite=None. 현재 PWA와 API는 같은 사이트(winterholic.net)라 불필요 | P2 2 (1×2) | 확인됨(운영 응답) | Strict 전환(origin 이전과 함께 결정) | 불필요 |
| F8 | refresh 절대 수명 상한 없음(30일 슬라이딩), 만료·폐기 행 정리 없음 | P2 2 (1×2) | 확인됨 | 절대 상한(90 또는 180일) + 정리 | **필요** (상한 값) |
| F9 | SSE 스트림·원격 데스크톱 연결이 접속 시점에만 인증 | P2 2 (1×2) | 확인됨(코드) | 주기 재검증(`sid`·credential epoch) | 불필요 |
| F10 | 429·잠금 이벤트가 감사 기록과 알림에 남지 않아 무차별 대입 시도를 볼 수 없음 | P2 2 (1×2) | 확인됨(코드) | 잠금 전이 감사 + 푸시 | 불필요 |
| F11 | SPA 응답에 CSP·frame-ancestors 없음(HSTS만) | P2 | 확인됨(응답 헤더) | 기존 SEC2-WEB-01로 처리 | 불필요 |

양호한 점: 세션 ID(refresh)는 384비트 난수로 예측 불가, 로그인 시 항상 새 계열 발급(세션 고정 불가), 쿠키는 HttpOnly·Secure·경로 한정, refresh/logout CSRF 방어, 재사용 탐지, 비밀번호 변경 시 refresh 폐기, 코드에 Tailscale 헤더 신뢰 없음.

## 1. 방법과 증거 범위

- 읽기 전용: `lsof`, `launchctl print`, `tailscale status/serve status/funnel status/debug prefs`(읽기), `docker ps`, `socketfilterfw --get*`, 운영 DB 집계 SELECT(`default_transaction_read_only=on`), 코드 읽기.
- 외부 프로브(공개 도메인 `ttakkari-api.winterholic.net`, `ttakkari.winterholic.net`): GET 약 19회, 위조 헤더 GET 14회, WebSocket 핸드셰이크 2회(Origin 불량·없음, 모두 거절되어 연결 슬롯 사용 없음), CORS preflight(OPTIONS) 2회. 부하·공격 시험은 하지 않았다.
- GET이 아닌 요청 2건: `POST /api/auth/refresh`(가짜 쿠키, 상태 변경 없음, 쿠키 속성 확인용), `POST /api/auth/login`(빈 본문, 검증 단계 422에서 종료되어 실패 카운터에 영향 없음).
- 이 Run의 guard에 막힌 명령(우회하지 않음, 엔진 claude):
  - 변수로 명령 이름을 구성한 실행(`$TS status` 형태) → "명령 이름을 변수·치환으로 구성한 실행 차단". 전체 경로를 직접 써서 다시 실행했다.
  - `systemsetup -getremotelogin`, `defaults read /Library/Preferences/com.apple.RemoteManagement`, `launchctl print system/com.apple.screensharing` 묶음 → "고위험 명령 차단(시스템 보안 설정 변경)". **원격 로그인(SSH)·화면 공유 설정 상태는 확인하지 못했다**(확인 필요). 대신 `lsof`로 22·5900 리스너가 없음을 확인했다.
  - `~/.ttakkari/logs/deployer.log` 읽기, `.env`가 포함된 grep → "보호 영역 접근 차단·민감 파일 차단".
- 비밀 취급 기록: `front/.env*`를 `cat`하면서 `VITE_API_BASE` 값을 확인했다. 이때 `.env.local`에 Vercel OIDC 토큰 키가 있음을 보게 되어, 출력은 `sed`로 값을 가린 상태였고 이 문서에는 값을 쓰지 않았다. 이 파일을 열 의도는 아니었으므로 기록해 둔다. `~/.cloudflared/ttakkari.yml`은 터널 ID·자격 증명 경로를 가린 채 ingress 규칙만 읽었다. `~/.ttakkari/deploy/proxy.json`은 생성된 Caddy 설정(비밀 없음)이다.

## 2. 실제 운영 네트워크 구조

```
[iPhone PWA / 브라우저]
   │ HTTPS
   ├─▶ ttakkari.winterholic.net ── Cloudflare ── Vercel (정적 SPA, 비밀 없음)      server: cloudflare, HSTS만
   │
   └─▶ ttakkari-api.winterholic.net ── Cloudflare ── cloudflared 터널(아웃바운드 연결)
                                                        │ http_status:404 기본 규칙 외 호스트 1개
                                                        ▼
                                     127.0.0.1:8787  Caddy (launchd net.winterholic.ttakkari.proxy)
                                        │ /api/ready, /api/bootstrap-ready 만 404, 나머지 reverse_proxy
                                        ▼
                                     127.0.0.1:8791  uvicorn 세대 프로세스(8791/8792/8793 슬롯)
                                        ├─ PostgreSQL 127.0.0.1:5432 (pg_hba trust, ADR 0048 2.4)
                                        ├─ 엔진 Run → stdio MCP → 127.0.0.1:8787 /api/agent·/api/orchestrator
                                        └─ 원격 데스크톱 WS 브리지 → 127.0.0.1:5900 (화면 공유, 현재 리스너 없음)
Tailscale: 설치·실행 중(1.102.4, 독립 배포판 앱 + 시스템 확장). Serve·Funnel 설정 없음(21:56 KST 재확인). RunSSH=False, ShieldsUp=False.
```

확인된 사실(근거):

| 항목 | 값 | 근거 |
|---|---|---|
| 백엔드 바인딩 | 127.0.0.1만. 8787 Caddy, 8791 uvicorn | `lsof -iTCP -sTCP:LISTEN`, `launchctl print`(`--host 127.0.0.1 --port 8791`) |
| 외부 입구 | Cloudflare Tunnel 1개, 호스트 `ttakkari-api.winterholic.net` 1개 | `~/.cloudflared/ttakkari.yml` ingress |
| 터널 경로 필터 | 없음(호스트만 매칭) | 같은 파일, `path` 규칙 없음 |
| Caddy admin | 127.0.0.1:20191 (persist=false) | `proxy.json`, `generation_deployment.py:222` |
| 비루프백 리스너 | `*:3306` mariadbd, `*:5000`·`*:7000` ControlCenter, `*:49573` rapportd | `lsof` |
| macOS 방화벽 | 꺼짐(`State = 0`), 스텔스 모드 꺼짐 | `socketfilterfw --getglobalstate` |
| Tailscale | Running. tailnet `tail4d9637.ts.net`, 이 Mac의 이름 `winterholic-macstudio`, 피어 7대(윈도우 5, ubuntu 서버 1(`tag:ubuntu-server`), iPhone 1), `CertDomains` 없음(HTTPS 인증서 미활성) | `tailscale status --json` |
| Tailscale Serve/Funnel | 설정 없음. 2026-10-19 21:56 KST 재확인 | 호스트(엔진 claude) `/Applications/Tailscale.app/Contents/MacOS/Tailscale serve status` → `No serve config`, `funnel status` → `No serve config`(둘 다 exit 0) |
| Docker | 포트가 모두 `127.0.0.1`에 게시(다른 프로젝트 컨테이너 17개). 따까리 서비스용 컨테이너 없음(게임 빌드는 필요 시 기동) | `docker ps` |
| 인바운드 webhook | **없음**(백엔드 코드에 webhook 라우트 0건) | `grep -ri webhook` |
| Tailscale 헤더 사용 | **없음**(`Tailscale-User-*`, `X-Forwarded-*` 신뢰 코드 0건) | `grep`, 아래 3절 |
| 클라이언트 IP | 피어가 루프백이면 `CF-Connecting-IP`를 신뢰 | `security/auth.py:76-88` |
| uvicorn 프록시 헤더 | 기본값(`proxy_headers=True`, 신뢰 IP 127.0.0.1) | `cli.py:20`(옵션 미지정) |
| 원격 데스크톱 | Device 기본값 `127.0.0.1:5900`. 화면 공유 리스너는 확인 시점에 없음 → 기능은 현재 꺼진 상태로 추정(확인 필요) | `remote_desktop.py:31`, `lsof` |
| 공개 URL 의존 | 세대 배포 검증이 공개 URL의 `/api/health`, `/api/version`을 호출. 최초 부트스트랩 스모크는 공개 URL에 소유자 토큰으로 `/api/workspaces`와 SSE 호출 | `generation_deployment.py:384`, `deployment.py:289`, `generation_bootstrap.py:143` |

### 2.1 진입점 구분표: 공개 유지 필요 / 내부 전용

"공개 유지"는 tailnet 밖의 사람이나 시스템이 접근해야 하는 것이다. 판단 근거가 약한 칸은 확인 필요로 표시했다.

| 진입점 | 소비자 | 인증 | 판정 |
|---|---|---|---|
| `/api/health`, `/api/version` | 세대 배포 검증(공개 URL 호출) | 없음 | **공개 유지**(필드 최소화는 SEC2-EXPO-02). 배포 검증을 tailnet URL로 옮기면 내부화 가능 |
| `/api/public/shares/{sid}`, `/download` | 공유 링크 받는 사람(`/share/*` SPA 페이지) | capability 토큰 | **공개 유지** |
| `/api/qwen-status` | 소비처 불명 | 없음 | 내부 전용 후보(소비처 확인 필요) |
| `/api/dl/{token}`, `/api/view/{token}` | 소유자 기기에서 여는 5분 링크 | 토큰 | 내부 전환 가능. 단 공유·내보내기 링크를 외부인에게 주는 경로가 있는지 확인 필요 |
| `/api/auth/login`, `refresh`, `logout` | 소유자 브라우저 | 비밀번호·쿠키 | 내부 전용(tailnet 전환 후) |
| `/api/transfers/raw` 등 단축어 업로드 | iPhone 단축어(`ttk_` 토큰) | 업로드 토큰 | 내부 전환 가능(단축어 실행 시 폰이 tailnet에 연결되어야 함, 실측 필요) |
| 소유자 API 나머지(소유자 301 중 위 항목 제외) | PWA | access JWT | 내부 전용 |
| `/api/agent`·`/api/orchestrator`(agent 범위 105) | 엔진 프로세스가 **루프백**으로 호출 | 에이전트 JWT | 내부 전용. 지금은 공개 도메인으로도 도달(401) |
| `/api/remote-desktop/*`, WS `/socket` | PWA | JWT + 재인증 grant + 티켓 | 내부 전용 |
| `/api/ready`, `/api/bootstrap-ready` | 배포 감독(루프백) | 없음 | 이미 Caddy가 404 차단(확인됨) |
| 게임 origin(ADR 0049) | 게임 플레이 | play 토큰 | 터널 ingress에 해당 호스트 없음 → 현재 미공개로 추정(`TTAKKARI_GAME_ORIGIN_HOST` 값은 열지 않음, 확인 필요) |
| Vercel SPA `ttakkari.winterholic.net` | 브라우저 | 없음(정적) | 공개 유지. `/share/*`가 이 호스트에서 열림 |

외부 연동(MCP, GitHub, 날씨, NotebookLM·토스 Chrome, 사용량 조회, Web Push)은 모두 **아웃바운드**다. 외부에서 들어오는 webhook 진입점은 없으므로, 공개 경로를 줄여도 webhook은 깨지지 않는다.

### 2.2 라우트 수 재산정(검증 지적 1 반영)

산정 시점은 2026-10-19 21:50 KST경, `back` `ede2bf2`다. 기준은 기존 `tests/test_route_inventory.py`의 `collect_routes()`다. FastAPI `_IncludedRouter`를 펼쳐 (method, path)를 키로 모으고, 인증 의존성 종류로 분류한다. 운영 설정·DB·비밀은 쓰지 않는다.

```
export TTAKKARI_NO_ENV_FILE=1 TTAKKARI_JWT_SECRET=<합성 값> HOME=<임시 디렉터리>
cd back && .venv/bin/python - <<'EOF'
import sys; sys.path.insert(0, "tests")
import test_route_inventory as t, collections
routes = t.collect_routes()             # {(method, path): label}
print(collections.Counter(routes.values()))
EOF
```

| 분류(label) | 수 | 의미 |
|---|---|---|
| owner-plain 247 + owner-scoped 54 | **301** | 소유자 access JWT(`require_user`). scoped는 `user_access` 검사 포함 |
| agent-plain 63 + agent-scoped 42 | 105 | 에이전트 세션 JWT(`require_orchestrator`) |
| transfer-plain | 2 | 업로드 토큰 또는 소유자(`_simple_auth`) |
| public | 14 | 인증 의존성 없음. `/api/auth/{login,refresh,logout}`, `/api/{health,version,qwen-status,ready,bootstrap-ready}`, `/api/{dl,view}/{token}`, `/api/public/shares/{sid}`(GET·HEAD)와 `/download`(GET·HEAD). 토큰 경로는 핸들러 안에서 검사 |
| ws-ticket | 1 | WebSocket `/api/remote-desktop/socket` |
| **HTTP 합계** | **422** | GET 208, POST 142, DELETE 33, PATCH 28, PUT 9, HEAD 2 |
| 전체(HTTP + WS) | 423 | |

이전 위협 모델의 408은 "HTTP operation 전체"(소유자는 289)였다. 이 문서 초판이 408을 소유자 API 수로 쓴 것은 오류였다. 두 수치의 차이(408에서 422, 289에서 301)는 이후 추가된 라우트(공유 HEAD 라우트, 게임 롤백 등)로 설명되나 커밋별 증분은 확인하지 않았다(확인 필요). 위 표의 `public` 14에는 `/api/ready`, `/api/bootstrap-ready`가 들어가지만 Caddy가 404로 막으므로 터널로는 도달하지 않는다(확인됨, 3.1절).

## 3. 일반 인터넷 경유 우회 접근 점검

대상은 `https://ttakkari-api.winterholic.net`. 모두 확인됨(2026-10-19 12:33 UTC 전후).

### 3.1 익명 GET

| 경로 | 결과 | 해석 |
|---|---|---|
| `/api/health` | 200 `{"ok":true}` | 의도된 공개 |
| `/api/version` | 200, `revision`(커밋 SHA 전체)과 `pid` 노출 | 의도된 공개이나 정보량 과다(SEC2-EXPO-02) |
| `/api/qwen-status` | 200, 모델 상태 | 공개 필요성 불명 |
| `/api/ready`, `/api/bootstrap-ready` | 404(Caddy) | 차단됨 |
| `/docs`, `/openapi.json` | 404 | 운영에서 숨김 |
| `/api/auth/me`, `/api/workspaces`, `/api/agent/sessions`, `/api/orchestrator/context`, `/api/remote-desktop/devices` | 401 | 인증 필요 |
| `/api/dl/x`, `/api/view/x` | 403 | 토큰 검사 |
| `/api/public/shares/<없는 id>` | 404 `share_unavailable` | 정상 |
| `/api/system/backup-status` | 404 | **운영 리비전 `d922465`에 이 라우트가 없음**(커밋 `7c1ed5c`가 조상이 아님). 운영이 최신 main보다 뒤이거나 라우트 경로가 다름. 배포 상태는 확인 필요 |
| `/`, `/api/nonexistent` | 404 | 정상 |

### 3.2 헤더 위조

| 위조 헤더(요청에 추가) | `/api/auth/me`·`/api/workspaces` 결과 |
|---|---|
| `Tailscale-User-Login`, `Tailscale-User-Name` | 401, 401 |
| `X-Forwarded-For: 127.0.0.1`, `X-Real-IP: 127.0.0.1`, `X-Forwarded-Host: localhost` | 401, 401 |
| `CF-Connecting-IP: 127.0.0.1` | **403**(Cloudflare 엣지가 거절, 앱에 도달하지 않음) |
| `Host: 127.0.0.1:8787` | 403(엣지) |

코드로 확인한 결론:

- `Tailscale-User-*`는 어디에서도 읽지 않는다. 위조해도 효과가 없다.
- 인증은 Bearer JWT 서명과 `aud`·`sub` 검사뿐이고 소스 IP·헤더에 기대지 않는다(`security/auth.py:58-71`). 따라서 헤더 위조로 인증을 우회할 경로는 없다.
- 헤더가 영향을 주는 곳은 `client_ip()` 하나다(`security/auth.py:79-88`). 피어가 루프백이면 `CF-Connecting-IP`를 믿고 로그인 제한 키와 감사 IP로 쓴다. 인터넷 경로에서는 Cloudflare가 이 헤더를 스스로 설정하고 클라이언트가 보낸 값은 위 시험처럼 거절된다. 그러나 **루프백에서 접근하는 모든 것**(같은 uid의 에이전트, 그리고 앞으로 Tailscale Serve)은 이 헤더를 임의로 정할 수 있다(S15의 확장, F5).
- WebSocket `/api/remote-desktop/socket`: 불량 Origin·Origin 없음 모두 403(핸드셰이크 거절, `api/remote_desktop.py:119-123`). 허용 Origin을 흉내 낸 연결은 슬롯을 점유하므로 시험하지 않았다. 허용 Origin은 브라우저가 아닌 클라이언트도 흉내 낼 수 있으므로 Origin은 인증이 아니다. 실제 방어는 30초 일회 티켓과 10분 grant다(`remote_desktop.py:56,68`, 코드로만 확인).
- CORS: 허용 origin `https://ttakkari.winterholic.net`만 `Access-Control-Allow-Origin`을 받고, 불량 origin의 preflight는 400.

### 3.3 판단

우회는 없지만 **공개 표면이 크다**. 공개 도메인에서 도달 가능한 소유자 라우트 301개, 에이전트·오케스트레이터 105개, 업로드 2개, 원격 데스크톱 WS 1개(2.2절 집계)는 대부분 자체 취약점이 없더라도 (a) 비밀번호 추측(새 세션을 얻는 유일한 정상 경로), (b) 프레임워크·인증 파서 취약점, (c) 앞으로 추가될 라우트의 인증 누락, (d) 서명키·쿠키 유출 시의 재사용에 노출된다. 인벤토리 테스트(SEC2-GATE-00)가 (c)를 줄여 주지만 (a)(b)는 네트워크 경계로만 닫힌다. 운영 DB에서 확인한 실제 시도 흔적은 `auth.login denied` 3건(2건이 10-11, 1건이 10-13, 서로 같은 IP)뿐이라 지금까지 실공격의 증거는 없다. 단 429로 막힌 시도는 기록되지 않으므로(F10) 이 숫자는 하한이다.

## 4. Tailscale 접근 통제 설계안

### 4.1 현 환경의 제약

1. **PWA와 API는 현재 같은 사이트다.** `ttakkari.winterholic.net`(Vercel)과 `ttakkari-api.winterholic.net`은 둘 다 `winterholic.net` 아래라 same-site다. ADR 0004는 "다른 사이트"를 전제로 `SameSite=None`을 골랐지만 지금은 그렇지 않다.
2. API를 `*.ts.net`으로 옮기면 SPA(Vercel)에서 API는 **cross-site**가 된다. refresh 쿠키가 서드파티 쿠키가 되어 iOS Safari·홈 화면 PWA에서 차단될 수 있다(ITP, **확인 필요**: 실기기 시험). 막히면 앱을 열 때마다 재로그인하게 되므로 사용자 요구와 충돌한다.
3. 배포 감독이 **공개 URL을 직접 호출**한다(4.4.1절 목록). 공개 경로를 줄일 때 `/api/health`, `/api/version`은 남겨야 하고, 최초 부트스트랩 스모크는 소유자 토큰으로 `/api/workspaces`와 SSE까지 공개 URL로 호출하므로 별도 처리가 필요하다.
4. Tailscale HTTPS 인증서가 아직 활성화되지 않았다(`CertDomains` 없음). Serve의 `--https`와 PWA(secure context, 푸시)에 필요하다. 관리 콘솔에서 사용자가 켜야 한다.
5. tailnet에는 윈도우 PC 5대와 ubuntu 서버(`tag:ubuntu-server`)가 같이 있다. 태그가 있다는 것은 ACL 정책이 쓰이고 있다는 뜻이지만 **정책 내용은 확인하지 못했다**(관리 콘솔 접근 필요). 기본 allow-all이면 이 노드들이 Mac Studio의 `3306`, `5000`, `7000`, 화면 공유에 닿는다.
6. Mac 방화벽이 꺼져 있다. 터널은 아웃바운드 연결이라 방화벽으로는 터널 경유 접근을 막지 못한다. 방화벽은 LAN·tailnet 쪽 보조 수단이다.

### 4.2 선택지 비교

| 방식 | 경계가 네트워크에서 강제되나 | 모바일 PWA | 구현 난이도 | 판정 |
|---|---|---|---|---|
| **A. Tailscale Serve**(tailnet 전용 HTTPS → Caddy 8787) | 예. tailnet에 인증된 기기만 도달. Funnel이 아니므로 공개되지 않음 | iPhone에 Tailscale 연결 필요. same-origin이면 쿠키 문제 없음(A1) | 낮음~중간 | **권고** |
| B. Caddy/uvicorn을 tailnet IP(100.124.118.44)에 직접 바인딩 | 예 | HTTP라 secure context가 아님 → PWA·Service Worker·푸시 불가 | 낮음 | 부적합 |
| C. macOS 방화벽/pf | LAN·tailnet 인바운드만 | 무관 | 낮음 | **보조**(터널에는 무효) |
| D. Tailscale Grants/ACL | 예(노드 간) | 무관 | 낮음(관리 콘솔) | **필수 보조**(A의 경계 자체를 좁힘) |
| E. Cloudflare Access | 예(Cloudflare 엣지, 공개 경로는 유지) | 로그인 흐름과 CORS 상호작용 복잡 | 중간 | 대안(비상 접근용으로만 검토) |
| F. Tailscale Funnel | 아니오(공개) | - | - | **사용 금지**(현재 설정 없음, 확인됨) |

### 4.3 권고 구조: A + D + 터널 허용목록(+ C)

```
소유자 기기(tailnet)  ──WireGuard──▶  Tailscale Serve https://winterholic-macstudio.tail4d9637.ts.net
                                            │  /      → SPA 정적 파일 (같은 origin)
                                            │  /api   → Caddy 내부 리스너 → uvicorn 세대
공유 링크 수신자(인터넷) ──▶ Cloudflare 터널 ttakkari-api.…  ingress: 공개 경로 정규식만 통과, 나머지 404
                              허용: /api/health, /api/version, /api/public/shares/…
```

**A1. 같은 origin에서 SPA도 제공**(권고). Serve 뒤에서 SPA 정적 파일(`dist`)도 같은 호스트로 낸다. 쿠키는 first-party가 되어 iOS 차단 위험이 사라지고, CORS가 필요 없으며, `SameSite=Strict`로 올릴 수 있다. 비용은 (1) 프런트 빌드를 Mac에 올리는 경로(Vercel 대신, 또는 병행), (2) **새 origin이므로 PWA 재설치, 푸시 재구독, localStorage(초안·설정) 초기화**, (3) 공개 공유 페이지 `/share/*`는 Vercel에 남겨 공개로 유지하는 것이다.
**A2.** SPA는 Vercel에 두고 API만 tailnet으로 옮기는 방식은 위 2번 때문에 권고하지 않는다. 실기기에서 쿠키가 실제로 전달되는지 확인된 뒤에만 선택지로 남긴다.
**A3.** `ttakkari-ts.winterholic.net`을 DNS 전용 레코드로 100.124.118.44에 매핑하고 Caddy가 DNS-01 인증서를 받는 방식은 same-site를 유지하지만, xcaddy 빌드와 Cloudflare DNS API 토큰을 Mac에 둬야 해서 비밀이 늘어난다. 비권고.

**신뢰 원칙.** Serve는 `Tailscale-User-Login` 등을 붙일 수 있지만 **앱은 이 헤더를 신뢰하지 않는다**. 경계는 "tailnet에 인증된 노드만 이 포트에 도달한다"는 네트워크 사실이고, 앱 인증(비밀번호·JWT)은 그대로 유지한다. 헤더는 감사 참고용으로만 쓸 수 있다.

**IP 신뢰 경로 분리**(F5, 전환 전에 필요). Serve는 루프백으로 연결하므로 지금 구조에서는 tailnet 클라이언트가 `CF-Connecting-IP`를 위조해 IP 키와 감사 IP를 바꿀 수 있다. Caddy에 리스너를 둘로 나눈다.

- `127.0.0.1:8787`: cloudflared 전용. `CF-Connecting-IP`를 그대로 전달.
- `127.0.0.1:8788`: Tailscale Serve와 내부 호출 전용. `header_up -CF-Connecting-IP`로 지우고 출처 키는 피어(Serve가 붙이는 X-Forwarded-For의 tailnet IP, **Serve의 덮어쓰기 동작은 확인 필요**)를 쓴다.

Caddy 설정은 배포 감독이 생성하므로(`generation_deployment.py:220-226`) 이 변경은 배포 시스템 변경이다. 단위 테스트와 격리 시험이 필요하다. 같은 uid의 에이전트가 8787에 직접 헤더를 붙이는 경우는 막을 수 없다(ADR 0048과 같은 신뢰 모델 한계). 그 경우 영향은 IP 키 오염과 로그 IP 위조로 한정되고, 인증은 우회되지 않는다.

**D. Grants.** 관리 콘솔(사용자 작업)에서 Mac Studio로 들어오는 접근을 줄인다. 예시(적용하지 않음, 현재 정책 확인 후 조정):

```jsonc
"hosts": { "iphone": "100.85.98.103", "macstudio": "100.124.118.44" },
"grants": [
  { "src": ["iphone"], "dst": ["macstudio"], "ip": ["tcp:443"] }
  // 노트북·윈도우 PC가 Mac Studio에 필요한 포트가 있으면 줄별로 추가(확인 필요)
]
```

현재 규칙을 먼저 내보내서 영향 평가를 해야 한다. 윈도우 서버나 ubuntu 서버가 Mac에 접속하는 용도가 있으면 그 규칙을 지우면 깨진다(확인 필요). 같은 계정 소유 기기는 사용자 단위 src로는 폰과 서버를 구분할 수 없으므로 호스트 별칭이나 기기 태그를 쓴다. Mac Studio 노드 키 만료(기본 180일)도 꺼야 한다. 만료되면 내부 입구가 통째로 사라진다.

### 4.4 전환 절차와 롤백

모든 단계는 **다음 단계에서 사용자 승인 후** 진행한다. 이번에는 아무것도 실행하지 않았다.

#### 4.4.1 공개 URL을 직접 호출하는 내부 소비자(검증 지적 반영)

공개 터널을 허용목록으로 줄이면 아래 호출이 영향을 받는다. 코드 줄은 `ede2bf2` 기준이다.

| 소비자 | 호출 | 호출 시점 | 허용목록 반영 |
|---|---|---|---|
| `Slots.verify` (`generation_deployment.py:384-391`) | 로컬 8787과 **공개 URL**의 `/api/health`, `/api/version`(리비전 일치 확인) | 세대 배포마다(`:673`, `:838`, `:893`), 최초 부트스트랩(`generation_bootstrap.py:136`) | `/api/health`, `/api/version`을 허용목록에 **반드시 포함** |
| `Services.verify` (`generation_bootstrap.py:131-157`) | 공개 URL에 소유자 access 토큰(`issue_access_token()`으로 직접 발급)으로 `GET /api/workspaces`, 종료된 Run의 `GET /api/runs/{id}/stream`(Last-Event-ID) | **최초 전환(부트스트랩) 때만**. `requested()`는 `generations-enabled.json`이 있으면 거짓(`generation_bootstrap.py:36-45`)이라 일반 세대 배포에서는 돌지 않는다. 다만 `bootstrap-failed.json`·`restore_legacy` 경로로 되돌린 뒤 다시 부트스트랩하면 다시 돈다 | 허용목록에 넣지 않는다(소유자 API). 대신 base URL을 설정값으로 바꾸거나(`TTK_PUBLIC_URL`처럼), 부트스트랩 재실행은 "허용목록을 먼저 되돌린다"를 runbook에 명시. 현재 생성 세대가 운영 중이므로 일상 배포에는 영향 없음 |
| `public_ready` (`deployment.py:287-296`) | `TTK_PUBLIC_URL`(기본 공개 URL)의 `/api/health`, `/api/version` | 레거시 idle 배포 경로 | 허용목록에 포함된 경로만 호출 |
| 프런트 PWA 푸시 | `push-sw.js`에 API 호출 없음(grep 0건). Web Push는 서버가 푸시 서비스로 보내는 아웃바운드 | - | 공개 경로 불필요 |

이 목록은 4.4의 P4 전에 **로컬 확인 항목**이다. P4 이후 첫 일반 세대 배포가 `Slots.verify`를 통과하는지를 완료 기준에 넣는다.

#### 4.4.2 단계

| 단계 | 내용 | 검증 | 롤백 |
|---|---|---|---|
| P0 사전 | 관리 콘솔에서 HTTPS 인증서 활성, 현재 ACL 내보내기, Mac Studio 키 만료 해제. `~/.cloudflared/ttakkari.yml` 백업. 4.4.1 소비자 표를 코드로 재확인 | `tailscale status --json`에 `CertDomains` 표시 | 없음(설정 읽기) |
| P1 프록시 이중 리스너 | 8788 추가(헤더 제거). 8787은 그대로 | 배포 감독 검증(`Slots.verify`) 통과, `lsof`에 8788 루프백 | 이전 `proxy_config` 복귀 배포 |
| P2 Serve 병행 | `tailscale serve --bg --https=443 http://127.0.0.1:8788` 형태. 공개 터널은 그대로 유지 | iPhone(Wi-Fi 끄고 셀룰러 + Tailscale 켬)에서 `…ts.net/api/health`, 로그인, SSE, 푸시. Serve가 클라이언트의 `X-Forwarded-For`·`Tailscale-User-*`를 덮어쓰는지, `CF-Connecting-IP`가 8788에서 제거되는지 측정(미검증 항목 6 해소) | `tailscale serve reset`(명령 하나, 즉시) |
| P3 SPA same-origin | 프런트 빌드를 Serve 경로로 제공, PWA 재설치, 푸시 재구독, 7일 병행 사용 | 앱 재실행 시 재로그인 없이 복구(iOS 쿠키 실측, 미검증 항목 9 해소), 푸시 수신, 원격 데스크톱 연결 | 기존 Vercel PWA로 복귀(공개 도메인이 아직 열려 있으므로 가능) |
| P3.5 내부 소비자 정리 | 부트스트랩 스모크 base URL 설정화 또는 runbook 확정(4.4.1). 격리 터널에서 `path` 매칭 시험 | 허용목록 후보 정규식이 `/api/health`, `/api/version`, `/api/public/shares/*`만 통과하고 `/api/workspaces`는 404임을 격리에서 확인 | 설정 변경 없음 |
| P4 터널 허용목록 | `ttakkari.yml`에 `path` 정규식 추가(아래), 터널 재시작 | 공개 도메인에서 `/api/workspaces` 404, `/api/health`·`/api/version`·공유 링크 정상, **다음 일반 세대 배포가 `Slots.verify`를 통과** | 백업 yml 복원 + 터널 서비스 재시작. 부트스트랩 재실행이 필요한 상황이면 먼저 이 롤백 |
| P5 보강 | Grants 최소화, `3306` 루프백 바인딩(사용자 결정), 앱 방화벽 | 외부 노드에서 3306 연결 거절 | 콘솔에서 정책 복원 |

P4 규칙 예시(미적용, 격리 터널에서 `path` 매칭 동작을 먼저 확인해야 함, **확인 필요**):

```yaml
ingress:
  - hostname: ttakkari-api.winterholic.net
    path: ^/api/(health|version|public/shares(/.*)?)$
    service: http://127.0.0.1:8787
    originRequest: { keepAliveTimeout: 90s }
  - service: http_status:404
```

주의: (1) 모든 구 PWA 클라이언트(공개 도메인 사용 중)는 P4 이후 동작하지 않는다. P3 완료 전에 P4를 하면 앱이 잠긴다. (2) 전환 중에도 Mac 로컬 콘솔과 노트북(tailnet)이 비상 경로다. (3) 미검증으로 남은 것: Serve의 헤더 덮어쓰기 동작(P2에서 측정), iOS cross-site 쿠키 차단(P3에서 실기기 측정), 다른 기기에서 3306 접속 가능 여부(P5 전후 측정). 이 문서는 이 셋을 확인된 사실로 쓰지 않는다.

### 4.5 영향

- **모바일 PWA**: iPhone에 Tailscale이 켜져 있어야 앱이 열린다. 꺼져 있으면 접속 불가(의도된 fail-closed). 상시 연결 또는 온디맨드 설정은 확인 필요. 배터리 영향은 실측 필요.
- **외부 webhook**: 없음. 영향 없다.
- **공개 공유 링크**: 영향 없음(허용 경로에 포함, SPA `/share/*`는 Vercel 유지).
- **iPhone 단축어 업로드**: 단축어가 tailnet 주소를 쓰도록 바꾸고 Tailscale 연결 상태에서만 동작한다(실측 필요).
- **에이전트 Run**: 엔진은 루프백으로 MCP를 호출하므로 영향 없다. 네트워크 경계 변경은 Run 실행·승인 흐름을 건드리지 않는다.
- **세대 배포**: 검증이 `/api/health`, `/api/version` 공개 호출에 의존하므로 허용목록에 반드시 포함한다.
- **가용성**: Tailscale 데몬·키 문제가 곧 접속 불가다. 백엔드 자체가 로그인 세션(`gui/501`) 아래 launchd로 도는 것과 같은 급의 의존이다. 부팅 후 로그인 전 상태에서 Tailscale 확장이 올라오는지는 확인 필요.

### 4.6 사용자 승인·작업이 필요한 항목

1. 방식 결정: A1(같은 origin, PWA 재설치 감수) 채택 여부.
2. 관리 콘솔 작업(사용자만 가능): HTTPS 인증서 활성, ACL 확인·변경, Mac Studio 키 만료 해제.
3. 공개 터널 축소(P4)의 시점.
4. 비상 접근 경로를 둘지(예: Cloudflare Access로 보호한 별도 호스트). 두면 공개 표면이 하나 남는다.
5. 따까리 밖 설정: `mariadbd` 바인딩, macOS 앱 방화벽 활성(수신 허용 프롬프트가 생길 수 있음).

## 5. 로그인 실패 제한 설계

### 5.1 현재 구현 (확인됨)

| 항목 | 현황 | 근거 |
|---|---|---|
| 저장소 | **프로세스 메모리**(`dict[ip, list[time]]`). 재시작·배포·세대 전환마다 초기화. 세대가 여럿이면 프로세스별로 따로 센다 | `security/auth.py:99-110,193-207` |
| 키 | IP만. 계정 단위 카운터 없음 | `auth.py:154-164` |
| 정책 | 5회 실패 → 15분 잠금(고정), 15분 지나면 한꺼번에 해제 | `config.py:420-421` |
| 전역 제동 | 토큰 버킷(버스트 8, 초당 1회 검증), 동시 검증 1, 대기 4, 대기시간 1초 | `config.py:422-426`, `auth.py:123-191` |
| 출처 IP | 루프백 피어면 `CF-Connecting-IP` | `auth.py:79-88` |
| 병렬 요청 | 같은 프로세스 안에서는 세마포어와 이중 `check`로 막음. 프로세스 간·재시작 간에는 막지 못함 | `auth.py:166-191` |
| 성공 시 | 해당 IP 실패 기록 삭제, 버킷 환불 | `auth.py:209-211` |
| 원격 데스크톱 재인증 | 같은 IP 카운터를 공유하지만 `admission`(동시성·버킷)은 거치지 않아 Argon2 검증이 병렬 제한 없이 돈다(유효한 소유자 JWT가 있어야 도달) | `api/remote_desktop.py:43-54` |
| 감사 | 실패 검증만 `auth.login denied`로 기록. 429로 막힌 요청은 기록 없음 | `api/auth.py:88-92` |
| 사용자 존재 | 계정 식별자 입력이 없고 비밀번호 하나. 미설정 시 503 메시지가 다름 | `api/auth.py:81-86` |
| 비밀번호 정책 | 최소 12자, Argon2 기본 매개변수 | `security/passwords.py:8-14` |

문제: (1) 메모리라 배포가 잦은 이 시스템(하루 여러 번)에서 카운터가 수시로 지워진다. (2) IP를 바꿔 가며(IPv6 /64 포함) 오는 분산 추측을 계정 단위로 막지 못한다. 전역 버킷이 평균 초당 1회로 상한은 걸지만 하루 약 8만 회 추측이 가능하다. (3) 같은 전역 버킷이 **정상 소유자의 로그인까지 429로 만들 수 있다**(공격자가 초당 1회 이상 보내면 버킷이 계속 비어 있음). 영구 차단은 아니지만 공격자가 계속 보내는 동안은 로그인할 수 없다.

### 5.2 요구와 불변식

요구: 5회 실패 후 1분, 이후 5분, 15분, 최대 60분 단계 지연. 계정과 출처를 모두 고려. 재시작 후 유지(DB). 병렬 요청 레이스 방지. 사용자 존재 비노출. Mac Studio 로컬 관리자 전용 해제(일반 API 미노출). 공격자에 의한 영구 차단 방지. 실행 중 에이전트 Run 무영향.

초판 설계(계정 키 잠금 + 성공 시 키 전체 초기화)에는 독립 검증이 지적한 결함이 있었다.

| 결함 | 초판의 문제 |
|---|---|
| 잠금 지속 | 계정 키를 잠그면 공격자가 해제 직후 실패 1회를 반복해 단계를 계속 올리고, 쿠키 없는 새 기기의 정상 로그인이 계속 막힌다 |
| 예산 누락 | 정답 요청이 키를 통째로 초기화하면 같은 순간의 오답 요청이 쌓은 소비가 지워진다. 예약 ID가 없어 어느 소비가 누구의 것인지 알 수 없다 |
| 예외 혼재 | 신뢰 기기 쿠키가 계정 잠금과 CPU 제한을 한꺼번에 면제했다. 쿠키 도난 시 CPU 방어가 사라지고, 미신뢰 폭주가 신뢰 요청의 CPU를 굶긴다 |
| 확장 | IP 회전, 출처 행 정리(TTL), 원격 재인증 키, 429·잠금 감사가 빠져 있었다 |

개정 설계는 아래 다섯 불변식을 만족해야 한다. 테스트로 고정한다(5.9절).

1. **해제는 시계로만 결정된다.** 잠금 해제 시각은 단계 길이로 정해지고, 잠금 중 도착한 요청은 어떤 결정 상태도 바꾸지 않는다(검증하지 않고 거절, 단계 상승·시간 연장 없음). 공격자의 요청 수가 해제를 늦추지 못한다.
2. **계정 단위는 잠금이 아니라 속도 제한이다.** 계정 전체를 막는 상태가 존재하지 않는다. 미신뢰 경로 전체가 쓰는 시간당 시도 예산(토큰 버킷)만 있다.
3. **신뢰 레인은 미신뢰 레인이 소비할 수 없는 별도 예산을 쓴다.**
4. **예산은 검증 전에 원자적으로 예약하고, 정답은 자기 예약분만 환불한다.** 다른 요청의 소비를 지우지 않는다.
5. **상태 행 수와 감사 행 수는 공격 요청 수가 아니라 정책 상한에 묶인다.**

정책 해석(사용자 확정 필요): **출처·기기 키당 5회 시도 → 1분 잠금 → 해제 후 1회 탐침 → 실패하면 5분 → 1회 탐침 → 15분 → 1회 탐침 → 60분(상한, 이후 반복)**. 단계마다 5회씩 주는 해석은 키당 하루 추측 가능 수가 5배가 되므로 권고하지 않는다. 키당 하루 최대 시도는 5 + 24 = 29회(60분 상한 도달 후)다. 24시간 무실패면 처음 단계로 돌아간다.

### 5.3 대안 비교(잠금 지속 문제)

| 안 | 내용 | 장점 | 문제 | 판정 |
|---|---|---|---|---|
| A | 계정 키 잠금을 모든 미신뢰 요청에 적용(초판) | 분산 추측에 강함 | 공격자가 해제 직후 실패 1회로 재잠금을 반복 → 새 기기 정상 로그인 지속 차단(검증 지적) | **기각** |
| B | 출처(IP) 키만, 계정 제동 없음 | 정상 사용자 영향 최소 | IP 회전 시 상한이 전역 CPU 버킷뿐이고 그 버킷이 소유자 로그인도 막는다(현재 문제) | 불충분 |
| C | 출처·기기 키는 단계 잠금, **계정 단위는 미신뢰 레인 전체의 속도 제한**, 레인 분리 | 영구 차단 불가(불변식 1·2), 분산 추측 총량 상한, 신뢰 경로는 공격 중에도 동작 | 미신뢰 새 기기는 공격 중 대기 가능(최대 한 refill 주기) | **권고** |
| D | C + **새 기기 등록 코드**(Mac CLI가 1회용 코드를 발급, 비밀번호와 함께 입력) | 공격 중에도 새 기기 즉시 등록 | 새 기기 로그인 UX에 코드 입력 추가(공격 경보 중에만 요구하도록 제한 가능) | C의 보조, 사용자 승인 필요(LGN-05) |
| E | TOTP 2차 인증 | 가장 강함 | ADR 0004 Phase 2 후보, 로그인 UX 변경 | 이번 범위 밖, 후속 |
| F | 최근 로그인 성공 IP를 신뢰 | 쿠키 없는 기기 구제 | 모바일 IP 변동·CGNAT 공유로 오탐·오허용 | 기각 |

**권고: C, 그리고 D를 선택 기능으로.** Tailscale 경계(4절) 이후에는 tailnet 출처를 신뢰 레인으로 편입한다. 그러면 외부 공격이 미신뢰 레인에만 갇히고 소유자의 정상 경로는 공격과 무관해진다.

### 5.4 레인과 예외 분리

| 레인 | 진입 조건 | 적용 키 | 잠금·지연 | CPU 토큰 버킷 |
|---|---|---|---|---|
| D 신뢰 기기 | 유효 `ttk_dev` 쿠키(서버 발급 256비트, DB에는 SHA-256) | `dev:<id>` | 기기 키 단계 잠금. **출처 잠금·계정 버킷은 면제** | T 전용(시간당 30회 상한) |
| I 등록 코드(선택) | 유효한 1회용 코드(10분, 코드당 3회 시도) | `inv:<id>` | 코드 키 소진 시 폐기. 계정 버킷 면제 | T 전용 |
| N tailnet(4절 완료 후) | 8788 리스너 경유(tailnet 노드) | `src:ts:<노드>` | 단계 잠금(미신뢰와 별도 예산) | T 전용 |
| U 미신뢰 | 위 어느 것도 아님(잘못된 쿠키 포함) | `src:<ip 또는 IPv6 /64>` | 출처 단계 잠금 + **`acct:u` 속도 제한**(버킷 크기 6, 10분당 1개 refill) | U 전용 |

CPU와 잠금 예외를 분리한다(검증 지적 c).

| 제한 | 신뢰 기기 쿠키 | 이유 |
|---|---|---|
| 계정 속도 제한(`acct:u`) | **면제** | 쿠키는 서버가 발급한 난수라 위조할 수 없다 |
| 출처 단계 잠금(`src`) | **면제**, 대신 `dev` 키 | 출처 오염(루프백 위조, CGNAT 공유)의 피해를 막는다 |
| CPU 동시 검증 상한(세마포어) | **면제 없음** | 프로세스 보호는 모든 레인에 적용 |
| CPU 토큰 버킷 | **분리**(T와 U 따로) | U 폭주가 T를 굶기지 못하고, T도 시간당 상한이 있어 쿠키 도난 시 CPU 악용이 제한된다 |

쿠키 위조·도난 반례: 위조는 불가능하다(난수 비교, 없거나 형식 불량·폐기된 쿠키는 레인 U로 처리). 도난 쿠키는 **비밀번호가 있어야** 로그인되고, 얻는 것은 자기 `dev` 키의 하루 29회 추측뿐이다. 비밀번호 변경과 `logout-all`은 `login_devices`를 전부 폐기한다. 쿠키는 기기당 하나, 최대 8개다.

### 5.5 원자 예약과 환불(검증 지적 b)

저장 구조(신규 표 3개, 모두 online-safe):

```sql
CREATE TABLE login_throttle (               -- 출처·기기·원격 재인증 키별 단계 상태
  key text PRIMARY KEY,                     -- 'src:203.0.113.7' | 'src6:2001:db8:1:2::/64' | 'dev:<uuid>' | 'inv:<uuid>' | 'rd:owner'
  budget smallint NOT NULL,                 -- 다음 잠금까지 남은 시도
  level smallint NOT NULL DEFAULT 0,        -- 0..4
  locked_until timestamptz,
  stage_started_at timestamptz NOT NULL,
  last_failure_at timestamptz,
  lock_trigger uuid,                        -- 이 잠금을 일으킨 예약 ID
  updated_at timestamptz NOT NULL
);
CREATE TABLE login_bucket (                 -- 시간 기반 refill, 잠금 상태 없음
  key text PRIMARY KEY,                     -- 'acct:u' | 'cpu:u' | 'cpu:t'
  tokens real NOT NULL, refilled_at timestamptz NOT NULL
);
CREATE TABLE login_attempts (               -- 예약 ID와 소비 기록(24시간 보관)
  id uuid PRIMARY KEY, lane text NOT NULL, keys text[] NOT NULL,
  reserved_at timestamptz NOT NULL,
  outcome text NOT NULL DEFAULT 'pending'   -- pending | ok | fail | abandoned
);
CREATE TABLE login_devices (                -- 신뢰 기기(해시만)
  id uuid PRIMARY KEY, token_hash char(64) NOT NULL UNIQUE,
  created_at timestamptz NOT NULL, last_seen_at timestamptz NOT NULL,
  revoked_at timestamptz, user_agent varchar(512)
);
```

**예약(검증 전, 트랜잭션 하나).** 키 행을 `SELECT … FOR UPDATE`로 항상 같은 순서(`dev|inv|src` → `acct:u` → `cpu:*`)로 잠가 교착을 막는다. 행이 없으면 `INSERT … ON CONFLICT DO NOTHING` 후 잠근다.

1. 24시간 이상 실패가 없으면 `level=0`, `budget=5`.
2. `locked_until > now`이면 거절(Retry-After). **어떤 상태도 쓰지 않는다**(불변식 1). 차단 횟수는 프로세스 메모리 카운터에만 쌓는다.
3. 레인 U면 `acct:u` 버킷을 시계로 refill하고 토큰이 1 미만이면 거절. 아니면 1 소비.
4. CPU 버킷(U 또는 T)도 같은 방식. 동시 검증 세마포어는 트랜잭션 밖의 프로세스 상한.
5. `budget == 0`이면(잠금이 막 끝남) `budget = 1`(탐침). 이어서 `budget -= 1`. 0이 되면 `level = min(level+1, 4)`, `locked_until = now + (60, 300, 900, 3600)초[level-1]`, `lock_trigger = 이 예약 ID`, `stage_started_at = now`. **이 요청은 진행**하고 같은 순간 도착한 다른 요청은 2번에서 거절된다.
6. `login_attempts`에 `pending` 행을 넣고 커밋한 뒤 Argon2 검증은 락 밖에서 한다.

**확정(검증 후).**

- **실패**: 행의 `outcome='fail'`, `last_failure_at=now`. 카운터는 이미 예약 때 소비됐으므로 더 건드리지 않는다.
- **성공**: `outcome='ok'`. 이 예약이 소비한 것만 환불한다(`budget = min(budget+1, 5)`, 버킷 토큰 +1). `lock_trigger`가 자기 자신이면 그 잠금을 풀고 `level -= 1`. **전체 초기화(`level=0`, `budget=5`)는 이 키에 `stage_started_at` 이후 다른 `pending`·`fail` 행이 하나도 없을 때만** 한다. 있으면 자기 몫 환불만 한다.
- **고아 예약**: 연결이 끊겨 2분 넘게 `pending`인 행은 정리 작업이 `fail`로 바꾼다(보수적).

레이스가 안전한 이유: 소비는 항상 예약 시점에 행 잠금 아래에서 일어나므로, 20개 병렬 오답은 예산만큼만 검증까지 간다. 정답 1개와 오답 19개가 섞이면 정답은 자기 몫만 환불하고 오답들의 소비는 남는다. 정답과 오답의 완료 순서는 결과에 영향이 없다. 대안으로 `pg_advisory_xact_lock(hashtext(key))` 아래 `login_attempts`를 창으로 세는 방식도 안전하지만 단계(level) 상태를 따로 유지해야 해서 상태 행 방식을 택했다.

### 5.6 IP 회전, 행 정리, 원격 재인증, 감사(검증 지적 d)

- **IP 회전**: IPv6는 /64를 한 키로 본다. IPv4·IPv6 모두 주소를 바꿔 오는 공격은 출처 키로는 못 막으므로 **`acct:u` 버킷이 미신뢰 레인 전체의 총량을 묶는다**(10분당 1회 refill, 하루 최대 약 144회 + 초기 6회). 이 총량은 키 개수와 무관하다.
- **출처 행 TTL**: `level=0`이고 `updated_at`이 7일 지난 행, `outcome` 24시간 지난 `login_attempts`를 기존 retention 루프에서 지운다. 출처 행이 2만 개를 넘으면 가장 오래된 `level=0` 행부터 지우고, 그래도 넘치면 새 출처를 공용 키 `src:overflow` 하나로 합쳐 알린다. 행 수는 `acct:u` 버킷의 refill 속도에 묶이므로 폭증하지 않는다(불변식 5).
- **원격 재인증**: `rd:owner` 키를 따로 쓴다(출처 단계 잠금과 같은 규칙, 같은 CPU 세마포어). 유효한 JWT가 이미 있으므로 `acct:u`는 소비하지 않는다. 도난 JWT로 비밀번호를 추측하는 경로가 로그인 레인과 예산을 공유하지 않고, 반대로 로그인 잠금이 원격 데스크톱을 막지도 않는다.
- **감사·알림(검증 지적 d)**: 요청마다 쓰지 않는다. (1) 잠금 시작마다 `auth.login_locked`(단계, 키 종류, IP 접두사) 1행, (2) 잠금 종료 시점에 메모리 차단 카운터를 모아 `auth.login_lock_summary` 1행, (3) `acct:u` 버킷이 비는 에피소드마다 최대 10분에 1행. 단계 2 이상이거나 버킷 에피소드면 푸시 1회(30분 중복 억제). 429 폭주 1000건이 감사 3행 이하여야 한다.
- **사용자 존재 비노출**: 입력에 계정 식별자가 없어 열거 대상이 없다. 그래도 (1) 잠금 응답은 어느 키가 잠겼는지 밝히지 않는 같은 429 본문과 `Retry-After`만 쓴다. (2) `Credential`이 없을 때도 더미 해시로 검증을 1회 수행해 시간을 맞춘다. (3) "비밀번호가 설정되지 않았습니다" 503은 최초 설정 전에만 나온다.
- **Mac 로컬 관리자 전용 해제**: HTTP 라우트는 만들지 않는다. `ttakkari login-status`, `login-unlock [--key K | --all]`, `login-invite`(선택)를 `set-password`(`cli.py:67-80`)와 같은 DB 직접 접속 방식으로 추가한다. `set-password`는 잠금·신뢰 기기·초대 코드도 초기화한다. 라우트 인벤토리 테스트가 해제·초대 라우트의 부재를 고정한다. 같은 uid의 에이전트도 CLI를 실행할 수 있지만 해제는 인증을 면제하지 않는다. `set-password`로 비밀번호를 바꿀 수 있는 현재 신뢰 수준(ADR 0048)보다 약한 권한이라 새 위험이 아니다.
- **에이전트 Run 무영향**: 이 로직이 닿는 곳은 `POST /api/auth/login`과 원격 데스크톱 재인증뿐이다. 에이전트 JWT(`aud=ttakkari-agent`), 루프백 MCP 호출, SSE, refresh, 진행 중인 소유자 세션은 이 상태를 읽지 않는다. 대조 테스트로 고정한다.

### 5.7 구현 위치

| 파일 | 변경 |
|---|---|
| `src/ttakkari/security/login_throttle.py`(신규) | 레인 판정, 예약·확정·환불, 버킷, 키 계산(`/64`), 신뢰 기기 검증, 고아 정리 |
| `src/ttakkari/security/auth.py` | `LoginLimiter`를 CPU 보호(세마포어·메모리 카운터)로 축소. IP별 실패 목록 제거는 S3 이후 |
| `src/ttakkari/api/auth.py` | `login`을 예약 → 검증 → 확정으로, `ttk_dev` 발급(login·refresh), 더미 검증, 잠금 응답 통일 |
| `src/ttakkari/api/remote_desktop.py` | `reauthenticate`를 `rd:owner` 예약으로 교체 |
| `src/ttakkari/models.py`, `alembic/versions/` | 위 5개 표(구현 시점 head 다음 번호) |
| `src/ttakkari/config.py` | 단계 길이, 예산(5), 버킷 크기·refill, 레인 상한, 고아 시간. **모드는 DB `app_settings`**(재배포 없이 전환) |
| `src/ttakkari/cli.py` | `login-status`, `login-unlock`, `login-invite`, `set-password` 초기화 |
| 기존 retention 루프 | 유휴 행·오래된 시도 행 정리, 고아 예약 정리 |

### 5.8 shadow → enforce 단계 전환(검증 지적 e)

모드는 `app_settings['login_throttle_mode']`에 두고 5초 캐시로 읽는다(재시작·재배포 없이 전환, 롤백도 같은 방법). 전환은 S2·S3에서 사용자 승인 후 한다.

| 단계 | 모드 | 동작 | 종료 조건(다음 단계로 가는 조건) | 롤백 |
|---|---|---|---|---|
| S0 | `off` | 스키마·코드만 배포. 기존 메모리 제한이 그대로 enforce | 마이그레이션 up/down, 전체 테스트, 세대 배포 검증 통과 | 코드 되돌림(스키마는 무해) |
| S1 | `shadow` 7일 | 새 로직이 예약·판정하되 **거절하지 않고** `would_block`만 기록. 기존 제한 계속 enforce | (a) 정상 소유자 로그인의 `would_block` 0건(성공 로그인과 대조하는 SQL), (b) 불변식 점검 SQL: 음수 budget 0건, 고아 `pending` 0건, (c) 병렬·재시작 시험 통과, (d) CPU 레인 지연 p95 변화 없음 | `off` |
| S2 | `enforce_source` 3일 | `dev`·`src`·`rd` 키만 enforce, `acct:u`는 shadow. 기존 제한 병행 | 소유자 오차단 0건, `acct:u` 가상 차단 기록이 실제 공격 흔적과 일치하는지 검토 | `shadow` |
| S3 | `enforce` | 전체 enforce, 기존 IP 목록은 CPU 보호만 남김. 7일 관찰 후 기존 IP 목록 제거 | 오차단 0건, 에이전트 API 401 증가 0 | `shadow` + `login-unlock --all` |

각 단계의 공통 확인: 에이전트 Run 관련 401·429 증가 없음, 로그인 성공률 변화 없음, 푸시 알림 중복 억제 동작.

### 5.9 테스트 계획

`tests/test_login_throttle.py`(시간 주입 단위·DB 테스트, 격리 DB `ttakkari_test`)와 `tests/test_login_throttle_concurrency.py`. 모든 테스트는 수정 전 RED, 수정 후 GREEN.

단계와 상태:
1. 5회 실패 후 6번째 429(`Retry-After` 약 60). 61초 뒤 탐침 1회, 실패하면 300, 900, 3600, 이후 3600 유지. 24시간 무실패면 초기화.
2. 성공은 자기 예약만 환불. 조건이 맞을 때만 전체 초기화.

병렬·원자성(검증 지적 b):
3. 오답 20개 병렬 → `verify_password` 호출이 정확히 예산만큼.
4. **정답 1 + 오답 19 병렬** → 정답은 성공하고 오답 소비는 보존된다(`budget` 값 검증). 순서를 바꿔 100회 반복(셔플).
5. 정답이 5번째 예약(잠금 트리거)인 경우 잠금이 풀리고 `level`이 되돌아간다. 트리거가 아닌 정답은 잠금을 풀지 못한다.
6. 연결 끊김(고아 예약) → 2분 뒤 `fail`로 정리.

재시작·다중 프로세스:
7. 새 엔진·새 모듈 인스턴스로도 잠금 유지. 두 개의 세대 프로세스가 같은 상태를 봄.

잠금 악용 반례(검증 지적 a):
8. 공격자가 해제 직후마다 실패 1회씩 24시간 반복(시간 주입) → **신뢰 쿠키 로그인은 항상 성공**.
9. 같은 상황에서 쿠키 없는 새 기기는 `acct:u` 토큰이 생긴 직후 도착하면 성공한다(결정적 순서 시나리오). 공격이 멈춘 뒤 단계 길이 안에 복구된다(영구 아님).
10. 잠금 중 요청 1000건 → `level`·`locked_until` 불변, 감사 행 3개 이하.

IP 회전·정리(검증 지적 d):
11. 서로 다른 IP 200개, IPv6 /64 내 주소 회전 → 총 검증 수가 `acct:u` 버킷 이하.
12. 출처 행 TTL 정리, 2만 개 상한과 `src:overflow` 동작.
13. 원격 재인증이 `rd:owner`만 소비하고 로그인 레인에 영향이 없다. 반대 방향도 같다.

쿠키·CPU 분리(검증 지적 c):
14. 쿠키 위조·형식 불량·폐기·비밀번호 변경 후 → 레인 U 처리. 도난 쿠키는 `dev` 키 잠금을 받는다.
15. 레인 U 폭주 중 레인 T 요청의 지연이 허용 범위 안. 동시 검증 세마포어는 두 레인 모두에 적용.

기타:
16. 사용자 존재 비노출: 잠금·실패 응답 본문 동일, 정답/오답 유무로 키 종류가 드러나지 않음, `Credential` 없음 시 시간 차이 허용 범위.
17. shadow 모드는 거절 0건·`would_block` 기록, 모드 전환이 5초 안에 반영.
18. 에이전트 무영향: 출처·계정 모두 잠긴 상태에서 에이전트 토큰의 `/api/agent/*`, 소유자 세션의 SSE·refresh가 200.
19. 로컬 해제 CLI, 인벤토리 테스트로 해제·초대 라우트 부재.
20. 등록 코드: 발급, 1회 사용, 만료, 코드당 3회 시도.
21. 마이그레이션 up/down. 기존 `test_password_session_revocation.py`의 limiter 테스트를 새 구현에 맞게 갱신.

## 6. 세션 보안 감사

범위: 소유자 세션(access JWT + refresh 쿠키). 에이전트 세션 토큰(`aud=ttakkari-agent`)은 SEC2-AGT-01/02에서 다룬다.

현재 구조: access JWT(HS256, 15분, 프런트 메모리) + refresh(불투명 난수, DB에 SHA-256, 30일 슬라이딩, 회전·재사용 탐지, HttpOnly 쿠키 `Path=/api/auth`). 운영 DB 현황(집계만 조회): refresh 행 613개, 활성(미폐기·미만료) 12개가 서로 다른 12개 계열, 만료 행 3개, 감사 로그 8379행, 가장 오래된 refresh 행 2026-10-09.

| # | 항목 | 현황과 근거 | 위험 | 개선안 |
|---|---|---|---|---|
| 1 | 세션 ID 예측불가성 | refresh는 `secrets.token_urlsafe(48)`(384비트), DB에는 해시(`security/auth.py:49-55`). access `jti`는 `uuid4`. **양호** | 낮음 | 유지 |
| 2 | 로그인 시 재발급·세션 고정 | `login`은 요청 쿠키를 읽지 않고 항상 새 계열·새 토큰을 발급(`api/auth.py:53-64,101`). 로그인 전 세션이 없어 고정 공격 면이 없다. 다만 재로그인해도 이전 계열을 폐기하지 않아 활성 계열이 12개로 쌓임 | 낮음 | 로그인 시 같은 기기 쿠키의 이전 계열 폐기(SES-02) |
| 3 | 쿠키 속성 | 운영 응답으로 확인: `HttpOnly; Secure; SameSite=none; Path=/api/auth`, Domain 없음(호스트 한정)(`api/auth.py:33-44`, 가짜 쿠키 refresh의 삭제 응답 헤더) | 낮음 | PWA와 API가 같은 사이트이므로 `SameSite=Strict`로 올릴 수 있음. `__Secure-` 접두사 검토. 4.3의 origin 이전과 함께 결정(F7, SES-05) |
| 4 | CSRF | refresh·logout은 Origin 허용목록과 `Sec-Fetch-Site`로 검사(`api/auth.py:67-73`). 나머지는 Bearer라 CSRF 면이 없음. 운영 preflight 확인: 허용 origin만 통과(400/200). Origin·Sec-Fetch-Site가 둘 다 없는 비브라우저 요청은 통과하나 쿠키가 있어야 의미가 있음 | 낮음 | `cors_origins`에 origin을 추가하면 CSRF 허용도 같이 커지므로 추가 시 리뷰(SEC2-AUTH-05) |
| 5 | XSS 경유 유출 | access는 메모리(`front/src/api/client.ts:74`), refresh는 HttpOnly라 직접 읽기는 어렵다. 그러나 XSS는 같은 origin에서 `/api/auth/refresh`를 호출해 access를 얻을 수 있다(`client.ts:111-125`). SPA 응답에 CSP·X-Frame-Options 없음(HSTS만, 응답 헤더 확인) | 중간(2×3) | 기존 SEC2-WEB-01·02. 이 문서에서 새로 만들지 않음 |
| 6 | 로그아웃 무효화 | logout은 해당 계열 전체 폐기(`api/auth.py:147-156`), logout-all은 전체 폐기(`159-164`)로 **refresh는 즉시 무효**. access JWT는 최대 15분 유효, SSE·원격 데스크톱 연결은 계속됨(8, 9번) | 낮음 | SES-02(sid 확인) |
| 7 | 서버측 폐기 | refresh는 DB 조회로 폐기 가능(양호). access는 stateless(`security/auth.py:58-71`). **개별 기기 목록·폐기 API 없음**. 전체 폐기뿐이라 의심 세션만 끊을 수 없다. `RefreshToken.replaced_by` 컬럼은 정의만 있고 쓰이지 않음(`models.py:138`) | 중간(1×2) | SES-02: access에 `sid`(계열 ID) 클레임, 요청 시 짧은 TTL 캐시로 계열 활성 확인, 세션 목록·개별 폐기 API |
| 8 | 서버 만료·비활성 만료 | 비활성 만료는 마지막 refresh 기준 30일 슬라이딩(`api/auth.py:57`). **절대 상한 없음**: 쿠키가 도난되어도 사용되는 동안은 무기한(단, 원 소유자가 사용하면 재사용 탐지로 끊김). 만료·폐기 행 정리 코드 없음(`grep RefreshToken`, 현재 규모는 작음) | 낮음(1×2) | SES-03: 계열 절대 상한 + 정리 |
| 9 | 장기 로그인 방식 | 30일 슬라이딩 + 회전이라 자주 쓰면 사실상 영구 로그인. 요구("반복 로그인 강제 금지")에 부합 | - | 절대 상한을 두되 길게(권고 180일, 90일 대안). **짧은 만료로 바꾸지 않는다** |
| 10 | WebSocket 인증·만료 | 소켓은 원격 데스크톱 1개. Origin 허용목록, 일회 티켓(30초), grant(10분, 비밀번호 재인증), 동시 2개, 연결 후 2시간 상한(`api/remote_desktop.py:119-142`, `remote_desktop.py:56,105`). 연결 중에는 비밀번호 변경·로그아웃을 확인하지 않음(기존 S6) | 낮음(1×3→기존 AUTH-01) | SES-04: 주기 재검증 |
| 10b | SSE | `/api/runs/{id}/stream`은 연결 시점에만 `require_user`(`api/sessions.py:283-324`). 만료·로그아웃 뒤에도 Run이 끝날 때까지 스트림이 열려 있다 | 낮음(1×2) | SES-04: 60초 단위 재검증으로 종료(클라이언트는 Last-Event-ID로 재연결) |
| 11 | 탈취 세션 재사용 | refresh 회전·재사용 탐지로 감지(`api/auth.py:133-140`). 단, **계열 폐기 사건이 13건**이고 원인이 동시 갱신인지 도난인지 구분되지 않는다(아래 6.1). 기기 바인딩·이상 출처 알림 없음. UA·IP는 저장(`api/auth.py:53-64`)하나 검증하지 않음 | 중간(2×2) | SES-01(오탐 해소), SES-06(새 기기 로그인·재사용 알림) |
| 12 | 브라우저·PWA 일관성 | access는 메모리라 앱을 열 때마다 refresh 호출(`front/src/auth.tsx:17-25`). 탭이 둘 이상이거나 PWA와 브라우저가 겹치면 refresh가 동시에 나감(탭 안에서는 `refreshInFlight`로 합쳐지나 탭 사이는 아님, `client.ts:111-125`). Service Worker는 `/api/*`를 NetworkOnly(`vite.config.ts:41-44`)라 토큰 캐시 없음 | 중간(F3) | SES-01 |

### 6.1 F3 상세: refresh 재사용으로 계열이 폐기된 사건(검증 지적 2 반영)

**사실(확인됨, 운영 DB 읽기 전용 조회, IP는 해시 앞자리로만 구분).**

- `auth.refresh_reuse` 13건(10-13부터 10-19). **IP는 2개**(12건 + 1건). 두 IP 모두 성공한 `auth.login` 기록에 나오는 IP다. 초판의 "같은 IP 1개"는 하루 단위 집계를 전체로 잘못 읽은 것이다.
- 폐기된 계열의 User-Agent: Mac Chrome 3계열, iPhone 2계열(iOS 18.7·18.5 Safari), Windows Chrome 3계열. 즉 소유자가 쓰는 브라우저들이다. 다만 2건은 한 계열 안에 iOS와 Mac UA가 섞여 있다(데스크톱 사이트 요청으로 같은 기기가 Macintosh UA를 보냈을 가능성이 있으나 **확인하지 못함**).

| 시각(KST) | 건수 | 직전 회전과의 간격 | 계열 행 수 | 비고 |
|---|---|---|---|---|
| 10-13 21:25:48 | 1 | 같은 초 | 41 | UA 2종(ios, mac) |
| 10-14 22:26:13 | 1 | 같은 초 | 35 | UA 2종(ios, mac) |
| 10-16 20:20:14 | 1 | 7초 | 181 | iPhone, 계열 안 IP 6개 |
| 10-18 21:48:19 | 2 | 같은 초 | 5 | 같은 초에 2건 |
| 10-18 21:58:56 | 2 | 같은 초 | 3 | 같은 초에 2건 |
| 10-18 22:00:50 | 2 | 113초 | 3 | 같은 초에 2건 |
| 10-19 07:11:40 | 1 | 같은 초 | 12 | |
| 10-19 19:12:35 | 2 | 약 12시간 | 12 | 같은 초에 2건, 지연 재생 |
| 10-19 19:13:22 | 1 | 같은 초 | 198 | Mac Chrome |

해석(**추론**, 상관 증거):

- 13건 중 8건이 같은 초 안(직전 회전과 같은 초), 1건이 7초 안이다. 그리고 4쌍이 같은 초에 겹쳐 있다. 이것은 같은 쿠키를 가진 요청이 **거의 동시에 둘 이상** 도착했을 때의 모양이고, 동시 갱신 가설과 일치한다. 그러나 이 자료만으로 "같은 기기의 동시 갱신"임을 입증하지는 못한다. 쿠키가 다른 곳에도 있었다는 가설(복사·도난)도 같은 모양을 만든다.
- 113초(2건)와 약 12시간(2건) 사건은 동시 갱신으로 설명되지 않는다. 오래된 쿠키를 가진 다른 탭·기기가 나중에 갱신한 경우일 수 있고, 쿠키 도난일 수도 있다.
- 따라서 이전 문서의 "동시 갱신" 서술은 **추론**이며, 원인은 하나가 아닐 수 있다. 도난 가능성은 배제하지 못했다.

**코드 사실**: 이미 회전된 토큰이 한 번이라도 다시 오면 계열 전체를 폐기하고 401을 준다(`api/auth.py:133-140`). 유예가 없다. 프런트는 탭 안에서 `refreshInFlight`로 요청을 합치지만(`client.ts:111-125`) 탭·PWA·브라우저 사이는 합치지 않는다. 재사용 감사에는 UA·간격이 없다(`api/auth.py:137`).

**원인 확인 방법(계획, 모두 읽기 전용 또는 계측).**

1. 이미 수행: 감사 기록 × `refresh_tokens` 조인(위 표). 비밀 값은 출력하지 않았다.
2. 접근 로그 대조: 13개 시각 ±2초의 `POST /api/auth/refresh` 요청 수·UA·응답 코드를 uvicorn 접근 로그(`~/.ttakkari/deploy/logs/<포트>.log`)에서 grep한다. **이 Run에서는 못 했다**: 엔진 claude, 명령 `ls ~/.ttakkari/deploy/`, 오류 `[ttakkari-guard] ttakkari 보호 영역 접근 차단: ~/.ttakkari/deploy/`. `ops_search_logs`(`auth/refresh`, 72시간)는 0건이었다. 오류 이벤트 저장소라 접근 로그가 없다. 사용자 호스트 셸 또는 승인된 읽기 경로에서 수행해야 한다.
3. 같은 계열의 UA 구성을 계속 조회(위 표의 UA 2종 사건을 사용자 기기 목록과 대조: 같은 iPhone이 데스크톱 모드로 열었는지).
4. **재발 대비 계측(SES-01 1단계, 코드 몇 줄)**: 재사용 감사에 UA, 폐기 후 경과 시간, `replaced_by`(현재 미사용), 같은 초 도착 수, 소비된 토큰 해시 앞 8자, 요청 IP를 남긴다. 다음 사건부터 동시 갱신(같은 UA·같은 초)과 지연 재생(다른 UA·IP)을 구분할 수 있다.
5. 프런트 계측(선택): refresh 호출 맥락(탭 가시 상태, 호출 경로)을 서버가 로그로 남기는 헤더.

**개선(원인 확정 후 적용)**: (a) 위 4번 계측. (b) 프런트 Web Locks API로 탭 사이 refresh 직렬화. (c) 서버: 방금 회전된 토큰(20 내지 30초 이내, 같은 계열, 같은 UA)이면 계열을 폐기하지 않고 새 쿠키를 한 번 더 발급하며 `auth.refresh_grace`로 기록. 유예 안의 탈취 재생이 허용되는 것이 비용이고 유예 밖은 지금처럼 폐기한다. 다른 UA·IP의 재사용은 유예 없이 폐기한다.

## 7. 기존 SEC2 항목과의 매핑

기존 항목을 복제하지 않고 확장·세분화·참조만 했다.

| 이번 항목 | 기존 SEC2 항목과의 관계 |
|---|---|
| SEC2-NAC-01~06 | 기존 SEC2-EXPO-01(포트·터널 노출 인벤토리)의 실행안. EXPO-01의 관측(루프백·3306)을 이어받고 중복 조사는 하지 않음 |
| SEC2-NAC-03 | **SEC2-AUTH-03(루프백 IP 위조) 흡수.** AUTH-03은 이 항목으로 대체 |
| SEC2-LGN-01~05 | **SEC2-AUTH-03의 "로그인 제한 정책 결정"을 대체.** 정책값 확정은 사용자 승인 항목. 개정 2에서 레인 분리·원자 예약·shadow 단계·LGN-05(새 기기 등록 코드, 선택) 추가 |
| SEC2-SES-02 | **SEC2-AUTH-01(access JWT·grant 즉시 폐기, epoch)을 확장.** epoch 클레임과 `sid` 클레임을 한 번에 설계하도록 의존 관계로 연결. AUTH-01의 범위 변경은 하지 않음 |
| SEC2-SES-04 | SEC2-RD-01·AUTH-01과 연계(원격 데스크톱 연결 재검증) |
| SEC2-SES-05 | SEC2-WEB-01·WEB-02(CSP·XSS)에 의존, 쿠키 속성만 다룸 |
| SEC2-NAC-04 | SEC2-EXPO-02(`/api/version` 최소화)와 같은 공개 경로 목록을 공유 |
| 변경 없음 | SEC2-AUTH-02(완료), AUTHZ-03(진행 중), AGT-01·02 |

## 8. 확인하지 못한 것(확인 필요)

- tailnet ACL·Grants 현재 내용, HTTPS 인증서 활성 여부는 콘솔 값이 아니라 `CertDomains` 없음으로만 추정했다.
- 원격 로그인(SSH)·화면 공유 설정(guard에 막힘). `lsof`상 22·5900 리스너 없음만 확인.
- `cors_origins`·`TTAKKARI_GAME_ORIGIN_HOST` 등 운영 설정 값(`config.env`는 열지 않음). 허용 origin은 preflight 응답으로만 확인.
- Tailscale Serve가 클라이언트 `X-Forwarded-For`·`Tailscale-User-*`를 덮어쓰는 정확한 동작, cloudflared `path` 정규식 매칭의 실제 동작, iOS PWA에서 cross-site 쿠키 차단 여부. 모두 격리·실기기 시험 대상.
- 운영 리비전 `d922465`에 `/api/system/backup-status`가 없다는 것의 의미(배포 지연인지 경로 차이인지).
- 재사용 사건 13건의 실제 원인. 감사 기록에 UA·간격이 없어 DB 조인 상관 증거(6.1)까지만 확인했고, 접근 로그 대조는 guard에 막혔다(`~/.ttakkari/deploy/`). 동시 갱신과 쿠키 도난을 구분하지 못했다.
- 이 개정에서 `login_attempts` 환불 규칙, 레인 상한 값(버킷 6, 10분 refill, 시간당 30회)의 적정성은 설계 값이며 실측하지 않았다.
- 독립 검증 보고서 원문(guard 차단으로 읽지 못함). 판정 요약 외의 세부 지적은 반영하지 못했을 수 있다.
- 이 문서의 어떤 항목도 코드 실행이나 테스트로 구현을 검증하지 않았다. 구현 설계는 계획이다(미실행).
