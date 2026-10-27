# 수집 0건 신고: 진단 경로 보완 (미복구)

## 원 요청과 관측 범위

2026-10-27 사용자가 아침에 의뢰한 수집이 처리되지 않았다고 신고했다. 첨부 화면의 임무는
`디자인 시스템·UI/UX 지식 백과사전 구축`이며 1회차, 수집/적재 및 모든 표시 지표가 0인데 `일부 수집`이다.
첨부 자체는 해당 시점의 화면 증거다. 운영 DB의 해당 mission ID/생성 시각/검색 이력은 아직 조회하지 못했다.

## 확인한 사실

- MCP `host_operation_start(kind="runtime_report")` 실행 결과 운영 active revision은
  `78c4a2e7cde02536327eefd7184b64826d1726aa`, generation은
  `f84f2b63-79aa-480a-bb39-c0efea420f2b`였다. 이 관측은 서버 기동/소유권만 확인하며 수집 성공을 뜻하지 않는다.
- 위 revision `collector/missions.py:view`는 모든 job 종료 및 적재 대기 없음 상태에서
  `counts["acquired"] == 0`이면 `partial`을 반환한다. 따라서 화면의 일부 수집은 실제 일부 자료 확보의 증거가 아니다.
- 해당 revision의 일반 주제 검색은 `duckduckgo_html`을 사용한다. papers 주제의 영문 검색만 arXiv를 추가한다.
- 원 플랫폼 세션 `2f5888bb-5d18-4690-946c-9356cfd1dd55`의 마지막 기록은 운영 수집/검색/UI 인수가 미완료인 failed/blocked였다.
  과거 중첩 환경 실패를 현재 운영 장애의 원인으로 승격하지 않는다.
- 운영 오류 검색 `collection`, `mission`, `search_provider_failed`는 해당 관측 창에서 0건이다.
  이는 오류가 없었다는 증명이 아니다. 제품 DB의 job error/mission gaps는 해당 오류 색인과 별개다.

## 이번 변경

기존 개인 전용 `runtime_report`에 `collection` 읽기 전용 진단을 붙인다. 최근 owner 임무 20개에 한해
제목/ID/상태/시각, 검색 종료 이유/사용 횟수, 첫 5개 gap 코드, 현재 회차 job의 상태/단계/오류 코드/시도 수/다음 재시도,
전체 회차의 실제 acquired/accepted/failed/blocked/imported distinct item 수를 반환한다.
job 그룹은 200개까지 표시하고 잘림 여부를 명시한다. 본문/원본 envelope/전체 plan/오류 메시지/접속 정보는 조회하지 않는다.
DB 쓰기, 수집 재실행, 정책/예산 완화, 인증 변경은 없다.

## 실행 증거

- 누락 함수를 호출하는 회귀 2개가 AttributeError로 RED인 것을 확인한 뒤 구현했다.
- gap 코드 누락 회귀 2개가 KeyError로 RED인 것을 확인한 뒤 좁게 추가했다.
- `PYTHONPATH=src ../back/.venv/bin/python -m pytest unit_tests/test_collection_runtime_report.py unit_tests/test_collection_adapters.py unit_tests/test_collection_plan.py unit_tests/test_collection_research.py -q`
  → `42 passed in 1.05s` (로그 `review/collection-diagnostics-20261027/unit-tests.txt`).
- 새 진단 검사는 가짜 결과의 SQL projection 확인 2개와 실제 in-memory SQLite SELECT/집계 1개다.
  SQLite 검사는 중복 적재 영수증의 distinct 집계, 다른 tenant 제외, 이전 회차 오류 제외 및 민감 본문 비노출을 확인한다.
  PostgreSQL 운영 스키마/HTTP 통합/실제 수집 복구 증거로 확대하지 않는다.
- 변경 파일 Ruff 및 `git diff --check` 통과. 테스트는 수집 임무를 새로 실행하지 않는다.

## 검색 재현: 운영 원인 미확정

동일 공개 검색어 `디자인 시스템 UI UX design system documentation`를 Codex 명령 환경의 실제
`adapters.search_web`로 실행한 첫 관측은 예외 없이 0건이었다. 이어 같은 공급자 응답을 확인한 한 관측은
result__a 링크 10개였고, 다음 관측은 명시적인 `No results found` HTML이며 같은 바이트를 parser에 재생하면 0건이었다.
마지막 원문은 `review/collection-diagnostics-20261027/search-public-response.html`에 보존했다.
관측은 공급자 응답이 달랐음을 보여주며, parser 버그/봇 차단/운영 임무 실패의 원인으로 단정하지 않는다.
추가 무작정 반복 호출은 중단했다.

## 접근 제약과 다음 동작

- 승인된 headless browser는 `/collect` 접근 시 `/login?next=%2Fcollect`로 이동했다. 로그인/권한 우회는 하지 않았다.
  close 호출은 `Target page, context or browser has been closed`로 반환됐다. 재시도하지 않았다.
- 명령 환경에는 TTK_ORCH_TOKEN/TTK_API_URL이 없다. 인증 파일/보호 config/비밀은 읽지 않았다.
- 읽기 전용 `psql -X -w -d ttakkari`는 명령 환경의 기본 접속 대상 `127.0.0.1:49786`에서
  `fe_sendauth: no password supplied`였다. 운영 DB 실패를 입증하지 않으며 인증 우회/변경/반복 접속하지 않았다.
- 이번 진단 변경을 main에 push하고 배포를 예약한다. 수집 0건 자체는 아직 복구하지 않았다.
- 배포 활성화 뒤 MCP runtime_report의 collection에서 해당 제목의 실제 ID·실패 단계·오류 코드·시도 수를 확인한다.
  그 관측을 근거로 검색/정책/수집/적재 중 실제 차단 단계만 교정하고, 원 임무의 안전한 재실행과 적재/검색까지 이어간다.
