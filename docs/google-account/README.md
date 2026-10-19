# Google 계정 공통 모듈: 새 기능(consumer) 붙이기

설계 근거는 `docs/adr/0051-google-account-common-module.md`. 이 문서는 새 기능을 붙일 때 필요한 것만 적는다.

## 구조 한 장

```
ttakkari/google_account/
  registry.py   Consumer 등록부(여기에 기능을 등록한다)
  service.py    계정 상태·로그인 위임·재인증 알림(기능이 부르는 mark_ok / mark_reauth)
  login.py      1단계 일반 Chrome 로그인 창 + 2단계 headless 루프백 CDP 쿠키 수집
  session.py    세션 파일(0600/0700, 원자적 교체), 계정별 프로필 경로, 옛 위치 이전
  redact.py     쿠키·토큰 마스킹   errors.py  공통 오류   metrics.py  브라우저 실행 계측
ttakkari/<기능>/consumer.py   기능이 정의하는 Consumer 등록 (NotebookLM 이 첫 예)
```

계정 하나의 세션과 전용 프로필은 등록된 모든 기능이 공유한다. 기능마다 다시 로그인하지 않는다.

## 새 기능이 정의하는 것

1. **Consumer 등록** (`ttakkari/<기능>/consumer.py`, `notebooklm/consumer.py` 를 본보기로)

   | 필드 | 뜻 |
   |---|---|
   | `id`, `label`, `description`, `path` | 식별자(`^[a-z][a-z0-9_-]{0,31}$`), 화면 이름·설명, 프런트 경로 |
   | `login_url`, `app_hosts` | 로그인 창이 여는 앱 화면(https)과 로그인 완료로 인정하는 호스트. `login_url` 의 호스트는 `app_hosts` 안이어야 한다 |
   | `cookie_domains` | 기본 Google 도메인(`google.com`, `googleusercontent.com`) 밖에서 세션에 담아야 하는 쿠키 도메인 접미사. 점으로 시작하지 않는다 |
   | `required_cookies` | 계정 필수 쿠키(`SID`, `__Secure-1PSIDTS`) 외에 이 기능이 요구하는 쿠키 이름 |
   | `enabled` | 기능 스위치(꺼지면 그 기능의 로그인 창이 열리지 않는다) |
   | `describe(account_id)` | 상태 요약 `{state, message, cooldown_until, retry_after, last_ok_at, ...}`. 쿠키·토큰 금지. 행을 만들지 말고 읽기만 한다 |
   | `verify`, `resume` | 선택. 접속 확인 1회(Google 요청), 기능의 `blocked` 해제 |
   | `on_session_saved`, `on_session_removed`, `on_reauth` | 선택. 세션이 바뀌면 기능이 자기 클라이언트·캐시를 비운다 |

   등록은 `registry._load_builtin()` 에 `importlib.import_module("ttakkari.<기능>.consumer")` 한 줄을 더하면 된다(기능이 import 되기 전에도 계정 화면에 나오게 하려는 것).

2. **요청 전 검사**: Google 에 나가기 전에 `google_account.service.service.effective(account_id)` 를 읽는다. `login_required`, `reauth_required`, `disabled` 면 요청하지 않고 `AuthRequired` 등을 낸다. 기능 자기 상태(쿨다운, blocked)는 그다음에 본다.

3. **결과 보고**
   - 요청 성공: `await service.mark_ok(account_id)`
   - 세션 만료(`AuthRequired`): `await service.mark_reauth(account_id, message, feature="<id>")`. 알림은 계정당 한 번이고 다른 기능의 `on_reauth` 훅이 불린다. 기능이 따로 알림을 보내지 않는다.
   - 429, 보호 신호: 기능이 자기 표에 기록한다. 계정 상태에 올리지 않는다.

4. **기능 상태 저장**: 쿨다운·blocked 같은 기능 고유 상태는 기능 자기 표에 둔다. 새 표는 additive 마이그레이션으로 만들고 `deploy/generation-contract.json` 을 함께 갱신한다. 계정 표(`google_accounts`)에는 기능 컬럼을 더하지 않는다.

5. **오류**: `ttakkari.google_account.errors` 를 쓰고 기능 고유 오류만 `GoogleAccountError` 를 상속해 정의한다. REST 오류 핸들러는 공통이 처리한다(`{code, message[, retry_after]}`).

6. **테스트**: `tests/test_google_account.py` 의 `FakeFeature` 패턴을 본보기로, 다른 기능의 쿨다운이 번지지 않는지와 재인증 공유를 확인한다. 실제 Google 요청은 하지 않는다.

## 지켜야 할 불변 조건

- 보호장치 우회 금지: 자동화 감지 끄기, UA 위장, 쿠키 DB 복호화, 키체인 열람을 하지 않는다. 1단계 로그인 창에는 원격 디버깅 플래그를 붙이지 않는다.
- 쿠키·토큰·CSRF 값을 응답·로그·예외·메트릭·DB 에 두지 않는다. 오류 문자열은 `redact.mask`/`safe_error` 를 거친다. 새 기능이 세션 값을 읽으면 `session.load_secrets(account_id)` 로 마스킹에 등록한다.
- 세션 파일은 0600, 디렉터리는 0700, 교체는 원자적이다. 암호화한다고 말하지 않는다.
- 계정 키(`account_id`)를 모든 표·잠금·파일 경로에 넣는다. 한 계정의 상태가 다른 계정으로 번지지 않는다.
- 한 기능의 쿨다운·보호 신호가 다른 기능과 계정 세션 상태를 막지 않는다.
- 유료 API 키 경로를 만들지 않는다.
- 에이전트(MCP)에는 상태 조회만 노출한다. 로그인 창, 세션 삭제, 재개는 사람이 UI 에서 한다.
- 무중단 배포: 이전 세대가 읽는 표·파일을 바꾸거나 지우지 않는다. 추가와 복사로만 옮긴다.

## 화면

프런트는 `src/features/google-account/`(계정 카드, 로그인 흐름, 기능 목록)와 설정 화면의 `#google-accounts` 섹션이다. 기능 페이지는 `GoogleAccountCard` 를 `feature="<id>"` 로 쓰면 된다. 새 기능은 별도 UI 없이 목록에 나타난다(`GET /api/google-accounts` 의 `features`).
