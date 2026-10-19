# 따까리 보안 강화 구현 (2026-10-19)

## 1. 개요 및 배경

사용자의 최신 보안 보완 지시:
- "너무 엄격하게 해서 에이전트 행동에 락이 걸려 아무것도 못하는 상황은 피하고 최악만 면하자는 마인드"
- "지금 내 비밀번호 하나만 알면 모든 게 되므로 비밀번호가 유출될 일이 없어야 하고 인증인가 없이 동작하는 게 없어야 한다"
- "쓰는데 불편함 없이 잘 모르는 외부인이 작업공간을 파괴하지 못하게"
- "CSRF·SQL 인젝션 같은 기본 외부 공격에 허점이 최대한 없어야 한다"

종전 감사·계획 단계를 넘어 실제 코드 결함을 TDD 방식으로 보완하고, 정상 동작 대조군과 함께 회귀 테스트를 완료했다.

## 2. 세부 구현 및 보완 내용

### 1) 세션 범위 및 인가 격리 (WBS SEC-01, SEC-02)
- **`src/ttakkari/api/agent.py`**:
  - `GET /api/agent/sessions`: 호출 세션의 풀이 `pool == "work"`인 경우 회사 세션만 필터링하여 반환. 개인 세션 목록 노출 차단.
  - `GET /api/agent/sessions/{session_id}`: 호출 세션이 `work` 풀일 때 개인 세션(`s.pool != "work"`) 상세 조회를 시도하면 HTTP 403 Forbidden 거부.
- **`src/ttakkari/api/stocks.py`**:
  - `agent_access`: 세션의 풀이 `work`일 때 개인 주식/토스증권 정보 조회를 HTTP 403 Forbidden으로 거절하여 회사 세션으로부터 개인 금융 자산 격리.

### 2) 비밀번호 변경 세션 폐기 및 로그인 검증 보호 (WBS SEC-06)
- **`src/ttakkari/cli.py` (`_upsert_password`)**:
  - 비밀번호 변경(`set-password`) 시 활성화되어 있던 기존 모든 `RefreshToken`의 `revoked_at`을 `utcnow()`로 즉시 갱신하여 이전 세션 일괄 폐기.
- **`src/ttakkari/api/auth.py` (`refresh`)**:
  - 세션 갱신 시 `Credential.updated_at`과 `RefreshToken.created_at`을 비교하여 비밀번호 변경 이전에 발급된 구 세션 토큰은 즉시 폐기하고 쿠키 삭제 헤더(`Max-Age=0`)와 함께 HTTP 401 반환.
  - `Credential` 행과 `RefreshToken` 행을 `with_for_update()`로 직렬화하고 재조회하여 동일 쿠키 동시 갱신 및 비밀번호 변경과의 경쟁 조건 차단.
- **`src/ttakkari/security/auth.py` (`client_ip`, `LoginLimiter`) & `src/ttakkari/api/auth.py` (`login`)**:
  - **신뢰 프록시 경계 강화 (`client_ip`)**: `trust_proxy_ip_header`(운영 `CF-Connecting-IP`)가 설정되어 있더라도 소켓 직접 피어(`request.client.host`)가 로컬 신뢰 프록시(`127.0.0.1`, `::1`, `localhost`)일 때만 전달 헤더의 첫 IP를 신뢰하고, 미설정이거나 비-loopback 직접 연결이면 전달 헤더 위조(spoofing)를 무시하고 소켓 IP를 사용.
  - **설계 대안 비교 및 선택**:
    - *대안 A (기각 — 실패 응답 sleep + 서브넷 잠금)*: 응답 전 `asyncio.sleep`만 두면 동시 요청 시 Argon2 검증이 병렬/연속 수행되어 추측 처리량과 CPU 부하를 막지 못하고, `/24` 서브넷 장기 잠금은 공용 NAT 대역의 정상 사용자까지 15분간 차단함.
    - *대안 B (채택 — 유한 Admission + 단일 검증 동시성 + 실패 전용 전역 검증 토큰 버킷 + IP별 제한)*:
      1. **IP별 실패 제한**: IP당 15분(`login_lock_seconds=900`) 내 5회(`login_max_failures=5`) 실패 시 15분간 즉시 429 차단, 추적 IP 맵은 `MAX_TRACKED_IPS=4096` 상한 및 주기적 만료 정리로 메모리 제한.
      2. **유한 In-flight Admission (`login_max_inflight=4`) & 동시성 1 (`login_verify_concurrency=1`)**: 로그인 엔드포인트(`/api/auth/login`)에서 동시에 검증 중이거나 대기 중인 요청 수를 최대 4개로 제한하고 초과 동시 요청은 DB 조회나 해시 검증 전에 즉시 `429 Too Many Requests`(`Retry-After: 1`)로 load shedding. 실제 Argon2 검증(`verify_password`)은 `Semaphore(1)` 안에서 `asyncio.to_thread`로 실행하여 이벤트 루프 블로킹을 막고 동시 검증 스레드를 1개로 제한.
      3. **실패 검증 토큰 버킷 (`login_verify_burst=8`, `login_verify_rate_per_second=1.0`)**: 다중/분산 IP가 IP별 5회 제한을 우회하더라도 프로세스 전체에서 단기 버스트 8회까지만 실패 검증을 허용하고, 소진 시 검증 전 즉시 429(`Retry-After: 1`)로 거절. 올바른 비밀번호로 로그인 성공(`limiter.success`)하거나 자격증명 미설정(503)인 경우에는 토큰을 즉시 환급(`refund`)하여 평시 정상 로그인에는 지연이나 예산 소모가 전혀 없음. 공격이 멈추면 `1.0초`(`1 / login_verify_rate_per_second`) 후 토큰이 재충전되어 수분간 계정이 잠기는 현상 없이 즉시 정상 로그인 회복.
      4. **격리 범위**: 이 제한은 `/api/auth/login`에만 적용되며 `/api/auth/refresh`, 일반 Bearer API, 에이전트 Run/MCP API에는 전파되지 않음.

### 3) 민감 정보 마스킹 확장 (WBS SEC-05)
- **`src/ttakkari/runs/events.py` (`append`)**:
  - `RunEvent` DB 저장 직전 `scrub_value(payload)`를 적용하여, 실행 도구 인자·출력·에러에 포함된 비밀번호, 토큰, API 키, 쿠키, Authorization 값이 DB에 평문 저장되지 않도록 자동 마스킹.
- **`src/ttakkari/oplog/scrub.py`**:
  - 토큰 마스킹 정규식 패턴에 따까리 내부 토큰 접두사(`r"\bttk_[A-Za-z0-9_\-]{16,}"`) 추가.

### 4) CSRF 방어 보완
- **`src/ttakkari/api/auth.py` (`_check_csrf`)**:
  - 브라우저 쿠키(`ttk_refresh`)가 전송되는 상태 변경 엔드포인트(`/api/auth/refresh`, `/api/auth/logout`)에 CSRF 방어 적용:
    - 요청에 `Origin` 헤더가 있는 경우 `cors_origins` 허용 목록 검증 (위반 시 403).
    - `Origin`이 없을 때 `Sec-Fetch-Site: cross-site` 요청 차단 (위반 시 403). 허용된 `Origin`과 `Sec-Fetch-Site: cross-site`가 함께 온 정상 교차 사이트 프론트 배포는 허용(200).
    - `Origin`이나 `Sec-Fetch-Site` 헤더가 없는 비브라우저 API 클라이언트(curl, Python, 단축어, MCP)는 정상 허용하여 호환성 유지.

### 5) 셸/명령 가드 Fail-Closed 보완 (WBS SEC-04)
- **`src/ttakkari/security/claude_hook.py`**:
  - PreToolUse 가드 훅 실행 중 예외(malformed JSON 등) 발생 시 단순히 exit 0으로 통과(fail-open)시키던 결함을 수정하여, 안전을 위한 명시적 deny JSON 출력 및 차단(fail-closed) 적용.

### 6) SQL Injection 및 XSS / 파일 경로 격리 검증
- SQLi는 이번 변경 파일과 `src/ttakkari/api/system.py`의 시간 버킷 쿼리를 범위로 살폈다. 쿼리의 동적 버킷 크기는 서버측 고정 선택지이고, 외부 값은 `:cutoff`로 바인딩된다. 전체 DB 쿼리 무결성 감사를 했다는 뜻은 아니다.
- XSS는 `api/artifacts.py`의 sandbox CSP·`nosniff` 응답과 `tests/test_api_artifacts.py`의 inline content 대조를 확인했다. 모든 사용자 제공 HTML 전체에 대한 브라우저 침투시험은 하지 않았다.

## 3. 검증 결과 및 증거

- **보안 회귀 테스트 (`tests/test_password_session_revocation.py` 외 8개 파일, 총 `130 passed, 2 skipped, 2 xfailed`)**:
  - `tests/test_password_session_revocation.py`:
    - 비밀번호 변경 후 기존 refresh 401 거절 및 `Max-Age=0` 삭제 쿠키 헤더 확인, 동시 refresh 경쟁 시 1건만 성공(`[200, 401]`), 비밀번호 변경과 refresh 경쟁 후 활성 토큰 0건 확인.
    - 24개 분산 IP 동시 공격 + 16개 분산 IP 연속 공격 시 실제 `verify_password` 동시 실행 수(`max_concurrency == 1`), 동시 파도 호출 상한(`<= login_max_inflight=4`), 누적 버스트 호출 상한(`<= login_verify_burst=8`), 초과 요청 429 load shedding(`Retry-After >= 1`) 계측.
    - 로그인 포화 중에도 기존 `/api/auth/refresh`, `/api/auth/me`, `/api/agent/sessions` 정상 통과(200 OK) 대조군 확인.
    - 공격 중단 약 1초 후 정상 비밀번호 로그인 즉시 회복(200 OK) 및 평시 연속 정상 로그인(12회) 무차단 확인.
    - `trust_proxy_ip_header=None` 및 비-loopback 직접 연결 피어(`203.0.113.77`)에서 `CF-Connecting-IP`/`X-Forwarded-For` 헤더 위조(spoof) 시 무시되고 소켓 IP 기준으로 5회 실패 후 429 차단됨을 계측.
  - `tests/test_agent_scope_auth.py`: work 세션의 목록 격리, 상세 조회 403, 주식 403, 비정상/타 audience 토큰 401 및 personal 세션 정상 대조군 검증.
  - `tests/test_csrf_defense.py`: 악성 Origin 403, Origin 없는 cross-site 403, 허용 Origin + cross-site 200, 비브라우저 200 검증.
  - `tests/test_runs_events_masking.py`: RunEvent 저장 전 비밀 마스킹, DB 이벤트 목록 및 SSE 스트림 canary 부재, 일반 진단 메시지 보존 검증.
  - `tests/test_claude_hook_fail_closed.py`: 정상 도구(Read/Bash/Edit/pytest) 허용, 위험 명령 차단, malformed 입력 시 fail-closed deny 검증.
  - 기존 회귀: `tests/test_api_auth.py`, `tests/test_guard.py`, `tests/test_tossinvest.py`, `tests/test_api_artifacts.py` 모두 통과.
- **코드 스타일 및 정적 점검**:
  - 변경 파일 `ruff check`: `All checks passed!`
  - `git diff --check`: 통과.
  - `auth_scan.py src/ttakkari/security/auth.py`: `total: 0 finding(s)`.

## 4. 한계·잔여 위험 및 후속 절차

- **이전 이력 보존**: 최초 구현 커밋 `9477a743b5ce93c3da60c08fc13e1148c2268070`은 독립 검증에서 4건의 결함(허용 Origin+cross-site 403, 401 쿠키 삭제 헤더 누락, 동일 refresh 동시 2회 회전 성공, 미사용 전역 실패 리스트 누적)과 다중 IP 로그인 제한 미완으로 실패 판정되었고, 당시 원격 `main`은 `729a0a9d48c60fc8de1b5ff67872e4894c082384`에 머물러 있었다. 이번 커밋은 독립 검증 수정본(8개 파일)을 보존·이어받아 분산 IP 로그인 검증 제한과 대조군 테스트를 완성한 출고 후보다.
- **프로세스 범위(`per-process`) 한계**: `LoginLimiter`는 프로세스 인메모리 상태다. 따까리 백엔드는 단일 Uvicorn 워커로 구동되며 세대별 배포(`generations`)에서도 Caddy 프록시가 단일 active 슬롯으로만 신규 HTTP 요청을 전달하므로 평시에는 단일 지점으로 동작한다. 단, 세대 전환이나 프로세스 재시작 시점에는 인메모리 카운터·버킷이 초기화된다.
- **공격 포화 시 일시적 Load Shedding과 복구 시간**: 분산 공격이 진행되는 도중 동시 요청 상한(`login_max_inflight=4`)이나 실패 검증 버스트(`login_verify_burst=8`)가 포화되면 정상 사용자의 신규 로그인 요청도 요청 단위로 짧은 `429 Too Many Requests`(`Retry-After: 1`)를 받을 수 있다. 그러나 수분간 계정을 잠그는 전역 락이 아니므로 공격 요청이 잦아들면 `1.0초`(`1 / login_verify_rate_per_second`) 내에 검증 토큰이 자동 회복되어 정상 로그인이 재개된다. 이미 로그인된 세션의 자동 `/api/auth/refresh`와 일반/에이전트 API는 로그인 검증 포화의 영향을 받지 않는다.
- **토큰·세션 폐기 범위와 미검증 영역**:
  - 비밀번호 변경(`set-password`)은 모든 활성 `RefreshToken`을 즉시 폐기하고 이후 갱신을 차단한다.
  - 그러나 이미 발급된 무상태(stateless) 사용자 access JWT는 만료 시각(기본 설정 15분, 운영 override 미열람)까지 유효하며, 에이전트 Run 토큰(`aud="ttakkari-agent"`)은 진행 중인 Run이 중단되지 않도록 해당 Run 만료(`run_timeout_seconds + 600`)까지 유효하다.
  - 원격제어(`remote_desktop`)의 일회성 WS 티켓(30초), 재인증 grant(10분), 이미 연결된 WebSocket 세션(최대 2시간)을 비밀번호 변경 시 즉시 회수하는 연동은 이번 범위에 포함하지 않았으며 실제 브라우저·WebSocket·실제 엔진 Run·운영 DB/설정은 건드리지 않았다.
- **배포 보류**: 이번 단계는 출고 후보 커밋 및 원격 `main` push까지만 수행하며, 운영 배포(`request_backend_deploy`)는 별도 독립 검증 작업자의 최종 확인 후 진행한다.

## 5. 7cf7c45 독립 검증 결과 (2026-10-19)

- 결함 1건 수정: 로그인 중 `needs_rehash`가 참이면 ORM 대입이 `Credential.updated_at`(onupdate)을 올려, 다른 기기의 정상 refresh 세션까지 "비밀번호 변경"으로 폐기됐다. 재해시는 `updated_at`을 유지하는 UPDATE로 바꾸고 회귀 테스트를 추가했다.
- 유지한 정책(사용자 결정 필요): 같은 IP에서 연속 5회 실패하면 올바른 비밀번호도 15분간 429다(기준선 설정 그대로). 지속적인 분산 공격 중에는 로그인만 429가 날 수 있고 refresh와 에이전트 API는 영향이 없다.
- 전체 테스트 2278 passed, 변경 파일 ruff 통과. 저장소 전체 ruff 75건은 기준선에 있던 다른 파일의 것이다.

## 독립 재검증 후속 (2026-10-19)

- 기준점: 시작 시 `git pull --ff-only` 결과 `Already up to date`; `main`/`origin/main`은 `d9c8eb7`(efcd1ab 이후 NotebookLM 문서 커밋). 처음에는 변경 없음이었고 이 세션은 `.env`, 운영 DB/설정, 실제 사용자 자료를 열지 않았다. 서비스/launchctl/운영 공격 시험도 하지 않았다.
- 배포 상태는 앞선 확인 영수증 `4e5dcb2c-7345-4b94-9a64-935019f25e16` 기준 `draining`, activated revision `efcd1ab`: 신규 요청 전환은 됐지만 구세대 퇴역 완료로 해석하지 않는다.
- 새 범위 결함 3건을 모두 합성 fixture로 재현해 최소 수정했다.
  1. `GET /api/agent/artifacts`가 work 호출자의 세션을 확인한 뒤 전체 활성 Artifact를 반환했다. 동일 PostgreSQL 앱 라우트 실행에서 개인 연결/다른 workspace/소유 불명 orphan 산출물이 work 결과에 섞이는 것을 확인했다. work는 현재 workspace의 work-session Artifact와 사용자가 명시적으로 등록/업로드한 공용 파일만 보며, `PATCH`도 같은 조건을 적용한다. personal 호출 동작은 그대로 둔다.
  2. `/api/agent/forget`이 전역 UUID로 CtxMemory를 찾아 scope 없이 아카이브했다. work 토큰으로 합성 personal 기억이 `200 {"forgotten": true}`가 되는 것을 확인했다. 이제 work scope가 아닌 ID는 `404`; work 기억 삭제는 계속 가능하고 personal 기억은 보존된다.
  3. `POST /api/agent/entities`만 `entity_detail`에 기본 `all` 범위를 사용했다. API를 통해 만들 수 있는 합성 cross-scope relation에서 work 응답이 personal 엔터티명을 반환했다. POST 응답도 work scope로 제한했고 GET과 같은 동작을 한다.
- 격리 테스트 DB: `ttk_security_audit_20261019_01` (기존 `ttakkari_test`와 운영 DB를 사용하지 않음). 수정 전 RED 결과: artifacts는 personal+타 workspace 행 노출, forget은 personal 합성 기억 아카이브, entities는 personal target/note 반환. 정상 대조군: work 산출물·사용자 명시 공용 파일 표시/이동 가능, work 기억 삭제 가능, personal 세션 artifact 목록은 기존 범위 유지.
- 재현·검증 명령과 결과:
  - `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_security_audit_20261019_01 .venv/bin/pytest -q tests/test_agent_scope_auth.py tests/test_csrf_defense.py tests/test_runs_events_masking.py tests/test_claude_hook_fail_closed.py tests/test_api_auth.py tests/test_api_artifacts.py tests/test_guard.py` → `99 passed, 2 xfailed in 14.94s`.
  - `.venv/bin/ruff check src/ttakkari/api/agent.py tests/test_agent_scope_auth.py` → `All checks passed!`; `git diff --check` 통과.
  - `TTAKKARI_TEST_DB=postgresql+asyncpg://localhost/ttk_security_audit_20261019_01 .venv/bin/pytest -q tests/test_password_session_revocation.py` → `10 passed in 4.66s`.
  - `tests/test_guard.py` strict xfail 2건은 변수 확장 민감 경로 읽기 및 일반 Bash 외부 쓰기다. 함수 수준 한계로 기록하며 OS 샌드박스 우회 입증으로 승격하지 않는다.
  - 운영 설정 없이 합성 키로 앱 모듈을 import하고 `app.openapi()`의 `/api/agent` 경로를 집계: `agent_http_operations=75 operations_without_openapi_security=0`, unguarded `[]`. 이는 라우트의 인증 선언 확인이지 scope 인가, 실제 HTTP 공격, WebSocket 보호의 완전성 검증은 아니다. OpenAPI 생성 중 public-share duplicate operation ID 경고 2건도 관측했다.
  - SQLi는 합성 query `%' OR 1=1 --`가 artifact 필터에서 빈 결과임을 실제 라우트/DB로 확인. 추가로 `api/system.py` raw query는 bucket 크기 60/600초 서버 고정값과 `:cutoff` 바인딩을 코드에서 확인했다. 전 SQL 전체를 검증했다고 주장하지 않는다.
- 남은 범위·기록: `Memo`, `CalendarEvent`는 모델에 personal/work scope가 없어 에이전트 API가 현재 공통 자산처럼 조회/생성/수정한다. 범위 소유권 필드와 기존 데이터 분류가 없으므로 기능 전체를 막지 않고 SEC-01/02 공유 자산 정책 미해결로 남긴다. parent/worker/보관 Artifact 전수 변형도 미검증이다. SEC-03/08/10/11은 실제 엔진·MCP·변환기·호스트 경로, SEC-04는 hook 외 OS 실행 경계, SEC-05는 모든 stdout/error/일반 로그, SEC-06은 access/agent 토큰 만료·원격 WS 허가 폐기, SEC-07 dependency advisory 도달성/갱신, SEC-09 전체 소유자 CSP, SEC-12 자동 게이트, SEC-13 사고대응, SEC-14 전체 독립 종결은 계속 미완이다. WBS SEC-01~14 완료 표시는 바꾸지 않는다.
- 정상 사용 영향: 개인 에이전트의 전역 Artifact 목록은 변경하지 않는다. work 세션은 자기 workspace의 회사 산출물과 명시적으로 업로드/등록한 파일을 계속 보고 분류 이동할 수 있다. 다른 workspace 및 scope 불명 agent artifact는 숨긴다. work memory의 정상 삭제 및 이름 조회는 유지한다.
- 이전 worker의 `2278 passed`는 그쪽 보고로 남기며 이번 직접 실행과 합산하지 않는다.
