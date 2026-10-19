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
| **Google 인증 방식** | CDP 로그인 도구가 브라우저에서 쿠키를 추출한다 (`Network.getAllCookies` 사용). | Patchright persistent Chrome 프로필에 인증 상태를 둔다. README상 암호화 자격증명 저장소는 없다. | 기본은 브라우저 기반 로그인, 추가로 브라우저 쿠키 추출(`rookie-cookies` optional extra), headless master-token 경로, 선택형 curl_cffi JA3 impersonation transport가 있다. 이번 요구에서는 headless/impersonation 경로를 설치·활성화하지 않는다. |
| **토큰 갱신 구조** | 저장된 쿠키로 `https://notebooklm.google.com/`에 경량 HTTP GET 1회 호출 → HTML에서 CSRF 토큰(`SNlM0e`), 세션 ID(`FdrFJe`), 빌드 라벨(`cfb2h`)을 정규표현식으로 자동 추출 | 브라우저 페이지 탐색을 유지하며 웹 세션에 의존 | 세션 재연결 시 홈페이지 재조회 |
| **세션 만료 처리** | 홈페이지 리다이렉트 감지와 HTTP 갱신 외에 브라우저 기반 복구 경로도 있다. 쿠키 나이/Google 측 보호 정책에 따라 자동 복구가 실패할 수 있다. | 브라우저 페이지에서 인증을 확인한다. | 토큰 갱신 조정기와 복구 경로가 현재 HEAD에 있다. 지원 계정/환경을 실제 로그인 없이 확정하지 않는다. |
| **비공식 API 변경 취약성** | `NOTEBOOKLM_RPC_OVERRIDES`로 알려진 RPC ID를 런타임 덮어쓰기할 수 있다. 페이로드·응답 스키마 변경은 해결하지 못한다. | 웹 UI DOM/CSS 변경에 취약하며, 상류 저장소가 archived 상태다. | 공개 클라이언트 API를 통해 RPC 변경을 흡수하는 후보. 버전 고정 시 상류 릴리스 전까지 변경 대응 지연은 남는다. |

#### 핵심 분석 결론 (인증 및 통신):
- 비교한 프로젝트는 스스로 비공식 도구/클라이언트라고 명시하며, 클론된 코드에서 `batchexecute` 등 내부 웹 프로토콜 사용을 확인했다. Google 공식 문서 전체를 조사해 공개 API 부재를 독립 확정한 것은 아니다.
- 인증은 프로필의 Google 세션 쿠키와 홈페이지에서 추출하는 CSRF 토큰(`SNlM0e`)을 사용한다. 세션 ID(`FdrFJe`)와 빌드 라벨(`cfb2h`)은 구현에서 선택적으로 추출·갱신된다. 필요한 쿠키의 정확한 집합은 계정·환경에 따라 달라질 수 있다.
- `PleasePrompto` 저장소는 실제 archived 상태이며, README는 Patchright 기반의 persistent Chrome/DOM 자동화를 기술한다. 봇 탐지·CAPTCHA 발생 확률은 저장소 코드만으로 계량할 수 없어 단정하지 않는다.
- `jacob-bd`는 HTTP RPC가 기본이지만 CDP 로그인·인증 복구 경로도 포함하고, 실험적 CDP RPC transport도 존재한다. 그러므로 “로그인 때만 브라우저 1회”라고 일반화할 수 없다. HTTP 경로도 비공식 RPC이며 Google의 정책 허용이나 보호장치 비우회를 입증하지 않는다.

---

### 2.2 B. 기능 구현 현황

| 기능 영역 | `jacob-bd/notebooklm-mcp-cli` | `PleasePrompto/notebooklm-mcp` | `teng-lin/notebooklm-py` |
| :--- | :--- | :--- | :--- |
| **노트북 CRUD** | 생성(`CCqFvf`), 목록(`wXbhsf`), 상세(`rLM1Ne`), 이름변경(`s0tc2d`), 삭제(`WWINqb`) | 생성, 목록, 선택, 삭제 (DOM 클릭 기반) | 생성, 목록, 상세, 이름변경, 삭제 |
| **URL/웹 소스 추가** | 신규 v2 RPC `ozz5Z` 및 구형 `izAoDd` 지원 | DOM 모달 입력 | RPC 지원 |
| **유튜브 소스 추가** | 지원 (`metadata[5][0]`에서 YouTube URL 정상 파싱) | 지원 (DOM 입력) | 지원 |
| **문서/파일 업로드** | Resumable upload URL(`o4cbdc`) 발급 후 `/upload/_/` 바이너리 푸시 | MCP의 `add_source`는 URL/text만 지원. 파일 업로드 미지원 | 파일 소스 및 resumable upload 경로 지원 |
| **Google Drive 소스** | 드라이브 파일 ID 등록 및 동기화(`FLmJqe`) 지원 | MCP 도구에서 미지원 | Drive 소스 API 있음. 실제 계정 권한 필요 |
| **질의응답 (Chat)** | `GenerateFreeFormStreamed` 스트리밍 Q&A 지원 | DOM 채팅창 입력 및 응답 폴링 | RPC 스트리밍 Q&A |
| **출처·인용 보존** | 스트리밍 답변의 citation 구조 파싱 | 답변 후 DOM citation 패널에서 추출 | 답변 stream의 citation/reference 구조 파싱 |
| **스튜디오 (오디오/비디오)** | 오디오·비디오 오버뷰, 슬라이드덱, 플래시카드, 퀴즈 등 | 오디오 오버뷰 생성·상태·다운로드 도구 | 오디오·비디오 및 여러 아티팩트 생성/폴링 |
| **리서치 (Web/Drive Search)** | Fast Research(`Ljjv0c`), Deep Research(`QA9ei`) 지원 | 미지원 | Fast/Deep Research API 있음 |

#### 지원하지 않는 기능 및 기술적 제약:
1. **인증 방식은 요구 범위로 제한**: `notebooklm-py`에는 브라우저 외에 master-token 자동 인증과 선택형 TLS JA3 impersonation extra도 있다. 정책 적합성을 확인하지 않았고 이번 요구에서는 `headless`/`impersonate` extra 및 관련 transport 설정을 사용하지 않는다. ID/비밀번호 자동 로그인의 허용 여부도 이 분석으로 단정하지 않는다. 요구 경로는 사용자가 전용 브라우저 창에서 직접 로그인하는 방식이며, CAPTCHA·2FA·접근 제한을 우회하지 않고 보호 신호가 나오면 중지한다.
2. **오디오 오버뷰 음성/화자 커스텀**: 비교 저장소의 기능 목록만으로 NotebookLM 제품 계정/버전별 설정 가능성을 확정하지 못했다. 지원 여부 미검증으로 남기며 설계에서 커스텀 음성을 제공하지 않는다.
3. **Google Drive 연동 제약**: 사용자가 해당 Google 계정에 이미 Drive 접근 권한이 있어야 연동 소스를 읽을 수 있다.

---

### 2.3 C. 성능 및 안정성

| 지표 / 항목 | `jacob-bd/notebooklm-mcp-cli` | `PleasePrompto/notebooklm-mcp` |
| :--- | :--- | :--- |
| **응답 시간 (P50/P95)** | 미측정. 저장소에 비교 가능한 벤치마크 자료를 확인하지 못함 | 미측정. 저장소에 비교 가능한 벤치마크 자료를 확인하지 못함 |
| **네트워크 요청 수** | 작업·인증 상태·재시도에 따라 달라짐. 일반화된 측정값 없음 | 페이지와 기능별로 달라짐. 일반화된 측정값 없음 |
| **CPU / 메모리 오버헤드** | 미측정 | 미측정. 상시 브라우저 구조상 별도 호스트 측정이 필요함 |
| **동시 작업 처리** | Q&A 비동기 job admission 기본 한도 8. 전역 RPC 세마포어는 아님. 실제 RPC/retry 코드는 동기 경로를 포함함. | 세션 관리자가 최대 동시 세션 설정을 제공함. 브라우저/페이지별 처리 특성은 코드로만 성능 판단할 수 없음 |
| **오류 복구 및 재시도** | HTTP 429와 일부 5xx를 기본 최대 3회, 지수 간격 1/2/4초(최대 간격 16초)로 재시도. 일부 작업은 중복 부작용 방지를 위해 서버 오류 재시도를 끈다. 별도 900초 쿨다운 근거는 찾지 못함. | 브라우저 세션 종료 시 복구·재시도 코드가 있으나 RPC 백오프와 같은 의미는 아니다. |

---

### 2.4 D. 코드 품질, 유지보수성, 라이선스

| 항목 | `jacob-bd/notebooklm-mcp-cli` | `PleasePrompto/notebooklm-mcp` | `teng-lin/notebooklm-py` |
| :--- | :--- | :--- | :--- |
| **언어 및 런타임** | Python (>=3.11) | Node.js / TypeScript | Python (>=3.10) |
| **모듈 구조** | `core/`(RPC), `services/`(도메인), `mcp/`(FastMCP), `cli/`(Typer) 계층 분리 우수 | 브라우저 핸들러 중심 모놀리식 구조 | `rpc/`, `client/` 라이브러리 구조 |
| **유지보수 상태** | v0.15.6 릴리스(2026-10-17), 확인 HEAD `d6d427c` | v2.0.0, archived=true, 마지막 커밋 2026-09-10 | 최신 릴리스 v0.8.5(2026-10-12), 확인 HEAD `4188d7e`는 릴리스 이후 9커밋 |
| **라이선스** | MIT License (Copyright 2025 Jacob Ben David) | MIT License (Copyright 2025 Please Prompto!) | MIT License (Copyright 2026 Teng Lin) |
| **재사용 고지 의무** | MIT 라이선스 전문 및 저작권자 고지 필요 | MIT 라이선스 전문 및 저작권자 고지 필요 | MIT 라이선스 전문 및 저작권자 고지 필요 |

버전·라이선스·아카이브는 2026-10-19 GitHub API 및 직접 클론의 LICENSE/패키지 메타데이터로 확인했다. 세 저장소의 클론 HEAD는 각각 `d6d427cd`, `00179986`, `4188d7e2`였다. `notebooklm-py`는 README와 `pyproject.toml`에 Python API, MCP extra, 실험적 REST server extra를 명시한다. 저장소 HEAD의 기능을 릴리스 v0.8.5에 모두 있다고 간주하지 않는다.

### 2.5 측정 근거와 제한

위 세 저장소를 `/tmp/notebooklm-audit/` 아래 직접 클론해 정적 소스와 메타데이터를 확인했다. 실제 Google 로그인, NotebookLM 요청, 브라우저 자원 측정, 실계정 응답 시간/정확도는 실행하지 않았다. 기존의 P50/P95·브라우저 메모리·CPU 수치는 재현 가능한 원자료가 없어 삭제했다. 실제 계정/성능 항목은 다음 단계 검증 전까지 미검증이다.

정적 근거 위치(검증 클론 기준): jacob `src/notebooklm_tools/core/base.py`, `core/retry.py`, `services/chat.py`, `pyproject.toml`; PleasePrompto `README.md`, `src/session/`; notebooklm-py `src/notebooklm/client.py`, `auth.py`, `pyproject.toml`, `README.md`. 내부 RPC·인증 코드는 Google 지원 API라는 근거가 아니다.

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
| **Google 비공식 RPC/API 변경 (API Drift)** | 상 | 높음 | 상류 의존성을 고정하고 공개 API 호환성을 검증한다. RPC ID 덮어쓰기는 페이로드 변경을 고치지 않는다. |
| **세션 쿠키 만료** | 중 | 미측정 | 공개 인증 경로의 만료/재인증 결과를 분류한다. 만료 간격은 실계정 확인 전 단정하지 않는다. |
| **Google 계정 보호 장치 차단 (CAPTCHA/2FA)** | 상 | 미측정 | 보호·접근 제한 신호가 발생하면 자동화와 재시도를 중지한다. Google의 미지원 API를 “정상”이라고 가정하지 않는다. |
| **과금 위험** | 상 | 정책 미검증 | Gemini API 호출 경로를 만들지 않는다. NotebookLM 계정별 사용 한도·요금 정책은 별도로 확인한다. |
| **동시 요청 초과 (429 Rate Limit)** | 중 | 미측정 | 계정 범위 동시성·유한 재시도·취소를 정하고, 제한 우회 목적의 재시도는 하지 않는다. |
