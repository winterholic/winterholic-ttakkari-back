# 네트워크 접근 통제·로그인 제한·세션 보안 감사 (2026-10-19)

SEC2 보안감사 1단계 중 "네트워크 접근 통제·로그인 제한·세션 보안" 파트의 산출물이다.
실행 항목은 [보안 WBS](wbs.md) 6절(NAC·LGN·SES)에 있고, 위협 모델은 [threat-model.md](threat-model.md)에 있다.
코드 기준점은 `back` `dfee8d4`(작업 트리에 다른 작업자의 미커밋 변경 있음, 이 문서는 건드리지 않음), `front` `630040b`다.

표기 규칙은 기존 문서와 같다. **확인됨**은 이번 조사에서 명령 결과나 코드 줄로 직접 확인한 것, **추론**은 코드 구조로 보아 그럴 가능성이 높지만 재현하지 않은 것, **확인 필요**는 근거를 얻지 못한 것이다.

이번 단계는 분석과 계획만 했다. 운영 코드, 운영 설정, 포트, 방화벽, 터널, Tailscale 설정은 바꾸지 않았다. 비밀 값(`config.env`, 토큰, 키)은 읽거나 기록하지 않았다.

## 0. 결론

공개 도메인을 통한 **우회 접근은 확인되지 않았다**. 인증이 필요한 라우트는 전부 401이었고, 헤더 위조(Tailscale-User-*, X-Forwarded-For, CF-Connecting-IP, Host)는 인증을 우회하지 못했다.
다만 **공개 도메인이 소유자 API 408개 전체를 인터넷에 그대로 노출**하고 있고, 그 앞의 방어선이 비밀번호 1개와 메모리 안의 IP별 실패 카운터뿐이라는 구조적 문제가 있다. 비밀번호가 맞으면 에이전트 실행(= 이 Mac의 명령 실행)까지 열린다.

| ID | 발견 | 위험도(L×I) | 상태 | 권고 | 사용자 승인 |
|---|---|---|---|---|---|
| F1 | 소유자 전체 API가 공개 터널에 노출. 네트워크 경계 없음 | **P0 6 (2×3)** | 확인됨 | Tailscale Serve를 내부 입구로 병행 도입 후, 공개 터널은 공개 경로 허용목록만 통과 | **필요** (방식, PWA origin 이전, 관리 콘솔 작업) |
| F2 | 로그인 실패 제한이 프로세스 메모리·IP 단위. 재시작·배포·세대 전환마다 초기화, 계정 단위 제동 없음 | P1 6 (2×3) | 확인됨(코드) | DB 영속 단계 지연(계정+출처), 신뢰 기기 예외, 로컬 해제 CLI | **필요** (정책값, 신뢰 기기 쿠키) |
| F3 | refresh 재사용 오탐으로 강제 재로그인. 운영 DB에 `auth.refresh_reuse` 13건(10-13부터 10-19) | P1 4 (2×2) | 확인됨(DB), 원인은 추론 | 원인 계측 → 동시 갱신 잠금(프런트) + 서버 유예창 | 불필요 |
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
Tailscale: 설치·실행 중(1.102.4, 독립 배포판 앱 + 시스템 확장). Serve·Funnel 설정 없음. RunSSH=False, ShieldsUp=False.
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
| Tailscale Serve/Funnel | 설정 없음 | `tailscale serve status`, `funnel status` |
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
| 소유자 API 나머지(289) | PWA | access JWT | 내부 전용 |
| `/api/agent`·`/api/orchestrator`(105) | 엔진 프로세스가 **루프백**으로 호출 | 에이전트 JWT | 내부 전용. 지금은 공개 도메인으로도 도달(401) |
| `/api/remote-desktop/*`, WS `/socket` | PWA | JWT + 재인증 grant + 티켓 | 내부 전용 |
| `/api/ready`, `/api/bootstrap-ready` | 배포 감독(루프백) | 없음 | 이미 Caddy가 404 차단(확인됨) |
| 게임 origin(ADR 0049) | 게임 플레이 | play 토큰 | 터널 ingress에 해당 호스트 없음 → 현재 미공개로 추정(`TTAKKARI_GAME_ORIGIN_HOST` 값은 열지 않음, 확인 필요) |
| Vercel SPA `ttakkari.winterholic.net` | 브라우저 | 없음(정적) | 공개 유지. `/share/*`가 이 호스트에서 열림 |

외부 연동(MCP, GitHub, 날씨, NotebookLM·토스 Chrome, 사용량 조회, Web Push)은 모두 **아웃바운드**다. 외부에서 들어오는 webhook 진입점은 없으므로, 공개 경로를 줄여도 webhook은 깨지지 않는다.

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

우회는 없지만 **공개 표면이 크다**. 공개 도메인에서 도달 가능한 소유자 API 289개, 에이전트·오케스트레이터 105개, 원격 데스크톱 라우트는 대부분 자체 취약점이 없더라도 (a) 비밀번호 추측, (b) 프레임워크·인증 파서 취약점, (c) 앞으로 추가될 라우트의 인증 누락에 노출된다. 인벤토리 테스트(SEC2-GATE-00)가 (c)를 줄여 주지만 (a)(b)는 네트워크 경계로만 닫힌다. 운영 DB에서 확인한 실제 시도 흔적은 `auth.login denied` 3건(2건이 10-11, 1건이 10-13, 서로 같은 IP)뿐이라 지금까지 실공격의 증거는 없다. 단 429로 막힌 시도는 기록되지 않으므로(F10) 이 숫자는 하한이다.

## 4. Tailscale 접근 통제 설계안

### 4.1 현 환경의 제약

1. **PWA와 API는 현재 같은 사이트다.** `ttakkari.winterholic.net`(Vercel)과 `ttakkari-api.winterholic.net`은 둘 다 `winterholic.net` 아래라 same-site다. ADR 0004는 "다른 사이트"를 전제로 `SameSite=None`을 골랐지만 지금은 그렇지 않다.
2. API를 `*.ts.net`으로 옮기면 SPA(Vercel)에서 API는 **cross-site**가 된다. refresh 쿠키가 서드파티 쿠키가 되어 iOS Safari·홈 화면 PWA에서 차단될 수 있다(ITP, **확인 필요**: 실기기 시험). 막히면 앱을 열 때마다 재로그인하게 되므로 사용자 요구와 충돌한다.
3. 세대 배포 검증이 공개 URL의 `/api/health`, `/api/version`을 쓴다. 공개 경로를 줄일 때 이 둘은 남겨야 한다.
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

| 단계 | 내용 | 검증 | 롤백 |
|---|---|---|---|
| P0 사전 | 관리 콘솔에서 HTTPS 인증서 활성, 현재 ACL 내보내기, Mac Studio 키 만료 해제. `~/.cloudflared/ttakkari.yml` 백업 | `tailscale status --json`에 `CertDomains` 표시 | 없음(설정 읽기) |
| P1 프록시 이중 리스너 | 8788 추가(헤더 제거). 8787은 그대로 | 배포 감독 검증(health·version) 통과, `lsof`에 8788 루프백 | 이전 `proxy_config` 복귀 배포 |
| P2 Serve 병행 | `tailscale serve --bg --https=443 http://127.0.0.1:8788` 형태. 공개 터널은 그대로 유지 | iPhone(Wi-Fi 끄고 셀룰러 + Tailscale 켬)에서 `…ts.net/api/health`, 로그인, SSE, 푸시 | `tailscale serve reset`(명령 하나, 즉시) |
| P3 SPA same-origin | 프런트 빌드를 Serve 경로로 제공, PWA 재설치, 푸시 재구독, 7일 병행 사용 | 앱 재실행 시 재로그인 없이 복구, 푸시 수신, 원격 데스크톱 연결 | 기존 Vercel PWA로 복귀(공개 도메인이 아직 열려 있으므로 가능) |
| P4 터널 허용목록 | `ttakkari.yml`에 `path` 정규식 추가(아래), 터널 재시작 | 공개 도메인에서 `/api/workspaces` 404, `/api/health`·`/api/version`·공유 링크 정상, 세대 배포 검증 통과 | 백업 yml 복원 + 터널 서비스 재시작 |
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

주의: (1) 최초 부트스트랩 스모크가 공개 URL로 소유자 토큰 호출을 한다(`generation_bootstrap.py:143`). 이미 끝난 최초 전환용인지 확인하고, 아니면 base URL을 설정값으로 바꿔야 한다. (2) 모든 구 PWA 클라이언트(공개 도메인 사용 중)는 P4 이후 동작하지 않는다. P3 완료 전에 P4를 하면 앱이 잠긴다. (3) 전환 중에도 Mac 로컬 콘솔과 노트북(tailnet)이 비상 경로다.

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

### 5.2 목표 정책

요구: 5회 실패 → 1분, 이후 5분, 15분, 최대 60분 단계 지연. 계정과 출처를 모두 고려. 재시작 후 유지(DB). 병렬 요청 레이스 방지. 사용자 존재 비노출. Mac Studio 로컬 관리자 전용 해제(일반 API 미노출). 공격자에 의한 영구 차단 방지. 실행 중 에이전트 Run 무영향.

정책 해석(확정 필요): **5회 시도 → 1분 잠금 → 해제 후 1회 탐침 시도 → 실패하면 5분 → 1회 탐침 → 15분 → 1회 탐침 → 60분(상한, 이후 반복)**. 단계마다 5회씩 주는 해석은 출처당 하루 추측 가능 수가 5배 커지므로 권고하지 않는다. 출처당 하루 최대 시도는 5 + 24 = 29회(60분 상한 도달 후). 24시간 동안 실패가 없으면 단계가 처음으로 돌아간다. 성공하면 즉시 처음으로 돌아간다.

### 5.3 설계

**키 두 종류.**

| 키 | 의미 | 정책 |
|---|---|---|
| `src:<ip>` | 출처. IPv4는 주소, IPv6는 /64 접두사. 헤더가 없으면 `src:unknown` | 위 5.2 단계 지연 |
| `acct:owner` | 소유자 계정 전체(단일 사용자라 고정) | 출처와 무관하게 **신뢰 기기가 아닌** 시도의 합산. 예산 20회, 같은 단계 지연 |

**신뢰 기기 쿠키(`ttk_dev`)가 "공격자에 의한 영구 차단 방지"의 핵심이다.** 계정 단위 잠금을 모두에게 적용하면 공격자가 저비용으로 소유자를 계속 잠글 수 있다. 그래서:

- 로그인 또는 refresh 성공 시 서버가 불투명 난수(256비트)를 `ttk_dev`로 발급한다(HttpOnly, Secure, refresh 쿠키와 같은 속성, 수명 180일). DB에는 SHA-256만 저장한다(`login_devices`).
- 신뢰 기기 쿠키가 유효한 시도는 `acct:owner`의 합산·잠금을 받지 않는다. **자기 출처 키(`src`)는 여전히 받는다**.
- 쿠키는 인증이 아니다. 쿠키만으로는 로그인되지 않고 비밀번호가 필요하다. 쿠키가 도난되어도 얻는 것은 "계정 단위 잠금 면제"뿐이다.
- 비밀번호 변경과 `logout-all`은 `login_devices`를 전부 폐기한다. 기존 12개 세션(활성 계열 12개, 확인됨)은 다음 refresh에서 쿠키를 받으므로 사용자는 아무것도 하지 않는다.
- 계정 단위 잠금이 걸린 동안 **새 기기**(쿠키 없음)의 로그인은 최대 60분 지연된다. 이것이 남는 비용이고, Tailscale 경계(4절)가 적용되면 외부 공격이 그 상태까지 가지 못하므로 사실상 사라진다. 급하면 Mac 로컬 해제를 쓴다.

**상태 저장.** 모든 상태를 DB에 둔다. 같은 DB를 모든 세대가 보므로 재시작·세대 전환에 영향받지 않는다.

```sql
CREATE TABLE login_throttle (
  key            text PRIMARY KEY,            -- 'src:203.0.113.7' | 'src6:2001:db8:1:2::/64' | 'acct:owner'
  scope          text NOT NULL,               -- 'source' | 'account'
  budget         integer NOT NULL,            -- 다음 잠금까지 남은 시도
  level          smallint NOT NULL DEFAULT 0, -- 0..4 (잠금 단계)
  locked_until   timestamptz,
  last_failure_at timestamptz,
  lock_count     integer NOT NULL DEFAULT 0,  -- 누적(감사·알림용)
  updated_at     timestamptz NOT NULL
);
CREATE INDEX ix_login_throttle_updated_at ON login_throttle (updated_at);

CREATE TABLE login_devices (
  id           uuid PRIMARY KEY,
  token_hash   char(64) NOT NULL UNIQUE,
  created_at   timestamptz NOT NULL,
  last_seen_at timestamptz NOT NULL,
  revoked_at   timestamptz,
  user_agent   varchar(512)
);
```

현재 마이그레이션 head는 작업 트리에서 `0048_game_active_build`(미커밋)이므로 구현 시점의 head 다음 번호를 쓴다. 두 테이블 모두 신규 표라 online-safe다. 유휴 행(level 0, 7일 갱신 없음)은 기존 retention 루프에서 지운다.

**레이스 방지: 시도를 검증 전에 먼저 예약(pessimistic reservation).**

한 요청의 순서:

1. 짧은 트랜잭션 하나에서 `SELECT … FOR UPDATE`로 `src` 행, 이어서 `acct` 행을 잠근다(항상 이 순서, 교착 방지). 행이 없으면 `INSERT … ON CONFLICT DO NOTHING` 후 잠금.
2. 24시간 이상 실패가 없으면 `budget=5, level=0`으로 되돌린다.
3. `locked_until > now`이면 거절(429, `Retry-After`).
4. `budget == 0`이면(잠금이 방금 끝남) `budget = 1`(탐침 1회).
5. `budget -= 1`. 0이 되면 `level = min(level+1, 4)`, `locked_until = now + (60, 300, 900, 3600)[level-1]`. **이 요청은 진행**하고 같은 순간 도착한 다른 요청은 3번에서 거절된다.
6. 커밋 후 Argon2 검증을 수행한다(락을 쥐지 않음).
7. 성공이면 `src`(와 신뢰 기기가 아니면 `acct`)를 `budget=5, level=0, locked_until=NULL`로 되돌리고 `ttk_dev`를 발급한다. 실패이면 `last_failure_at`만 갱신하고 감사 기록을 남긴다(이미 예약 단계에서 셌음).

20개를 병렬로 보내도 행 잠금이 직렬화하므로 정확히 5개만 검증까지 간다. 클라이언트가 끊겨도 예약은 실패로 남는다(보수적).

기존 메모리 `LoginLimiter`는 **CPU 보호용**(동시 검증 1, 대기열, 전역 토큰 버킷)으로만 남기고 IP별 실패 목록은 제거한다. 전역 버킷이 소유자를 막는 문제는 신뢰 기기 쿠키가 있는 요청을 버킷 소비에서 제외해 해결한다(동시 검증 상한은 유지).

**사용자 존재 비노출.** 입력에 계정 식별자가 없어 열거 대상이 없다. 그래도 일관성을 위해: (1) 잠긴 상태에서는 비밀번호 검증을 하지 않고 어떤 키(출처·계정)가 잠겼는지 드러내지 않는 같은 429 본문을 쓴다. (2) `Credential`이 없을 때도 더미 해시로 검증을 한 번 수행해 응답 시간을 맞춘다. (3) 운영에서 "비밀번호가 설정되지 않았습니다" 503은 최초 설정 전에만 나오게 하고, 이후에는 같은 401을 쓴다.

**Mac Studio 로컬 관리자 전용 해제.** HTTP 라우트로는 만들지 않는다. `ttakkari login-status`(현재 잠금 목록), `ttakkari login-unlock [--source IP | --all]`를 `set-password`와 같은 방식(`cli.py:67-80`, DB 직접 접속)으로 추가한다. `set-password`는 잠금과 신뢰 기기도 같이 초기화한다. 라우트 인벤토리 테스트가 `unlock` 류 라우트가 없음을 고정한다. 같은 uid의 에이전트도 이 CLI를 실행할 수 있지만, 그것은 `set-password`로 비밀번호를 바꿀 수 있는 현재 신뢰 수준(ADR 0048)보다 약한 권한이고 해제는 보안 상태를 낮추지 않으므로(인증을 면제하지 않음) 새 위험이 아니다.

**영구 차단 방지.** (1) 모든 잠금에 상한 60분이 있고 24시간 무실패면 초기화된다. (2) 계정 단위 잠금은 신뢰 기기를 막지 않는다. (3) 로컬 해제 CLI. (4) 출처 키는 /64 단위라 공격자가 한 사용자의 정상 출처를 오염시킬 수 없다(출처 위조 불가 경로에서). 루프백에서 헤더를 위조해 **다른 IP의 키를 잠그는** 경우(같은 uid 에이전트)는 4.3의 프록시 분리와 신뢰 기기 예외로 영향이 제한된다.

**에이전트 Run 무영향.** 이 로직이 닿는 곳은 `POST /api/auth/login`과 원격 데스크톱 재인증뿐이다. 에이전트 JWT(`aud=ttakkari-agent`), MCP 루프백 호출, SSE, refresh, 진행 중인 소유자 세션은 이 상태를 읽지 않는다. 대조 테스트로 고정한다.

**원격 데스크톱 재인증.** 같은 예약 로직을 쓰되 키는 출처 키만 쓴다(유효한 JWT가 이미 있으므로 계정 키는 소비하지 않음). 이로써 도난 access JWT로 비밀번호를 추측하는 경로도 같은 지연을 받는다.

**관측.** 잠금 전이(level 상승)만 감사 기록(`auth.login_locked`, `outcome=denied`, 단계·키 종류·IP 요약)으로 남긴다(요청마다 쓰면 공격자가 감사 테이블을 채운다). level 2 이상이거나 `acct` 잠금이면 기존 알림 모듈로 푸시한다(30분당 1회).

**롤아웃.** `login_throttle_mode = off | shadow | enforce`. shadow에서는 예약만 하고 거절하지 않으며 3 내지 7일 동안 실제 기록을 본 뒤 enforce로 올린다. 롤백은 설정값 한 줄이고 데이터 변경은 없다.

### 5.4 구현 위치

| 파일 | 변경 |
|---|---|
| `src/ttakkari/security/login_throttle.py`(신규) | 상태 기계, DB 예약·성공·실패, 키 계산(`/64`), 신뢰 기기 검증 |
| `src/ttakkari/security/auth.py` | `LoginLimiter`를 CPU 보호로 축소, `client_ip` 유지 |
| `src/ttakkari/api/auth.py` | `login`에서 예약 → 검증 → 성공/실패 처리, `ttk_dev` 발급(login·refresh), 더미 검증 |
| `src/ttakkari/api/remote_desktop.py` | `reauthenticate`를 같은 예약으로 교체(`admission` 포함) |
| `src/ttakkari/models.py`, `alembic/versions/` | 두 표 |
| `src/ttakkari/config.py` | `login_stages_seconds`, `login_budget`, `login_acct_budget`, `login_quiet_reset_seconds`, `login_throttle_mode` |
| `src/ttakkari/cli.py` | `login-status`, `login-unlock`, `set-password`가 초기화 |
| 기존 retention 루프 | 유휴 행 정리 |

### 5.5 테스트 계획

`tests/test_login_throttle.py`(시간을 주입하는 단위·DB 테스트, 기존 격리 DB `ttakkari_test` 사용):

1. 5회 실패 후 6번째는 429, `Retry-After`가 약 60. 61초 뒤 탐침 1회 허용, 실패하면 300, 900, 3600, 이후 3600 유지.
2. 성공하면 단계·예산 초기화. 24시간 무실패면 초기화.
3. 다른 출처 B는 A의 잠금과 무관. IPv6는 같은 /64가 한 키.
4. 계정 경보: 서로 다른 출처 20회 실패 → 신뢰 기기 쿠키 없는 시도는 거절, **쿠키 있는 시도는 통과**(영구 차단 방지).
5. 재시작 지속: 새 엔진·새 모듈 인스턴스로도 잠금 유지. 두 개의 uvicorn 세대 프로세스가 같은 상태를 봄.
6. 레이스: `asyncio.gather` 20개 병렬 오답 → `verify_password` 호출이 정확히 5회. 교착 없음(출처 키 여러 개 + 계정 키 동시).
7. 사용자 존재 비노출: 잠금 응답 본문이 키 종류·비밀번호 정오와 무관. `Credential` 없음 대조 시 응답 시간 차이가 허용 범위 안.
8. 에이전트 무영향: 출처·계정 모두 잠금 상태에서 에이전트 토큰의 `/api/agent/*`, 소유자 세션의 SSE·refresh가 200.
9. 로컬 해제: CLI가 잠금을 풀고, `/api/auth/*`에 해제 라우트가 없음(인벤토리 테스트).
10. 헤더 위조: 비루프백 피어가 보낸 `CF-Connecting-IP` 무시(기존 테스트 유지), 이중 리스너에서 8788 경로는 헤더 제거.
11. 원격 데스크톱 재인증이 같은 예산을 소비.
12. 마이그레이션 up/down. 기존 `test_password_session_revocation.py`의 limiter 테스트를 새 구현에 맞게 갱신(RED 먼저).
13. 부하(격리): 출처 200개 동시 시도에서 CPU 보호 상한(동시 검증 1)이 유지됨.

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
| 11 | 탈취 세션 재사용 | refresh 회전·재사용 탐지로 감지(`api/auth.py:133-140`). 단, **오탐이 잦다**(아래 F3). 기기 바인딩·이상 출처 알림 없음. UA·IP는 저장(`api/auth.py:53-64`)하나 검증하지 않음 | 중간(2×2) | SES-01(오탐 해소), SES-06(새 기기 로그인·재사용 알림) |
| 12 | 브라우저·PWA 일관성 | access는 메모리라 앱을 열 때마다 refresh 호출(`front/src/auth.tsx:17-25`). 탭이 둘 이상이거나 PWA와 브라우저가 겹치면 refresh가 동시에 나감(탭 안에서는 `refreshInFlight`로 합쳐지나 탭 사이는 아님, `client.ts:111-125`). Service Worker는 `/api/*`를 NetworkOnly(`vite.config.ts:41-44`)라 토큰 캐시 없음 | 중간(F3) | SES-01 |

### 6.1 F3 상세: refresh 재사용 오탐

- 사실(확인됨, 운영 DB): `auth.refresh_reuse` 13건. 날짜별 1(10-13), 1(10-14), 1(10-16), 6(10-18), 4(10-19), 하루 모두 **같은 IP 1개**. 같은 기간 `auth.login ok`는 19건. 즉 소유자 기기에서 반복해서 강제 로그아웃이 일어난 흔적이다.
- 코드: 이미 회전된(폐기된) 토큰이 한 번이라도 다시 오면 계열 전체를 폐기하고 401을 준다(`api/auth.py:133-140`). 유예가 없다.
- 원인(**추론**, 계측 전): 탭·PWA 동시 refresh, 또는 응답이 유실된 뒤 재시도(모바일 네트워크 전환)에서 같은 쿠키가 두 번 도착. 재사용 감사 기록에 UA·토큰 나이가 없어 지금은 구분할 수 없다(`api/auth.py:137`).
- 개선: (1) 계측: 재사용 감사에 UA, 폐기 후 경과 시간, `replaced_by`를 남긴다(코드 몇 줄). (2) 프런트: Web Locks API로 탭 사이 refresh를 직렬화. (3) 서버: 방금 회전된 토큰(예: 20 내지 30초 이내)이고 같은 계열이면 계열을 폐기하지 않고 새 쿠키를 한 번 더 발급하며 `auth.refresh_grace`로 기록. 유예 안의 진짜 탈취 재생은 허용되는 것이 비용이고, 유예 밖은 지금처럼 폐기한다. 먼저 (1)로 원인을 확정한 뒤 (2)(3)을 적용한다.

## 7. 기존 SEC2 항목과의 매핑

기존 항목을 복제하지 않고 확장·세분화·참조만 했다.

| 이번 항목 | 기존 SEC2 항목과의 관계 |
|---|---|
| SEC2-NAC-01~06 | 기존 SEC2-EXPO-01(포트·터널 노출 인벤토리)의 실행안. EXPO-01의 관측(루프백·3306)을 이어받고 중복 조사는 하지 않음 |
| SEC2-NAC-03 | **SEC2-AUTH-03(루프백 IP 위조) 흡수.** AUTH-03은 이 항목으로 대체 |
| SEC2-LGN-01~04 | **SEC2-AUTH-03의 "로그인 제한 정책 결정"을 대체.** 정책값 확정은 사용자 승인 항목 |
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
- 재사용 오탐의 실제 원인. 감사 기록에 UA가 없어 추론에 머물렀다.
- 이 문서의 어떤 항목도 코드 실행이나 테스트로 구현을 검증하지 않았다. 구현 설계는 계획이다(미실행).
