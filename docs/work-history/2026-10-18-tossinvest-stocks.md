# 주식 투자·토스증권 연결 기초 틀 · 2026-10-18

> 후속 정정: 원 구현의 읽기 전용 요청 통제와 300초 간격 가드가 독립 검증에서 불충분한 것으로
> 확인됐고, 최초 배포도 테스트 파일명 충돌로 실패했다. 이 문서의 구현·검사 항목은 최초 작업
> 시점 기록이다. 현재 보완과 실행 근거는 [후속 기록](2026-10-18-tossinvest-guardrails.md)을 본다.

## 한줄 요약
`/stocks`+`/stocks/tossinvest` 화면과 읽기 전용 토스증권 어댑터·API·MCP 1개를 clean worktree에서 구현했다.
robots는 공개면 허용이나 로그인 영역 자동화 허가가 아니고, 구속력 있는 이용약관은 로그인 후 노출이라 미검증이다.
실제 연결 1회는 격리 샌드박스의 Chrome SIGABRT로 실패했다(호스트 불가로 일반화하지 않음).

## 작업 위치
- back-tossinvest·front-tossinvest worktree (origin/main 기준, detached). 본 체크아웃의 다른 세션 미커밋은 건드리지 않았다.

## 선례·실측
- back fcc195d(ADR 0021) 독스트링에서 전용 프로필·일반 Chrome 1회 로그인·가짜 키체인 쿠키 삭제 사고·세션 헤더 방식을 재사용했다.
  Playwright는 되살리지 않았다(ADR 0027 유지). 제거 문서 `docs/stt/remove-clovanote-2026-10-15.md`, ADR 0027, `~/work-history/2026-10-13.md` 23:15 항목을 읽었다.
- robots(200): `User-agent: * / Allow: / / Disallow: /_ul/`, Googlebot 전체 허용, bingbot `/api/` 금지.
- 약관: WTS 바닥글에 서비스 이용약관 링크 없음. corp id=53 이용자권리 및 유의사항(4247자)은 보안 수칙 문서로 자동화 금지 조항 없음.
  id=31은 신용정보 활용체제. 구속 약관은 로그인 후 노출이라 미검증.

## 구현
- back: `src/ttakkari/stocks/tossinvest.py`(Chrome 직접 실행+최소 CDP, GET·허용 호스트·상태변경 경로 가드),
  `src/ttakkari/api/stocks.py`(overview/status/runs/login-help/check/sync + agent 읽기 3개),
  `TossInvestSyncRun`+마이그레이션 0045, 설정 4개(`tossinvest_*`), MCP `tossinvest_status`(READ_ONLY).
  금액·수량·쿠키는 저장·반환하지 않는다. 수동 트리거+기본 300초 429.
- front: FEATURES `주식 투자`(/stocks 자리표시+연결 카드), `/stocks/tossinvest`(상태·확인/동기화 버튼·작업 로그·최초 로그인 안내),
  `stocksTypes`+client 6함수, chart 아이콘(ChartCandlestick), APP_ROUTE_ROOTS·AppShell 테스트 갱신.
- 문서: ADR 0044, api-contract 주식 투자 절.

## 검사
- back 단위 24 passed(DB·브라우저 없음), ruff clean, alembic head 0045 단일, 라우터 등록 확인.
  `tests/test_tossinvest.py`(DB 통합) 작성했으나 격리 샌드박스가 loopback·AF_UNIX를 막아 postgres 접속 불가라 미실행.
- front vitest 899 passed·10 skipped(`--maxWorkers=2`; 기본 병렬은 기기 부하로 github 쪽 3~4개 오락가락 실패, 격리 실행 전부 통과),
  lint·tsc·vite build exit 0.
- 실제 연결 1회(격리 워커 샌드박스, 스크래치 프로필): Chrome 발견, headless `Abort trap: 6`(exit 134) → `state=error`.
  전용 프로필 로그인도 없어 호스트 첫 결과는 `login_required`+사용자 1회 로그인 안내가 예상된다.

## 다음
1. 사용자가 일반 Chrome 창으로 전용 프로필(`~/.ttakkari/tossinvest-chrome`)에 로그인 → UI에서 연결 확인.
2. 로그인 상태에서 관측된 읽기 API 경로를 매핑해 보유·관심 요약 추출.
3. 값 조회 MCP 도구 추가 여부 판단. 주문·이체 경로는 계속 만들지 않는다.
