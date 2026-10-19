# ADR 0045. NotebookLM 연동 엔진 자체 구축 및 플랫폼 아키텍처

- 상태: 채택 (2026-10-19)
- 결정자: 에이전트 제안 (1단계 분석·설계·실측 기반)
- 대상: `winterholic-ttakkari` 백엔드, 프론트엔드, MCP 도구, 지식 원장

---

## 1. 배경 및 목적

Google NotebookLM은 대규모 문서, 웹페이지, 유튜브 영상, 텍스트 소스를 바탕으로 높은 정확도의 근거 기반 Q&A(RAG)와 심층 리포트, 오디오 팟캐스트를 생성하는 강력한 도구이다.  
따까리는 개인 지식 수집·분석 및 비서 플랫폼으로서 NotebookLM을 외부 단독 도구가 아닌 핵심 지식 처리 엔진으로 연동하고자 한다.

핵심 요구조건:
1. 기존 Google 계정의 무료 사용 권한 및 할당량을 활용하며, 유료 Gemini API 비용을 발생시키지 않는다.
2. 상시 브라우저 실행(Playwright 등)으로 인한 메모리·CPU 누수와 UI 변경 취약성을 피한다.
3. 따까리 기존 아키텍처(FastAPI, PostgreSQL, ttakkari MCP, `tk-*` 디자인 시스템, 지식 원장)와 유기적으로 통합한다.
4. 상위 에이전트가 내부 RPC 통신 방식을 알 필요 없도록 깔끔한 내부 공통 인터페이스를 제공한다.

---

## 2. 오픈소스 분석 결과와 방식 결정

### 2.1 오픈소스 비교 요약
- **`PleasePrompto/notebooklm-mcp` (v2.0.0, Node.js)**:
  - Patchright 기반으로 Chrome을 유지하고 DOM을 조작한다. 저장소 README는 다중 계정별 Chrome 프로필도 설명한다.
  - 저장소 API의 `archived=true`, README의 2026년 9월 유지보수 종료 고지를 확인했다. MIT 라이선스.
  - README와 소스에서 메모리·CPU 수치 실측 근거를 찾지 못했다. 자원 수치는 이 저장소 검증만으로 단정하지 않는다. DOM 의존성과 보관 상태를 이유로 신규 의존성 후보에서 제외한다.
- **`jacob-bd/notebooklm-mcp-cli` (v0.15.6, Python)**:
  - `batchexecute` HTTP 클라이언트와 별도의 CDP 기반 브라우저 로그인/인증 복구 경로를 제공한다. 기본 RPC 전송과 실험적 CDP 전송은 구분된다.
  - MIT 라이선스. `NOTEBOOKLM_RPC_OVERRIDES`는 알려진 `RPC_*` ID의 프로세스 실행 시 덮어쓰기이며, 프로토콜/페이로드 변경을 고치지 않는다.
  - 기본 동시 작업 수 8은 Q&A 비동기 작업 admission 한도다. 모든 RPC에 걸린 세마포어가 아니다. 재시도 구현은 기본 최대 3회, 지수 간격 1/2/4초(상한 16초)이며 동기 `time.sleep`을 쓴다. 부작용 RPC는 안전한 연결 실패 외에 무조건 재전송하지 않는다.
- **`teng-lin/notebooklm-py` (최신 릴리스 v0.8.5, Python)**:
  - MIT 라이선스의 비동기 Python 클라이언트다. 노트북·소스·Q&A·아티팩트 외에 MCP 어댑터와 실험적 단일 테넌트 REST 서버도 저장소에 있다.
  - 최신 릴리스 태그 뒤에도 저장소 HEAD가 9개 커밋 앞서 있다. HEAD에는 인증 갱신 조정기와 동시성 제어가 있으므로 “세션 복구 및 MCP/웹 통합이 부족하다”는 평가는 부정확하다. 릴리스 패키지와 HEAD의 차이는 도입 전 별도로 검증해야 한다.

### 2.2 방식 결정: 상류 라이브러리 의존 + 따까리 어댑터 (1단계 권고)
RPC와 페이로드 파서를 복사해 소유하지 않는다. `notebooklm-py`의 공개 `NotebookLMClient` API를 우선 후보로 두고, 따까리는 내부 `NotebookLMService` 계약과 REST/MCP 어댑터만 소유한다. 설치된 Chrome의 전용 프로필에서 사용자가 직접 로그인하는 인증 흐름은 라이브러리의 인증 경로가 요구사항을 만족하는지 구현 전 호환성 spike로 확인한다. 호환되지 않으면 인증 어댑터만 별도로 둔다. 상류의 private 모듈을 import하거나 RPC 코드를 복사하는 것은 금지한다.

선택 이유:
1. 비공식 RPC와 응답 파싱은 이미 잦은 변경을 겪는다. 복사하면 상류 변경 감시와 보안·세션 복구 수정 비용이 따까리에 그대로 남고, `NOTEBOOKLM_RPC_OVERRIDES`도 RPC ID만 바꿀 뿐 페이로드 변경을 해결하지 않는다.
2. `notebooklm-py`는 Python 라이브러리용 공개 API와 비동기 클라이언트를 제공한다. 버전을 고정하고, 의존성 갱신 때 공개 API 호환성 및 실제 계정 검증을 수행하는 편이 로컬 복사본을 추적하는 것보다 유지보수 책임이 명확하다.
3. 설치된 Chrome + CDP 인증을 기존 선례가 정당화한다고 간주하지 않는다. ADR 0021은 과거 Playwright 사용 기록이고 ADR 0027은 해당 연동을 제거한 결정이다. ADR 0044는 별도 Chrome/CDP 제어를 채택했으며, 보호 동작과 실제 계정 호환성은 이 연동에서도 별도로 검증해야 한다.

---

## 3. 핵심 설계 결정

### 3.1 통신 및 세션 아키텍처 (API 우선 원칙)
- **1회성 인증 (CDP)**:
  - 전용 Chrome 프로필 디렉터리(`~/.ttakkari/notebooklm-chrome`)를 사용한다 (기본 프로필은 macOS TCC 및 Chrome 136+ 차단으로 사용 불가).
  - 사용자가 일반 Chrome 창에서 1회 수동 로그인 (`POST /api/notebooklm/auth/login-window` 호출 시 브라우저 창 팝업).
  - 백엔드는 디버깅 포트의 WebSocket CDP를 통해 세션 쿠키를 메모리로 추출한다. 영속 저장 형식·키 관리·파일 권한은 확정 전이다. “암호화된 파일”을 전제로 구현하지 않는다.
- **일상 통신 (상류 라이브러리)**:
  - 요청별 브라우저 비상주를 목표로 한다. 장기 세션 복구 과정에서 브라우저가 필요한지와 리소스 영향은 spike 및 호스트 실측 전까지 미확정이다.
  - NotebookLM의 계정 토큰과 내부 RPC 세부는 상류 라이브러리가 소유하고, 따까리 서비스는 안정된 공개 API만 호출한다. 내부 RPC가 Google의 지원 API이거나 정책상 허용된다는 근거는 확인하지 못했다.
- **세션 만료 감지**:
  - 인증 확인 응답이 로그인으로 리다이렉트되거나 갱신할 수 없는 상태면 재시도 루프를 만들지 않고 인증 오류를 반환한다. 푸시 알림과 UI 상태는 별도 제품 결정이며 아직 구현 사실이 아니다.

### 3.2 계층 구조 및 모듈화
다른 에이전트나 API 라우터가 비공식 RPC의 세부 구현을 알 필요가 없도록 철저히 계층화한다:
```
[프론트엔드 UI: /notebooklm] ───► [REST API: /api/notebooklm/*]
                                           │
[상위 에이전트 / 작업 실행] ───► [ttakkari MCP 도구 어댑터]
                                           │
                                           ▼
                       [공통 서비스 계층: NotebookLMService]
                       - 계정 범위 세션·상태 관리
                       - 동시성 한도 (구현 전 결정)
                       - 멱등성·중복 요청 정책 (구현 전 결정)
                       - 제한된 재시도 및 오류 분류 (구현 전 결정)
                                           │
                                           ▼
                       [상류 어댑터: notebooklm-py 공개 API]
                       - 버전 고정, 호환성 검사, 오류 변환
                       - 상류 지원 기능만 노출
                                           │
                                           ▼
                       [Google NotebookLM Backend Endpoint]
```

### 3.3 지식 원장 (Knowledge Ledger, ADR 0031) 연계
NotebookLM은 단순한 채팅 도구가 아니라 따까리의 '지식 수집 및 1차 정제 엔진'으로 동작한다.
- 질의응답 결과와 분석 리포트를 구조화하여 파싱:
  - `Source Unit`: 등록된 웹페이지, 유튜브 영상, 문서 원문 발췌문.
  - `Evidence`: Q&A 응답에 첨부된 인용 번호(`citation`), 소스 인덱스, 텍스트 스팬(start/end).
  - `Assertion`: 질의응답을 통해 생성된 사실/결론 (주어-술어-목적어 트리플 및 극성/양상).
- 적재 방향을 제안한다. 실제 매핑·검토 상태·원문 접근성은 구현 전 확정해야 하며, 1단계 문서는 정본 기록 또는 `recall` 노출을 보장하지 않는다.

### 3.4 프론트엔드 'NotebookLM 플랫폼' 화면
- 라우트: `/notebooklm` (하위 `/notebooklm/:notebookId`)
- 사이드바/더보기: `shell/features.ts`에 등록하여 모바일 더보기 및 데스크톱 부가 기능 모달에 자동 노출.
- 디자인 시스템: 따까리 표준 `tk-*` 계약 및 토큰 준수.
- 주요 뷰:
  1. 연결 상태 카드: 인증 유효 여부, 마지막 확인 시각, 1회 로그인 팝업 버튼.
  2. 노트북 목록 및 생성/삭제/이름변경 모달.
  3. 소스 관리 패널: URL, 유튜브 링크, 파일 드래그앤드롭 업로드, 활성화 체크박스.
  4. 대화 및 분석 패널: 스트리밍 Q&A, 인용 칩 클릭 시 원문 하이라이트 표시, 지식 원장 동기화 버튼.

---

## 4. 결과 및 영향

### 긍정적 효과
- **API 과금 경로**: 설계상 Gemini API 키를 사용하지 않는다. NotebookLM 계정의 기능·사용 한도와 과금 정책은 계정/시점별로 달라질 수 있어 “비용 0원”을 보장하지 않는다.
- **자원·성능**: 아직 구현·실측하지 않았다. 브라우저 미상주만으로 메모리 증가량이나 응답 시간을 단정하지 않는다.
- **보호 정책**: 내부 RPC 호출도 “정상 API” 또는 보호장치 비우회로 간주할 근거가 없다. 계정 보호·정책 위반 신호가 나타나면 자동화와 재시도를 중지한다. CAPTCHA, 자동화 탐지 또는 접근 제한을 우회하지 않는다.
- **시너지**: 수집된 분석 결과가 따까리의 지식 원장에 안전하게 축적됨.

### 제약 사항 및 관리 요소
- 첫 1회는 사용자가 Mac Studio 화면(또는 원격 데스크톱)에서 브라우저 로그인을 직접 수행해야 함.
- 비공식 API이므로 Google의 대규모 인터페이스 개편 시 상류 의존성 갱신과 호환성 검증이 필요함. RPC ID 핫패치만으로 페이로드/응답 변경을 복구할 수 있다고 전제하지 않는다.
- MIT 구성요소를 실제로 채택할 경우 해당 라이선스와 저작권 고지를 보존한다.

---

## 5. 2단계 구현 결정 (2026-10-19, 백엔드 엔진·REST·MCP)

설계서 §9 미결정 사항에 대한 구현 시점의 결정과 근거다. 실제 Google 계정 요청은 하지 않았다(로그인 세션 없음). 아래 "검증"은 가짜 구현·모의 전송으로 한 것이다.

### 5.1 상류 의존과 spike
- `notebooklm-py==0.8.5`(PyPI 최신 안정, 2026-10-13) 정확한 버전 고정. 새로 설치된 패키지는 이 패키지와 `rich`·`markdown-it-py`·`mdurl` 뿐이다(click·filelock·httpx·idna는 기존 또는 공통). `headless`(master-token)·`impersonate`(TLS 위장)·`cookies` extra는 설치하지 않았고 코드에서도 쓰지 않는다(단위 테스트가 어댑터 소스에서 해당 이름을 막는다). 라이선스 고지는 `docs/notebooklm/THIRD_PARTY_NOTICES.md`.
- spike(가짜 쿠키 + 가짜 HTTP 전송으로 네트워크 차단, 프록시를 죽은 포트로 지정): 공개 API `NotebookLMClient.from_storage(path=...)`가 우리가 쓴 Playwright 호환 `storage_state.json`을 읽고 쿠키를 요청에 싣는 것, `ClientConfig(retry=RetryOptions(0, 0))`로 상류의 자동 재시도(429·5xx)를 끌 수 있는 것, 429의 `Retry-After`가 `RateLimitError.retry_after`로 전달되는 것을 확인했다. 따라서 "외부에서 얻은 쿠키 주입"은 공개 API로 가능하고 별도 최소 어댑터는 필요 없다. private 모듈(`_*`)은 import하지 않는다.
- 상류 기본 호스트는 `notebook.google.com`(구 `notebooklm.google.com`)이다. 로그인 창 URL은 `https://notebooklm.google.com/`이며 리다이렉트는 실제 호스트에서 미확인이다. 로그인 완료 판정은 두 호스트를 모두 인정한다.
- 상류는 쿠키 회전 결과를 같은 세션 파일에 되쓴다(자체 파일 락). 파일 소유와 위치는 우리가 정하고, 그 디렉터리(`0700`)에서만 일어난다. 상류의 `allow_headless` 브라우저 복구는 켜지 않았다.

### 5.2 계약과 교체 가능성
`NotebookLMBackend`(Protocol, `runtime_checkable`)와 DTO는 `notebooklm/ports.py`. 서비스·REST·MCP는 이 모듈만 안다. 상류 예외는 어댑터에서 `errors.py` 도메인 오류로 바꾸고 상류 메시지는 밖으로 내지 않는다. 테스트에 다른 이름·다른 `capabilities()`를 가진 두 번째 구현을 끼워 상위 코드가 무변경으로 동작함을 확인했다. 지원하지 않는 기능(audio/video overview, research, Drive 소스, sharing)은 `capabilities()`와 `auth/status`에 `available:false` + 이유로 노출한다. 파일 소스는 상류 지원이라 REST 업로드로 제공하고 MCP에는 노출하지 않는다(경로 권한 문제).

### 5.3 세션 저장 (정직한 보호 수준)
- 위치: `~/.ttakkari/notebooklm/accounts/<account_id>/storage_state.json`. 디렉터리 `0700`, 파일 `0600`, 임시 파일 작성 후 `fsync`·`os.replace` 원자적 교체. 구글 도메인 쿠키만 저장하고 `SID`·`__Secure-1PSIDTS`가 없으면 로그인 완료로 보지 않는다.
- **암호화하지 않는다.** 따까리에 쿠키를 감쌀 키 체계가 없다(기존 키체인 코드는 Antigravity 전용이고 비밀 저장소 열람 금지 대상이라 쓰지 않았다). 같은 macOS 사용자의 다른 프로세스는 파일을 읽을 수 있다. 보호는 파일 권한뿐이며 `auth/status`가 `encrypted:false`와 권한 일치 여부를 그대로 알린다. 전용 Chrome 프로필의 쿠키 DB는 Chrome 자체 방식(Keychain의 Chrome Safe Storage)이다.
- 마스킹: 저장·로드 때 쿠키 실제 값을 마스킹 목록에 등록해 문자열 그대로 치환하고, 값을 모르는 경우를 위해 `Cookie:`·`Authorization:`·`name=value`·JSON 필드·URL 토큰·40자 이상 토큰 패턴을 치환한다(`redact.py`). 오류 메시지·job 오류·REST 오류 응답은 모두 `mask()`를 거치며, 테스트가 응답·로그·metrics·DB 메시지에 쿠키 값이 없음을 확인한다. 상태 API는 쿠키 이름만 보여 준다.

### 5.4 로그인 창
- 설치된 Chrome(`/Applications/Google Chrome.app`)을 전용 프로필(`~/.ttakkari/notebooklm-chrome`, `0700`)로 **보이는 일반 창**으로 띄운다. `--remote-debugging-port=0 --remote-debugging-address=127.0.0.1`, 자동화 플래그(`--enable-automation`·headless) 없음. Chrome이 알려 준 CDP 주소가 루프백이 아니면 중단한다.
- CDP는 브라우저 수준 websocket으로 열린 탭 URL(`Target.getTargets`)과 쿠키(`Storage.getCookies`)만 읽는다. 페이지에 붙지 않고 스크립트 실행·클릭·입력을 하지 않는다.
- 완료 판정: NotebookLM 앱 호스트에 있고 `accounts.google.com`이 아니며 필수 쿠키가 2회 연속(약 4초) 있을 때. 그러면 세션을 저장하고 창을 닫는다(`Browser.close`, 실패하면 terminate/kill). 기본 10분(`notebooklm_login_timeout_seconds`) 타임아웃, 취소 API, 사용자가 창을 닫으면 종료, 같은 계정 중복 실행 잠금(프로세스 메모리 + 같은 프로필을 쓰는 Chrome 프로세스 검사), 서버 종료 시 정리.
- **보호 신호(fail closed)**: 로그인 중 URL에 보안 확인(`/signin/v2/challenge`·`/sorry/`·captcha 등)이 보이면 상태를 `protection`으로 바꾸고 쿠키를 읽지도 저장하지도 않는다. 따까리는 그 화면에서 아무것도 하지 않는다. 사람이 창에서 직접 마치면 이어서 진행하고, 못 마치면 타임아웃으로 끝난다. API 사용 중 보호 신호(CAPTCHA·`/sorry/`·unusual traffic 문구)가 오면 계정을 `blocked`로 두고 사람이 `POST /auth/resume` 할 때까지 Google에 요청하지 않는다.
- 호스트 확인(2026-10-19 Mac Studio): 이 로그인 흐름을 임시 프로필로 실행해 0.5초 안에 `waiting` 상태(루프백 CDP 연결, Chrome 프로세스 9개)가 됐고 취소로 모든 프로세스가 종료됨을 확인했다. 로그인 입력은 하지 않았다. 창이 화면에 실제로 보이는지는 시각 확인하지 못했다(프로세스·CDP 연결만 확인). 배포된 백엔드 서비스 프로세스 안에서의 창 표시는 미검증.
- 토스증권 어댑터(ADR 0044)와의 공통화는 하지 않았다. 그쪽은 읽기 전용 Fetch 통제와 얽혀 있고 다른 세션이 다루는 코드라 회귀 위험이 커서, 같은 선례(전용 프로필·일반 창·직접 실행한 Chrome·psutil 중복 검사·websockets)를 따라 `notebooklm/login.py`에 별도로 두었다. 후속 정리에서 `find_chrome`·종료 로직을 공통 모듈로 옮길 수 있다.

### 5.5 작업 모델
- 동기(요청별 타임아웃): 노트북 목록/상세/생성/이름변경/삭제, 소스 목록/삭제, 질의(기본 120초). DB job: 소스 추가(등록 요청 + 처리 완료 대기, 기본 180초). 장시간 분석 job은 이번 범위에 없다.
- 기존 따까리의 `reverse_jobs`·`artcraft_tasks`는 세션/Run에 결속되어 재사용할 수 없어 전용 `notebooklm_jobs` 한 테이블만 추가했다. 상태 `queued/running/succeeded/failed/cancelled/unknown`, 하트비트(5초), `cancel_requested`. 별도 큐·서비스·컨테이너 없음(프로세스 안 `asyncio` 태스크 + DB).
- 재시작 복구: 기동 시(단일 프로세스) `running→unknown`, `queued→failed`. 무중단 배포(generation) 모드에서는 이전 세대가 아직 돌리는 job을 건드리지 않도록 주기 작업(60초)이 하트비트가 120초 넘게 오래된 job만 같은 규칙으로 정리한다. 소스 원장도 같이 맞춘다(전송 중이던 건 `unknown`, 시작 전이던 건 해제).
- 마이그레이션 `0046_notebooklm`: `notebooklm_accounts`, `notebooklm_sources`(중복 원장), `notebooklm_jobs`. 모든 테이블이 `account_id`를 가지며 지금은 `primary` 하나만 쓴다. `upgrade → downgrade → upgrade`를 새 DB에서 확인했다.

### 5.6 동시성·재시도·쿨다운·재인증
- 계정당 동시 실행 기본 2(`notebooklm_concurrency`, 프로세스 안 세마포어). 무중단 배포 중 두 세대가 겹치면 합쳐 최대 4까지 갈 수 있다. 이 한도는 Google 사용 한도를 보장하지 않는다.
- 상류 자동 재시도를 끄고(`RetryOptions(0,0)`) 서비스가 정책을 소유한다. **읽기만** 일시 오류(연결·시간 초과·5xx)에 지수 백오프(기본 1/2/4초, 상한 8초, 지터)로 최대 3회 재시도한다. 쓰기(생성·이름변경·삭제·소스 추가)와 **질의**는 1회만 보낸다. 질의는 서버 대화 기록과 할당량을 쓰므로 쓰기로 취급한다. 소스 처리 대기도 재시도하지 않는다(이미 긴 대기).
- 쓰기가 시간 초과·연결 오류로 끝나면 `outcome_unknown`으로 표시하고 소스 원장을 `unknown`으로 둔다. 같은 소스를 다시 요청하면 원격 목록과 URL을 대조해 있으면 확정하고, 없으면 사람이 확인한 뒤 `force:true`로만 재등록한다.
- 중복 방지 키 `(account, notebook, 정규화 URL | 텍스트 해시 | 파일 해시)`는 DB 유니크 제약이며 예약을 먼저 커밋한 뒤 전송한다. URL은 대소문자·기본 포트·fragment·추적 파라미터·쿼리 순서·YouTube 주소 변형(`youtu.be`·shorts·embed)을 정규화하고 http(s)가 아닌 주소와 사설·루프백 주소를 거절한다.
- 429/`RESOURCE_EXHAUSTED`: `Retry-After`(없으면 기본 900초)를 `cooldown_until`로 DB에 기록하고 상태 API에 `cooldown`과 `retry_after`로 노출한다. 쿨다운 동안 모든 호출은 Google에 닿지 않고 429 `cooldown`을 즉시 반환한다. 우회·재시도 없음.
- 인증 만료(상류 `AuthError`)는 상태를 `reauth_required`로 바꾸고 열린 클라이언트를 닫고 기존 푸시 체계(`PushKind.notebooklm`, 알림함 `system`)로 **한 번** 알린다(`reauth_notified_at`로 중복 방지). 이후 호출은 Google에 닿지 않고 409 `auth_required`. 다시 로그인에 성공하면 해제된다.
- `auth/verify`는 Google 요청 1회이며 30초 간격으로 제한한다. `auth/status`는 요청을 보내지 않는다.

### 5.7 REST·MCP
REST는 `/api/notebooklm`(소유자 JWT)과 `/api/agent/notebooklm`(개인 세션 전용). 에이전트 경로에는 로그인 창·취소·resume·세션 삭제·업로드를 두지 않는다. MCP는 기존 `ttakkari` 서버에 `notebooklm_*` 11개(읽기 4개 `readOnlyHint`, 쓰기·질의 7개는 자동 승인 목록)를 등록했고 `api/assistant.py` `TOOLS`와 `tool_policy`, `tests/test_orchestrator.py`의 도구 집합 검사를 맞췄다. 삭제 도구는 `confirm=true`가 없으면 API를 호출하지 않는다. 계약은 `docs/api-contract.md`.

### 5.8 질의 구조와 원장 내보내기
질의 결과는 `answer`(NotebookLM 분석)·`citations`(번호, `source_id`, 원문 인용 텍스트, 원문 좌표 `source_span`, 답변 좌표 `answer_span`)·`sources`(인용된 출처의 제목·URL)로 분리한다. 상류가 폐기 예정으로 표시한 `answer_start_char`는 쓰지 않고 `start_char/end_char`(원문, UTF-16 code unit)와 `answer_anchor_*`(답변)를 쓴다. 값이 없으면 `null`이다. `build_ledger_export()`는 원문 발췌(`source_units`)·분석(`analysis`, `review_status: unreviewed`, `promoted: false`)·인용 연결(`citations`)·`assertions: []`로 나눠 돌려주며 자동 추출·승격·적재를 하지 않는다. 원장 실제 적재는 P0 게이트(PMS 5.4.7.3.6) 이후다.

### 5.9 계측
프로세스 안 카운터와 JSON 구조화 로그(`notebooklm {"event": ...}`): 요청 수(op별), 오류 코드별 횟수, 재시도 수, 지연(평균·최대), 브라우저 실행 횟수, 쿨다운·재인증·보호 신호·중복 차단 횟수. `auth/status.metrics`로도 보인다. P50/P95와 메모리는 측정하지 않았다(로그인 후 단계).

### 5.10 검증·미검증
- 검증(가짜 백엔드·모의 전송·실제 테스트 DB): 마스킹, 세션 파일 권한·원자 교체, 중복 소스 방지(동시 2건 포함), 읽기/쓰기 재시도 구분, 429 쿨다운과 쿨다운 만료, 세션 만료 감지와 1회 알림, 보호 신호 중단·재개, 동시성 한도 2, job 상태 전이·취소·재시작 복구(세대 겹침 포함), Protocol 교체, MCP 도구 호출(REST 경유), 개인/업무 세션 범위, 업로드 상한, 로그인 창 흐름(가짜 Chrome)과 실제 호스트 창 기동.
- 미검증: 실제 Google 로그인과 NotebookLM 응답(상류 파싱이 실제 응답과 맞는지 포함), 로그인 완료 판정이 실제 리다이렉트(`notebook.google.com`/`notebooklm.google.com`)와 맞는지, 사용 한도·약관·계정 보호 정책, 배포 서비스 프로세스에서의 창 표시, 장시간 운영·성능·메모리, 상류 쿠키 회전이 세션 파일에 되써지는 실제 동작.
