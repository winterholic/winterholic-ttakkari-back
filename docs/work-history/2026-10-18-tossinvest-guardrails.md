# 토스증권 독립 검증 지적과 배포 검사 보완 · 2026-10-18

## 판정과 범위

원 구현 269b8c6의 완료 보고는 독립 Codex 검증에서 failed로 기록했다.
작업자 dd84b528-dbf8-4e5c-82ab-7c8001a0540f에
verifier f11e8bf7-b887-43c0-844b-90a16ace34c2 및 실패 근거를 message_worker로 기록했다.
이번 보완은 기획 Codex 세션에서 직접 수행했다. 실제 로그인 연결은 여전히 미검증이다.

## 변경

- 간격 검사와 running 예약 저장을 PostgreSQL advisory transaction lock으로 직렬화하고
  브라우저 작업 전에 커밋한다. check/sync는 같은 예약을 사용한다.
  기본·최소 간격은 300초, 완료 후에도 300초를 보장하고 실행은 180초로 제한한다.
  실행 취소·재시작 뒤에도 마지막 예약이 남아 재요청을 제한하며, 중단 예약은 간격 후 재시도 가능하다.
- browser websocket에 Page 명령을 보내던 경로를 page target의 flat session으로 고쳤다.
  탐색 전 Fetch Request 통제를 설정하고, 명령 응답 대기 중 들어오는 요청도 별도 reader가 처리한다.
  POST·다른 메서드·주문 힌트·외부 호스트·비 HTTPS·기본 외 포트·중첩 인코딩을 차단한다.
  자식 target은 통제 설정 뒤 재개하고, 통제 실패 시 전체 확인을 중단한다.
- 공개 홈 HTML의 script/link 호스트를 urllib로 확인했다:
  static.toss.im, static.tossinvest.com, www.tossinvest.com.
  앞의 두 호스트는 Script/Stylesheet/Font/Image의 GET만 허용한다. XHR·POST는 계속 거부한다.
  정상 조회를 위한 POST도 차단하므로 실제 사이트의 로그인 갱신·일부 화면 기능은 실패할 수 있다.
- Chrome 제어 주소에 loopback을 명시하고 와일드카드 origin 옵션을 없앴다.
  시작 대기 15초, 시작 실패·취소 시 정리, 강제 종료 후 wait 및 stderr close를 추가했다.
  이 종료 경로는 모의 프로세스 검사이며 실제 Chrome 프로세스 실측은 아니다.
- tests와 unit_tests의 test_tossinvest.py 이름 충돌을 단위 파일 이름 변경으로 해소했다.
  MCP 도구 목록에 tossinvest_status를 추가하고 generation-contract를 migration head 0045와 맞췄다.
  0045는 테이블 추가뿐이므로 0044에서의 온라인 마이그레이션 허용 목록을 확장했다.
- 프론트는 running 상태를 두 주식 화면에서 '확인 중'으로 표시한다.
  front bed59a9를 origin/main에 push했다. 기존 front 작업 트리의 미커밋은 건드리지 않았다.

## 실행 근거

환경: macOS Codex 명령 샌드박스. DB 검사는 localhost PostgreSQL의 별도 테스트 DB
ttakkari_toss_guard_20261018에서 실행했고 운영 DB는 사용하지 않았다.

- RED: 원 코드의 새 DB 검사에서 동시 요청 8개 모두 202였으며 running 예약 행이 없었다(2 failed).
  모의 CDP 검사에서 Fetch.enable 부재·허용되지 않아야 할 URL·간격 0 허용을 재현했다(6 failed).
- GREEN: `python -m pytest -q tests/test_artcraft.py::test_gui_start_stop_with_confirmation
  tests/test_context.py::test_tools_listing_matches_mcp_server tests/test_generation_deployment.py
  tests/test_tossinvest.py unit_tests/test_tossinvest_unit.py unit_tests/test_tossinvest_cdp.py`
  → **91 passed, 1 warning in 6.87s**. 실행 시 TTAKKARI_TEST_DB와 PYTHONPATH=src를 지정했다.
  동시 8건 중 1건만 202·관측 1회, 나머지 7건 429, 별도 DB 세션의 running 조회,
  요청 취소 후 예약 유지, CDP 세션·POST/외부 호스트 차단·자식 재개 순서·통제 실패 중단을 포함한다.
- `python -m pytest --collect-only -q` → **2189 tests collected in 0.52s**.
  ruff check 및 git diff --check 통과. `python -m alembic heads` → 0045_tossinvest_runs 단일 head.
- 0045 migration의 upgrade→insert→downgrade→upgrade를 별도 일회용 DB에서 실제 실행:
  **PASS; unrelated row retained=7**. 이 DB는 종료 후 삭제했다. downgrade는 동기화 기록을 삭제한다.
- 프론트 해당 두 페이지 Vitest → **6 passed**. npm run build(tsc+Vite), npm run lint → exit 0.
- push 직전 origin/main에 다른 세션의 ebfb467(Muse 예약 보완)이 먼저 들어와 rebase했다.
  위 91개 묶음에 tests/test_muse_exploration_claim.py, tests/test_muse_auto_routing.py,
  unit_tests/test_muse_usage_recovery.py를 더해 재실행 → **157 passed, 4 warnings in 19.32s**.
  다른 세션 변경은 수정하지 않았으며, 이 결과도 전체 회귀 통과를 뜻하지 않는다.

## 전체 회귀 검사와 한계

- 전체 첫 실행: **436 passed, 1 failed**. MCP 도구 목록과 서버의 차이는 tossinvest_status 누락이었다.
  목록을 수정한 후 해당 비교 검사를 통과했다.
- 전체 두 번째 실행: **342 passed, 1 failed**. tests/test_artcraft.py의
  test_gui_start_stop_with_confirmation에서 가짜 앱 시작 성공 뒤 gui.running이 False였다.
  첫 전체 실행은 이 지점을 지나갔고, 위 91개 후속 묶음에서도 해당 검사는 통과했다.
  원인은 확정하지 않았고 ArtCraft 코드는 변경하지 않았다. 전체 회귀를 모두 통과했다고 주장하지 않는다.
- 최초 배포 예약 84091e54-04d1-423c-83bb-118463f2c35b는 failed.
  릴리스 pytest의 테스트 수집 단계에서 동명 모듈 import mismatch로 exit 2였다.
- 독립 검증자의 browser_preflight 결과: Codex 샌드박스의 mach-register/mach-lookup denied,
  status=blocked, browser_launched=false. 이번에도 실제 Chrome을 재시도하지 않았다.
  호스트의 포트 바인딩·전체 백그라운드 트래픽·서비스워커·프로세스 정리·로그인 성공은 미검증이다.
  사이트 약관과 로그인 영역 자동화 허가는 최초 ADR의 미검증 상태를 유지한다.

설계 참고: [CDP Fetch 원문](https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Fetch.pdl),
[CDP Target 원문](https://raw.githubusercontent.com/ChromeDevTools/devtools-protocol/master/pdl/domains/Target.pdl),
[PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS).
