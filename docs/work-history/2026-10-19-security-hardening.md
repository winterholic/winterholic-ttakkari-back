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

### 2) 비밀번호 변경 세션 폐기 및 로그인 락 개선 (WBS SEC-06)
- **`src/ttakkari/cli.py` (`_upsert_password`)**:
  - 비밀번호 변경(`set-password`) 시 활성화되어 있던 기존 모든 `RefreshToken`의 `revoked_at`을 `utcnow()`로 즉시 갱신하여 이전 세션 일괄 폐기.
- **`src/ttakkari/api/auth.py` (`refresh`)**:
  - 세션 갱신 시 `Credential.updated_at`과 `RefreshToken.created_at`을 비교하여 비밀번호 변경 이전에 발급된 구 세션 토큰은 즉시 폐기하고 HTTP 401 반환 (이중 방어).
- **`src/ttakkari/security/auth.py` (`LoginLimiter`)**:
  - 전체 사용자 대상 전역 잠금(`_global`) 로직을 제거하여, 공격자가 타 IP로 무차별 대입을 시도하더라도 정상 사용자의 IP가 잠기지 않도록 개선 (NAT/IP 광역 락 및 DoS 방지).

### 3) 민감 정보 마스킹 확장 (WBS SEC-05)
- **`src/ttakkari/runs/events.py` (`append`)**:
  - `RunEvent` DB 저장 직전 `scrub_value(payload)`를 적용하여, 실행 도구 인자·출력·에러에 포함된 비밀번호, 토큰, API 키, 쿠키, Authorization 값이 DB에 평문 저장되지 않도록 자동 마스킹.
- **`src/ttakkari/oplog/scrub.py`**:
  - 토큰 마스킹 정규식 패턴에 따까리 내부 토큰 접두사(`r"\bttk_[A-Za-z0-9_\-]{16,}"`) 추가.

### 4) CSRF 방어 보완
- **`src/ttakkari/api/auth.py` (`_check_csrf`)**:
  - 브라우저 쿠키(`ttk_refresh`)가 전송되는 상태 변경 엔드포인트(`/api/auth/refresh`, `/api/auth/logout`)에 CSRF 방어 적용:
    - 요청에 `Origin` 헤더가 있는 경우 `cors_origins` 허용 목록 검증 (위반 시 403).
    - `Sec-Fetch-Site: cross-site` 요청 차단 (위반 시 403).
    - `Origin`이나 `Sec-Fetch-Site` 헤더가 없는 비브라우저 API 클라이언트(curl, Python, 단축어, MCP)는 정상 허용하여 호환성 유지.

### 5) 셸/명령 가드 Fail-Closed 보완 (WBS SEC-04)
- **`src/ttakkari/security/claude_hook.py`**:
  - PreToolUse 가드 훅 실행 중 예외(malformed JSON 등) 발생 시 단순히 exit 0으로 통과(fail-open)시키던 결함을 수정하여, 안전을 위한 명시적 deny JSON 출력 및 차단(fail-closed) 적용.

### 6) SQL Injection 및 XSS / 파일 경로 격리 검증
- **SQLi**: 모든 DB 쿼리가 SQLAlchemy ORM 파라미터 바인딩 또는 정적 쿼리로 구성됨을 확인. 외부 입력 직접 문자열 보간 없음.
- **XSS/HTML 미리보기**: `api/artifacts.py`의 `INLINE_HEADERS`에 `Content-Security-Policy: sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'`가 적용되어 있으며, 자바스크립트 미실행 및 opaque origin 격리가 유지됨을 확인.

## 3. 검증 결과 및 증거

- **새로 작성된 회귀 테스트 5개 (10개 테스트 케이스)**:
  - `tests/test_runs_events_masking.py`: RunEvent 저장 전 비밀 마스킹 및 일반 필드 보존 검증 (PASSED)
  - `tests/test_agent_scope_auth.py`: work 세션의 목록 격리, 상세 조회 403, 주식 403 및 personal 세션 정상 대조군 검증 (PASSED)
  - `tests/test_password_session_revocation.py`: 비밀번호 변경 후 refresh 거절, 새 로그인 갱신 정상 동작, IP별 락 분리 검증 (PASSED)
  - `tests/test_csrf_defense.py`: evil Origin 403, cross-site 403, 정상 Origin 200, 비브라우저 200 검증 (PASSED)
  - `tests/test_claude_hook_fail_closed.py`: 정상 도구 허용, 위험 명령 차단, malformed 입력 시 fail-closed deny 검증 (PASSED)
- **기존 회귀 테스트**:
  - `tests/test_api_auth.py`: 21 passed
  - `tests/test_guard.py`: 31 passed, 2 xfailed
  - `tests/test_tossinvest.py` & `test_api_artifacts.py`: 57 passed, 2 skipped
- **코드 스타일**:
  - `ruff check`: All checks passed.

## 4. 후속 절차 및 배포 안내

- 본 작업자는 변경 사항을 main에 커밋 및 푸시 완료함.
- 독립 보안 검증 작업자(verification)가 별도 신선한 컨텍스트에서 최종 확인을 수행한 뒤 상시 방침대로 자동 배포를 진행하도록 배포 예약(`request_backend_deploy`)은 직전 상태로 인계함.
