# ADR 0051: Google 계정 로그인·세션 공통 모듈

- **상태**: Accepted (2026-10-19)
- **관련**: ADR 0045(NotebookLM 연동), `docs/google-account/README.md`(새 기능을 붙이는 방법)
- **범위**: NotebookLM 에 묶여 있던 Google 로그인·세션 관리를 `ttakkari/google_account/` 로 분리했다. consumer 는 NotebookLM 하나뿐이다.

## 1. 배경

나중에 Google 계정을 쓰는 다른 기능(MCP·화면)이 생긴다. 로그인 창, 전용 Chrome 프로필, 세션 쿠키 저장, 만료 알림은 Google 계정의 성질이지 NotebookLM 의 성질이 아니다.
기능마다 이를 복제하면 같은 계정을 기능 수만큼 다시 로그인해야 하고, 보안 불변 조건(0600/0700, 마스킹, 루프백 CDP)이 갈라진다.

## 2. 결정

### 2.1 계정 수준과 기능 수준을 나눈다

| 수준 | 소유 | 담는 것 |
|---|---|---|
| 계정 | `google_account` 모듈, 표 `google_accounts` | 로그인 창 수명주기, 세션 파일, 전용 프로필, 상태 `login_required → session_saved → ready ⇄ reauth_required`, 재인증 알림 1회, 브라우저 실행 계측 |
| 기능 | 각 consumer 패키지, 자기 표 | 쿨다운(429), 보호 신호 `blocked`, 접속 확인 이력, 기능 고유 정책 |

- 세션 만료는 어느 기능이 감지하든 계정 상태 `reauth_required` 로 올라가고 모든 기능이 같이 본다. 알림은 DB 에서 `reauth_notified_at IS NULL` 인 행을 먼저 갱신한 요청만 보내므로 동시 감지에도 계정당 한 번이다.
- 쿨다운과 `blocked` 는 기능 상태다. 한 기능의 429 나 보호 신호가 다른 기능을 막지 않고, 계정 세션 상태에도 번지지 않는다. 계정 수준 `blocked` 상태는 만들지 않았다. 보호 신호는 요청을 보낸 기능의 트래픽에 대한 반응이라 그 기능만 멈추는 편이 맞다. 로그인 창에서 만난 보호 화면은 로그인 시도 결과(`failed`)로만 남고 세션을 저장하지 않는다.
- 성공한 요청은 `mark_ok` 로 계정을 `ready` 로 올리되 `reauth_required` 는 덮어쓰지 않는다. 늦게 끝난 성공이 먼저 기록된 만료를 지우면 안 된다.

### 2.2 consumer 등록

`google_account.registry.Consumer` 하나를 정의해 등록한다. 정의하는 것은 앱 화면(`login_url`, `app_hosts`), 쿠키 도메인 범위(`cookie_domains`, `required_cookies`), 상태 요약 `describe`, 선택 동작(`verify`, `resume`), 세션 변경 훅(`on_session_saved`, `on_session_removed`, `on_reauth`)이다.
등록은 `registry._load_builtin()` 의 import 한 줄이다. 로그인 창이 여는 화면과 완료 판정 호스트는 로그인을 시작한 기능이 정한다. 기능을 지정하지 않으면 첫 등록 기능을 쓰고, 등록 기능이 없으면 `myaccount.google.com` 을 연다.

세션에는 등록된 모든 기능의 도메인 범위 쿠키가 담긴다. 나중에 등록한 기능은 이전 로그인에서 범위가 빠졌을 수 있어 기능 상태에 `covered` 를 싣는다. false 이면 그 기능의 화면에서 로그인을 한 번 더 하면 된다(같은 프로필이라 Google 쪽은 이미 로그인된 상태다).

### 2.3 계정 모델과 저장 위치

- 키는 `account_id`(`^[a-z0-9][a-z0-9_-]{0,31}$`). 지금은 `primary` 하나이고, 두 번째 계정은 같은 구조로 추가된다. 세션 파일, 프로필, 로그인 창 잠금, DB 행이 모두 계정별이다. 같은 계정에는 로그인 창을 하나만 연다. 다른 계정끼리는 동시에 열 수 있다.
- 세션: `<home>/google-accounts/<account_id>/storage_state.json`(0600), 디렉터리 0700, 임시 파일에 쓴 뒤 원자적 교체.
- 프로필: `<home>/google-accounts/<account_id>/chrome-profile/`. `TTAKKARI_NOTEBOOKLM_PROFILE` 을 명시한 환경은 `primary` 에 한해 그 경로를 그대로 쓴다.
- 세션 삭제는 `storage_state.json` 만 지운다. 프로필은 두어 다시 로그인할 때 Google 쪽 로그인 상태를 이어 쓴다(기존 동작 그대로).
- 암호화는 하지 않는다. 같은 OS 사용자의 다른 프로세스는 읽을 수 있다(ADR 0045 §5.3, ADR 0048).

### 2.4 이전과 무중단 배포

세대 배포 중 이전 세대가 같은 파일과 표를 읽고 쓴다. 그래서 이전은 추가와 복사로만 한다.

- DB: 마이그레이션 `0049_google_accounts` 가 새 표를 만들고 `notebooklm_accounts` 에서 계정 수준 상태를 복사한다(`ON CONFLICT DO NOTHING`). 옛 표와 컬럼은 건드리지 않는다. 쿨다운·`blocked` 행은 옛 표에 그대로 남아 NotebookLM 이 계속 쓴다. `ready/cooldown/blocked` 중 성공 이력이 있으면 계정은 `ready`, 없으면 `session_saved`.
- 세션 파일: 처음 쓸 때(`session.exists`) 옛 `<home>/notebooklm/accounts/<id>/storage_state.json` 을 새 위치로 옮기고 옛 경로에 새 파일을 가리키는 심볼릭 링크를 남긴다. 쿠키는 디스크에 한 벌만 있다. 이미 저장된 세션은 재로그인 없이 이어진다.
- 프로필: 로그인 창을 열 때, Chrome 이 그 프로필을 쓰고 있지 않으면 옛 `<home>/notebooklm-chrome` 을 `primary` 의 새 프로필 위치로 옮기고 옛 경로에 링크를 남긴다.
- 겹치는 동안 한계: 이전 세대가 옛 표에만 쓴 상태 변화(예: 이전 세대가 만료를 감지)는 새 세대의 계정 표에 보이지 않는다. 새 세대가 다음 요청에서 Google 의 응답으로 다시 알아낸다. 이전 세대가 로그인으로 옛 경로에 일반 파일을 다시 쓰면 `session.exists` 가 새 파일이 없을 때만 가져오므로, 새 파일이 있는 한 새 파일이 우선한다.
- 계약: `deploy/generation-contract.json` 의 `schema_revision` 을 `0049_google_accounts` 로, `online_migration_from` 에 `0048_game_active_build` 를 더했다. 표 추가만이라 온라인 적용이 안전하다.

### 2.5 REST·MCP

- 공통: `/api/google-accounts`(목록), `/{id}`(상태), `/{id}/login`·`login/complete`·`login/cancel`, `DELETE /{id}/session`, `/{id}/features/{feature}/verify|resume`. 소유자 전용.
- NotebookLM 기존 `/api/notebooklm/auth/*` 는 경로와 응답 키를 유지하고 같은 서비스로 위임한다(`account` 키만 추가).
- 에이전트: `GET /api/agent/google-accounts[/{id}]` 와 MCP `google_account_status`(읽기 전용, 개인 세션만)뿐이다. 로그인 창, 세션 삭제, 재개, 확인은 노출하지 않는다(기존 정책 유지).
- 알림: `PushKind.google_account`, 링크 `/settings#google-accounts`. 기존 `PushKind.notebooklm` 은 이미 쌓인 알림함 기록 때문에 남긴다.

### 2.6 공통으로 올린 것과 남긴 것

- 올림: 오류 계층(`google_account.errors`), 마스킹(`redact`), 세션 저장(`session`), 로그인 창(`login`), 계정 서비스(`service`), 계측(`metrics`: 브라우저 실행·로그인 결과·재인증 감지).
- 남김: NotebookLM 의 호출 정책(재시도, 쿨다운, 동시성), 소스 원장, job, 어댑터. `notebooklm.errors/redact/session` 은 기존 import 경로를 위한 얇은 재노출이다. `NotebookLMError` 는 공통 뿌리 `GoogleAccountError` 의 별칭이다.

## 3. 불변 조건 (그대로 유지)

- Google 보호장치를 우회하지 않는다. 자동화 감지를 끄지 않고, UA 를 위장하지 않고, 쿠키 DB 를 복호화하지 않고, 키체인을 열지 않는다.
- 1단계 로그인 창은 원격 디버깅·자동화 플래그 없이 연다. 2단계 headless Chrome 은 `--remote-debugging-address=127.0.0.1` 과 루프백 websocket 만 쓰고 바로 닫는다.
- 쿠키·토큰은 DB·로그·응답·예외·메트릭에 두지 않는다. 쿠키 이름만 보인다.
- 보호 신호가 보이면 세션을 저장하지 않고 안내한다. 기능의 보호 신호는 그 기능이 사람이 resume 할 때까지 멈춘다.
- 유료 API 경로는 없다.

## 4. 검증과 미검증

- 검증: 계정 격리, 세션 공유, 기능별 쿨다운·blocked 독립, 재인증 공유와 알림 1회(동시 감지 포함), 옛 세션·프로필 이전, 마이그레이션 데이터 복사와 downgrade, 마스킹, REST·에이전트 범위, 기존 NotebookLM 테스트(`tests/test_google_account.py`, `tests/test_notebooklm_service.py`, `unit_tests/test_notebooklm_unit.py`).
- 미검증: 실제 Google 로그인과 요청(사용자 차례), 다른 도메인(NotebookLM 외) 쿠키 범위가 실제로 필요한지, 로그인 후 옛 경로 링크를 이전 세대가 읽는 실기 동작(파일 시스템 수준 테스트만 했다).
