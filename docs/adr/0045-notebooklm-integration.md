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
  - Patchright 기반 브라우저 DOM 자동화 방식.
  - 2026년 9월 공식적으로 **아카이브 및 유지보수 중단** 선언.
  - 상시 브라우저 구동으로 메모리(1GB+)와 CPU 낭비가 심각하고 DOM 셀렉터 변경에 극도로 취약함. **배제.**
- **`jacob-bd/notebooklm-mcp-cli` (v0.15.6, Python)**:
  - 1회성 CDP 쿠키 획득 후 순수 `batchexecute` RPC 비동기 HTTP 통신 수행.
  - 최신 릴리스가 매우 활발하며, `NOTEBOOKLM_RPC_OVERRIDES` 핫패치, 동시성 제한, 에러 복구 등이 고도화됨. MIT 라이선스.
- **`teng-lin/notebooklm-py` (v0.8.5, Python)**:
  - 깔끔한 비동기 RPC 클라이언트이나 실전 세션 복구 및 MCP/웹 통합 기능이 부족함. MIT 라이선스.

### 2.2 방식 결정: 자체 내재화 (자체 구현)
기존 프로젝트를 외부 독립 프로세스나 서드파티 패키지로 무작정 의존하거나 포크하지 않고, **`jacob-bd/notebooklm-mcp-cli`의 핵심 batchexecute RPC 클라이언트 로직 및 1회성 CDP 쿠키 추출 메커니즘을 따까리 백엔드 코드베이스(`src/ttakkari/notebooklm/`)로 직접 내재화하여 자체 구현**한다.

선택 이유:
1. **런타임 단순성**: 별도의 Node.js 컨테이너나 외부 CLI 프로세스를 관리할 필요 없이, 기존 따까리 단일 FastAPI 백엔드 프로세스 안에서 경량 비동기 서비스로 동작한다.
2. **독립 유지보수성**: Google의 비공식 RPC ID가 변경되거나 응답 스키마가 바뀔 때 외부 라이브러리 릴리스를 기다리지 않고 따까리 내부에서 즉시 수정·핫패치할 수 있다.
3. **기존 선례와의 일관성**: 따까리는 이미 토스증권(ADR 0044) 및 클로바노트(ADR 0027)를 통해 "Playwright 영구 번들 배제, 설치된 Chrome + 1회성 CDP 세션 획득" 패턴을 확립했다.

---

## 3. 핵심 설계 결정

### 3.1 통신 및 세션 아키텍처 (API 우선 원칙)
- **1회성 인증 (CDP)**:
  - 전용 Chrome 프로필 디렉터리(`~/.ttakkari/notebooklm-chrome`)를 사용한다 (기본 프로필은 macOS TCC 및 Chrome 136+ 차단으로 사용 불가).
  - 사용자가 일반 Chrome 창에서 1회 수동 로그인 (`POST /api/notebooklm/auth/login-window` 호출 시 브라우저 창 팝업).
  - 백엔드는 디버깅 포트의 WebSocket CDP를 통해 세션 쿠키를 메모리로 추출하고 암호화된 파일에 저장한다. 키체인은 건드리지 않는다.
- **일상 통신 (순수 비동기 HTTP/RPC)**:
  - 브라우저를 상시 띄우지 않는다.
  - `httpx.AsyncClient`를 사용하여 `https://notebooklm.google.com/`에 경량 GET 요청을 보내 HTML에서 CSRF 토큰(`SNlM0e`), 세션 ID(`FdrFJe`), 빌드 라벨(`cfb2h`)을 추출/갱신한다.
  - 모든 노트북 CRUD, 소스 등록, 질의응답, 분석 요청은 `/_/LabsTailwindUi/data/batchexecute` 엔드포인트로 JSON RPC POST 요청을 직접 전송한다.
- **세션 만료 감지**:
  - GET 요청 시 `accounts.google.com`으로 302 리다이렉트가 감지되면 즉시 `SessionExpiredError`를 반환하고, 푸시 알림(`notify_user`) 및 UI 상태 뱃지를 통해 사용자에게 1회 재인증을 요청한다.

### 3.2 계층 구조 및 모듈화
다른 에이전트나 API 라우터가 비공식 RPC의 세부 구현을 알 필요가 없도록 철저히 계층화한다:
```
[프론트엔드 UI: /notebooklm] ───► [REST API: /api/notebooklm/*]
                                           │
[상위 에이전트 / 작업 실행] ───► [ttakkari MCP 도구 어댑터]
                                           │
                                           ▼
                       [공통 서비스 계층: NotebookLMService]
                       - 세션 관리 및 인증 토큰 갱신
                       - 동시성 제어 (Semaphore 8건)
                       - 요청 캐시 및 중복 제거
                       - 지수 백오프 및 에러 분류
                                           │
                                           ▼
                       [내부 클라이언트: NotebookLMClient]
                       - batchexecute RPC 페이로드 빌더 / 파서
                       - GenerateFreeFormStreamed 질의 스트림
                       - Resumable Upload 업로더
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
- 따까리 기존 지식 원장의 `SourceRevision/Unit -> Evidence -> Assertion` 장부에 정본으로 기록되어, 이후 RAG 검색 및 `recall` 도구에서 정확한 근거와 함께 조회된다.

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
- **비용 0원**: 유료 API 호출 없이 Google 계정의 무료 사용량만 활용.
- **고성능·저자원**: 상시 브라우저 구동이 없어 백엔드 메모리 증가가 수십 MB 수준에 불과하며, 응답 속도가 수백 ms 수준으로 대폭 개선됨.
- **안정성**: Google의 봇 탐지를 회피하는 기법에 의존하지 않으며, RPC ID 회전 시 무배포 핫패치 지원.
- **시너지**: 수집된 분석 결과가 따까리의 지식 원장에 안전하게 축적됨.

### 제약 사항 및 관리 요소
- 첫 1회는 사용자가 Mac Studio 화면(또는 원격 데스크톱)에서 브라우저 로그인을 직접 수행해야 함.
- 비공식 API이므로 Google의 대규모 인터페이스 개편 시 RPC 페이로드 유지보수가 필요함 (문서화된 핫패치 환경변수로 위험 완화).
- MIT 라이선스 저작권 고지를 소스 코드 헤더 및 문서에 명시함.
