# NotebookLM 오픈소스 분석 및 실현 가능성 실측 보고서

작성일자: 2026-10-19  
작성자: 따까리(winterholic-ttakkari) 분석·설계 에이전트  
문서 상태: 승인 대기 / 1단계 산출물

---

## 1. 분석 개요 및 목적

본 문서는 Google NotebookLM을 따까리의 자체 지식 수집·분석 엔진 및 'NotebookLM 플랫폼'으로 통합하기 위한 1단계 분석 보고서이다.  
기존 오픈소스의 구조와 장단점, 라이선스, 최신 이슈/PR을 심층 분석하고, Mac Studio 호스트 환경에서의 실현 가능성을 안전하게 실측한 결과를 바탕으로 자체 구축 방향성을 제시한다.

---

## 2. 오픈소스 심층 분석 및 비교

주요 분석 대상:
1. **`jacob-bd/notebooklm-mcp-cli`** (v0.15.6, Python, MIT License)
2. **`PleasePrompto/notebooklm-mcp`** (v2.0.0, Node.js/TypeScript, MIT License, **2026년 9월 아카이브/유지보수 종료**)
3. **`teng-lin/notebooklm-py`** (v0.8.5, Python, MIT License)

### 2.1 A. 통신 및 인증 구조

| 항목 | `jacob-bd/notebooklm-mcp-cli` | `PleasePrompto/notebooklm-mcp` | `teng-lin/notebooklm-py` |
| :--- | :--- | :--- | :--- |
| **통신 방식** | **하이브리드 (API 우선)**: 1회성 브라우저 CDP로 쿠키 획득 후, 내부 `batchexecute` RPC 직접 호출 | **순수 브라우저 자동화**: Patchright(Playwright 포크)로 상시 Chrome 창을 띄워 DOM 조작 | **RPC 클라이언트**: `batchexecute` RPC 비동기 HTTP 호출 |
| **Google 인증 방식** | 사용자 1회 브라우저 로그인 후, CDP 웹소켓(`Network.getCookies`)으로 세션 쿠키 추출 | Patchright 스텔스 브라우저 프로필 디렉터리에 로그인 세션 보관 | 브라우저 쿠키 파일 파싱(`rookie-cookies`) 또는 수동 토큰 주입 |
| **토큰 갱신 구조** | 저장된 쿠키로 `https://notebooklm.google.com/`에 경량 HTTP GET 1회 호출 → HTML에서 CSRF 토큰(`SNlM0e`), 세션 ID(`FdrFJe`), 빌드 라벨(`cfb2h`)을 정규표현식으로 자동 추출 | 브라우저 페이지 탐색을 유지하며 웹 세션에 의존 | 세션 재연결 시 홈페이지 재조회 |
| **세션 만료 처리** | 302 리다이렉트(`accounts.google.com`) 감지 시 `AuthenticationError` 발생, HTTP-only 새로고침 시도 후 실패 시 사용자 재인증 안내 | 페이지 내 로그인 버튼/폼 감지 시 오류 발생 | HTTP 302/401 발생 시 예외 발생 |
| **비공식 API 변경 취약성** | RPC ID 변경 시 실패 가능하나, **`NOTEBOOKLM_RPC_OVERRIDES` 환경변수 핫패치**로 무배포 복구 지원 | 웹 UI DOM/CSS 변경 시 즉시 파손, 핫패치 불가 | RPC ID 변경 시 코드 수정 필요 |

#### 핵심 분석 결론 (인증 및 통신):
- Google NotebookLM은 공식 외부 API를 제공하지 않으며, 내부 프론트엔드가 사용하는 `/_/LabsTailwindUi/data/batchexecute` RPC 엔드포인트를 호출한다.
- 인증에는 Google의 다중 세션 쿠키(`SID`, `HSID`, `SSID`, `APISID`, `SAPISID`, `__Secure-1PSID`, `__Secure-1PSIDTS` 등)와 함께, 홈페이지 렌더링 시 주입되는 CSRF 토큰(`SNlM0e`) 및 세션 ID(`FdrFJe`)가 필요하다.
- `PleasePrompto`의 상시 브라우저 구동 방식은 Google의 봇 탐지 우회(Patchright)에 의존하여 계정 보안 경고나 CAPTCHA 유발 위험이 높고 프로젝트가 이미 아카이브되었다.
- `jacob-bd`의 하이브리드 방식은 **로그인 쿠키 추출에만 브라우저를 1회 띄우고, 이후 모든 통신을 순수 비동기 HTTP/RPC로 처리**하므로 가장 안정적이고 빠르며 따까리 백엔드(FastAPI) 아키텍처에 가장 부합한다.

---

### 2.2 B. 기능 구현 현황

| 기능 영역 | `jacob-bd/notebooklm-mcp-cli` | `PleasePrompto/notebooklm-mcp` | `teng-lin/notebooklm-py` |
| :--- | :--- | :--- | :--- |
| **노트북 CRUD** | 생성(`CCqFvf`), 목록(`wXbhsf`), 상세(`rLM1Ne`), 이름변경(`s0tc2d`), 삭제(`WWINqb`) | 생성, 목록, 선택, 삭제 (DOM 클릭 기반) | 생성, 목록, 상세, 이름변경, 삭제 |
| **URL/웹 소스 추가** | 신규 v2 RPC `ozz5Z` 및 구형 `izAoDd` 지원 | DOM 모달 입력 | RPC 지원 |
| **유튜브 소스 추가** | 지원 (`metadata[5][0]`에서 YouTube URL 정상 파싱) | 지원 (DOM 입력) | 지원 |
| **문서/파일 업로드** | Resumable upload URL(`o4cbdc`) 발급 후 `/upload/_/` 바이너리 푸시 | 파일 업로드 드롭 지원 | 바이너리 업로드 지원 |
| **Google Drive 소스** | 드라이브 파일 ID 등록 및 동기화(`FLmJqe`) 지원 | Drive 피커 DOM 클릭 | 드라이브 ID 연동 |
| **질의응답 (Chat)** | `GenerateFreeFormStreamed` 스트리밍 Q&A 지원 | DOM 채팅창 입력 및 응답 폴링 | RPC 스트리밍 Q&A |
| **출처·인용 보존** | 응답 JSON의 `citations` 블록(출처 인덱스, 텍스트 스팬, 발췌문) 파싱 | DOM 인용 번호 스크랩 | citations 블록 파싱 |
| **스튜디오 (오디오/비디오)** | 팟캐스트/오디오(`R7cb6c`), 비디오 오버뷰, 슬라이드덱, 플래시카드, 퀴즈 | 오디오 오버뷰 생성 요청 | 오디오 생성/폴링 |
| **리서치 (Web/Drive Search)** | Fast Research(`Ljjv0c`), Deep Research(`QA9ei`) 지원 | 미지원 | Fast/Deep Research 지원 |

#### 지원하지 않는 기능 및 기술적 제약:
1. **순수 비브라우저 자동 로그인 불가**: Google 계정은 2FA, Passkey, CAPTCHA, 비정상 접속 탐지가 적용되므로, ID/비밀번호만으로 백엔드에서 헤드리스 자동 로그인을 시도하는 것은 약관 위반 및 계정 잠금 위험이 크다. **사용자가 브라우저 창에서 1회 수동 로그인**해야 한다.
2. **오디오 오버뷰 음성/화자 커스텀 불가**: NotebookLM의 오디오 팟캐스트는 Google 서버 측에서 고정된 남/녀 2인 화자 모델로 합성되므로, 커스텀 보이스 지정은 불가능하다.
3. **Google Drive 연동 제약**: 사용자가 해당 Google 계정에 이미 Drive 접근 권한이 있어야 연동 소스를 읽을 수 있다.

---

### 2.3 C. 성능 및 안정성

| 지표 / 항목 | `jacob-bd/notebooklm-mcp-cli` | `PleasePrompto/notebooklm-mcp` |
| :--- | :--- | :--- |
| **응답 시간 (P50/P95)** | **수백 ms ~ 1.5초** (순수 HTTP 통신) | **5초 ~ 20초** (브라우저 탐색, 렌더링, DOM 폴링) |
| **네트워크 요청 수** | 작업당 1~2회 (목록 1회, 상세 1회) | 페이지 로딩 시 수십~수백 개 정적 리소스 로드 |
| **CPU / 메모리 오버헤드** | 미미함 (수십 MB 수준의 httpx 클라이언트) | **극심함 (브라우저 1대당 500MB~1.5GB RAM, CPU 스파이크)** |
| **동시 작업 처리** | 비동기 세마포어(기본 8건 제한) + FastMCP 락 지원 | 브라우저 인스턴스 단일화로 동시 작업 불가 (직렬화 병목) |
| **오류 복구 및 재시도** | 지수 백오프(`retry.py`), HTTP 429 감지 시 대기, 쿨다운(900초) | 브라우저 재시작 및 충돌 복구 시도 (무거움) |

---

### 2.4 D. 코드 품질, 유지보수성, 라이선스

| 항목 | `jacob-bd/notebooklm-mcp-cli` | `PleasePrompto/notebooklm-mcp` | `teng-lin/notebooklm-py` |
| :--- | :--- | :--- | :--- |
| **언어 및 런타임** | Python (>=3.11) | Node.js / TypeScript | Python (>=3.10) |
| **모듈 구조** | `core/`(RPC), `services/`(도메인), `mcp/`(FastMCP), `cli/`(Typer) 계층 분리 우수 | 브라우저 핸들러 중심 모놀리식 구조 | `rpc/`, `client/` 라이브러리 구조 |
| **유지보수 상태** | **매우 활발함** (v0.15.6, 2026-10-17 릴리스) | **2026-09 아카이브/중단됨 (Unmaintained)** | 활발함 (v0.8.5) |
| **라이선스** | MIT License (Copyright 2025 Jacob Ben David) | MIT License (Copyright 2025 Please Prompto!) | MIT License (Copyright 2026 Teng Lin) |
| **재사용 고지 의무** | MIT 라이선스 전문 및 저작권자 고지 필요 | 아카이브 상태라 채택 비권장 | MIT 라이선스 고지 필요 |

---

## 3. Mac Studio 호스트 환경 실현 가능성 실측

지침에 따라 키체인을 열람하지 않고, 비밀번호/쿠키 값을 출력하지 않는 안전한 범위 내에서 실측을 수행했다.

### 3.1 실측 내용 및 결과
1. **설치된 브라우저 점검**:
   - `/Applications/Google Chrome.app` (v154.x) 정상 설치 및 실행 중 확인.
2. **디버깅 포트 점검**:
   - `lsof -i :9222 -i :9223` 실행 결과: 열려 있는 CDP 디버깅 포트 없음 (일반 브라우저 프로세스만 실행 중).
3. **기존 프로필 쿠키 저장소 검사**:
   - `~/.ttakkari/clovanote-chrome`: Cookies DB 부재.
   - `~/.ttakkari/tossinvest-chrome`: Cookies DB 부재.
   - `~/Library/Application Support/Google/Chrome/Default/Cookies`: macOS TCC 및 Chrome 파일 잠금으로 인해 접근 불가(`Errno 1: Operation not permitted`). ADR 0044의 관측 결과와 일치함.
4. **결론 및 기준선 측정 가능 여부**:
   - 현재 호스트에 NotebookLM 인증 세션이 사전 준비되어 있지 않으므로, **즉각적인 노트북 목록 조회 1회 실측은 불가 (`미실행: 호스트 인증 세션 부재`)**.

### 3.2 사용자에게 필요한 단 하나의 행동
사용자에게 필요한 행동은 **단 1회 전용 Chrome 창에서 Google 계정으로 로그인**하는 것이다.
- 절차:
  1. 백엔드 엔드포인트 `POST /api/notebooklm/auth/login-window`를 호출하거나 터미널에서 스크립트를 실행한다.
  2. Mac Studio 로컬 화면(또는 원격 데스크톱)에 전용 프로필(`~/.ttakkari/notebooklm-chrome`)로 Google Chrome 창이 열린다.
  3. 사용자가 NotebookLM에 로그인하고 창을 닫는다.
  4. 이후 백엔드가 CDP를 통해 쿠키를 1회 추출하여 안전한 세션 저장소에 보관하며, 이후 수주 동안 추가 로그인 없이 모든 기능이 백그라운드에서 동작한다.

---

## 4. 위험 분석 및 대응 전략

| 위험 요소 | 영향도 | 발생 가능성 | 대응 전략 |
| :--- | :--- | :--- | :--- |
| **Google 비공식 RPC ID 변경 (API Drift)** | 상 | 중 | `NOTEBOOKLM_RPC_OVERRIDES` 환경변수를 지원하여, 구글의 메서드 ID 변경 시 코드 수정 및 배포 없이 런타임 핫패치 적용 |
| **세션 쿠키 만료** | 중 | 높음 (수주 주기) | 홈페이지 요청 시 302 리다이렉트 감지 → 프론트엔드 및 알림 시스템(`notify_user`)으로 사용자 1회 재인증 안내 |
| **Google 계정 보호 장치 차단 (CAPTCHA/2FA)** | 상 | 낮음 | 봇 탐지 우회 스텔스 브라우징을 일체 사용하지 않고, 로그인은 일반 Chrome 창에서 사람이 직접 수행하며, 운영 중에는 정상 HTTP API 호출만 수행 |
| **과금 위험** | 상 | 없음 | 종량제 Gemini API 키를 일체 요구/호출하지 않으며, 계정의 무료 NotebookLM 권한만 활용 |
| **동시 요청 초과 (429 Rate Limit)** | 중 | 중 | 백엔드 서비스 계층에 비동기 세마포어(최대 8건) 및 지수 백오프 재시도 적용 |
