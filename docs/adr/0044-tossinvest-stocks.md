# ADR 0044. 토스증권 읽기 전용 브라우저 어댑터와 주식 투자 틀

2026-10-18. 로봇·약관 실측, 백엔드 어댑터·API·MCP 읽기 도구, 프론트 주식 투자·토스증권 연결 화면 구현.
실제 로그인 세션 연결은 미검증(아래 §6). 보유 종목 값 추출은 다음 단계로 미룸.

## 1. 목적과 범위

- 주식 투자 상위 메뉴·라우트(`/stocks`)를 만들고, 하위 토스증권 연결(`/stocks/tossinvest`)에서
  연결 상태·수동 확인·동기화·작업 로그만 본다. 주문·매수·매도·이체·설정 변경은 범위 밖이며
  코드에도 그런 경로를 만들지 않는다.
- 백엔드는 저장된 로그인 세션으로 토스증권 홈을 읽기 전용으로 열어 로그인 유효 여부와
  관측된 읽기 API 경로(값 제외)만 기록한다. 자동 새로고침·스케줄이 없다.

## 2. 로봇·약관 실측 (2026-10-18, 격리 워커 샌드박스 curl)

- `https://www.tossinvest.com/robots.txt` (HTTP 200) 원문:
  `User-agent: *` / `Allow: /` / `Disallow: /_ul/`,
  Googlebot 전체 허용, bingbot은 `/api/` 금지. sitemap에 stocks·community 등 공개면만 있다.
  즉 공개면 크롤은 허용이나, 로그인 영역 자동화의 허가가 아니다.
- 이용약관: WTS 홈페이지 바닥글에는 서비스 이용약관 링크가 없고
  투자 유의사항(`/investment-disclaimers`), 이용자권리 및 유의사항(corp 문서 id=53),
  개인정보처리방침 드롭다운만 있다. id=53 본문(4247자)은 보안 수칙 문서로
  자동화·크롤링 금지 조항이 없다. 구속력 있는 WTS·앱 이용약관은 로그인 후·가입 시점에만
  보이므로 자동화 관련 문구를 검증하지 못했다(미검증).
- 결론: 로그인 영역 자동화가 금지로 보인다고 단정하지도, 허용으로 보지도 않는다.
  위험을 명시하고 요청 빈도를 사람 수준(수동 트리거·서버 최소 300초 간격)으로 제한한다.

## 3. 클로바노트 선례(ADR 0021·0027, fcc195d)에서 재사용·개선한 것

- 재사용: 전용 Chrome 프로필(`~/.ttakkari/tossinvest-chrome`), 로그인은 자동화 플래그 없는
  일반 Chrome 창으로 사람이 1회, 프로필 1개당 Chrome 1대 직렬화, 쿠키 값 비노출,
  상태·작업 로그 DB 기록, 읽기 MCP 도구.
- 개선: Playwright 의존성을 되살리지 않는다(ADR 0027 "별도 브라우저 검사 도구를 앱 의존성으로
  남기지 않는다" 유지). 설치된 Chrome + 최소 CDP 클라이언트(websockets·psutil·httpx는
  기존 의존성)로 Target·Page·Runtime·Network·Fetch를 쓴다. Runtime.evaluate는 고정 읽기 스니펫만.
- 잘 안 됐던 이유와 대책: 네이버는 자동화 Chrome 로그인을 봇으로 보고 보호를 걸었다 →
  처음부터 일반 Chrome 창 로그인만 지원. Playwright 가짜 키체인이 실제 키체인 쿠키를
  지운 사고 → Chrome을 직접 띄워 실제 키체인을 그대로 쓴다. 기본 프로필 사용 불가
  (Chrome 136+ 차단·macOS TCC) → 전용 프로필 + 존재 근거를 보고에 남긴다.

## 4. 읽기 전용 강제 (코드)

- `assert_read_only`: GET이 아니면 거부, 호스트는
  www/api/corp/docs-api.tossinvest.com만, 주문·매수·매도·이체·출금·설정·인증 힌트 경로 거부.
- 2026-10-18 독립 검증 후 보완: browser websocket에서 빈 page target에 session을 붙이고,
  탐색 전에 Fetch Request 단계의 가로채기를 설정한다. GET·HTTPS 기본 포트·호스트·인코딩을
  검사해 허용 요청만 continue, 나머지는 failRequest. 응답 대기 중 이벤트도 별도 reader가 처리한다.
  공개 HTML에서 확인한 static.toss.im·static.tossinvest.com은 Script/Stylesheet/Font/Image GET만
  별도 허용한다. 자식 target은 시작을 일시정지한 뒤 같은 통제를 설정하고 재개하며, 실패하면
  전체 확인을 중단한다. 서비스워커 우회와 WebSocket 차단도 탐색 전에 설정한다.
- 위 제어는 모의 CDP 통신으로 검증했다. Chrome 전체의 시작 시점 백그라운드 트래픽과
  실제 자식 target·서비스워커 동작, 포트 바인딩, 프로세스 종료는 호스트에서 미측정이다.
  브라우저 전체의 모든 통신이 읽기 전용이라고 보증하지 않는다.
- 관측된 페이지 자체 요청은 재생하지 않으며, 경로(쿼리 제거)만 최대 50개 기록한다.
- `tossinvest_sync_runs`에는 상태·메시지·페이지 제목·경로 목록·소요 시간만 둔다.
  금액·수량·쿠키·세션 값은 저장·반환·로그 어디에도 없다.
- 수동 트리거만: DB advisory transaction lock으로 간격 검사와 running 예약 저장을 직렬화하고
  브라우저 실행 전에 커밋한다. 마지막 시작/갱신 후 최소 300초 안에 오면 429 + Retry-After.
  실행 중 재시작·취소에도 예약이 남는다. 설정은 300 미만을 거부하고 실행은 180초 제한이다.

## 5. API·MCP·화면

- REST(소유자 JWT): `GET /api/stocks/overview`, `GET /tossinvest/status`(브라우저를 띄우지 않음),
  `GET /tossinvest/runs`, `GET /tossinvest/login-help`, `POST /tossinvest/check|sync`,
  `POST /tossinvest/login-window`(고정 인자로 일반 Chrome을 열어 사람이 로그인; 로그인 창·headless 확인 실행 중이면 409).
- 로그인은 사람이 전용 Chrome 창에서 마친 뒤 창을 닫는다. 토스증권 로그인은 약 1주일 유지되며,
  만료되면 다시 로그인한다.
- 에이전트: `GET /api/agent/stocks/tossinvest/*` 읽기 3개. MCP 읽기 도구 1개
  `tossinvest_status`(READ_ONLY_TOOLS). 보유·값 조회 도구는 실제 세션 관측 후 다음 단계.
- 프론트: 부가 기능 `주식 투자`(/stocks, 자리표시+연결 카드), 하위 `/stocks/tossinvest`
  (상태·확인/동기화 버튼·작업 로그·최초 로그인 안내). 디자인 시스템 tk-* 계약과
  FEATURES·APP_ROUTE_ROOTS 등록 규칙을 따른다.

## 6. 검증과 미검증

- 검증: back 단위 24 passed(읽기 전용 가드·상태 판정·API·속도 제한, 브라우저·DB 없음),
  back ruff clean, front vitest 899 passed·10 skipped(`--maxWorkers=2`; 기본 병렬은
  이 기기 부하로 github 쪽 3~4개가 오락가락 실패하며 격리 실행은 전부 통과),
  front lint·tsc·vite build exit 0, alembic head 0045 단일, stocks 라우터 등록 확인.
- 미검증: DB 통합 pytest 전체(격리 샌드박스가 loopback·AF_UNIX를 막아 postgres 접속 불가),
  실제 로그인 세션 연결(아래), 운영 배포 후 동작.
- 실제 연결 1회(2026-10-18, 격리 워커 샌드박스, 스크래치 프로필): Chrome 바이너리 발견,
  headless 실행이 `Abort trap: 6`(exit 134)으로 종료 → `state=error`.
  샌드박스 한정이므로 호스트 불가로 일반화하지 않는다. 전용 프로필에 로그인이 없어
  호스트에서도 첫 결과는 `login_required` + 사용자 1회 로그인 안내가 예상된다.

## 7. 다음 단계

1. 사용자가 일반 Chrome 창으로 전용 프로필에 로그인 → UI에서 연결 확인.
2. 로그인 상태에서 관측된 읽기 API 경로를 매핑해 보유·관심 요약 추출(값 표시는 그때 설계).
3. 값 조회 MCP 도구 추가 여부 판단. 주문·이체 경로는 계속 만들지 않는다.

## 8. 독립 검증 및 배포 검사 보완 (2026-10-18)

- 원 구현 269b8c6: Codex 검증에서 페이지 POST 미차단과 조회형 간격 가드의 동시성 문제를 발견했다.
  실제 DB에서 동시 요청 8개가 모두 202였고, 보완 후에는 1개 202 + 7개 429, 관측 호출 1회였다.
  작업 도중 별도 DB 세션에서 running 예약이 보이고 요청 취소 후에도 429가 유지됨을 검사했다.
- 최초 배포 예약 84091e54-04d1-423c-83bb-118463f2c35b는 failed.
  릴리스 pytest 수집에서 tests/test_tossinvest.py와 unit_tests/test_tossinvest.py의 이름이 충돌했다.
  단위 파일을 test_tossinvest_unit.py로 바꾸고 전체 테스트 동시 수집을 검사했다.
- 단위와 통합을 따로 실행한 기존 결과만으로 배포 검사 통과를 보증하지 않는다.
  실제 로그인 연결은 여전히 미검증이며, Codex 명령 샌드박스의 Chrome IPC 차단을 호스트 불가로
  일반화하지 않는다. 후속 검사 결과는 docs/work-history/2026-10-18-tossinvest-guardrails.md에 기록한다.
