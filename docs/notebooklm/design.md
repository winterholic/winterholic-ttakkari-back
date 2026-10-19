# NotebookLM 연동 엔진 및 플랫폼 상세 설계서

작성일자: 2026-10-19  
작성자: 따까리(winterholic-ttakkari) 분석·설계 에이전트  
문서 상태: 승인 대기 / 1단계 산출물

---

## 1. 아키텍처 개요 및 모듈 구조

본 설계는 Google NotebookLM의 내부 `batchexecute` RPC 프로토콜과 1회성 CDP 세션 쿠키 추출을 결합하여, 유료 API 비용 없이 브라우저 상시 구동 리스크를 제거한 고성능·경량 자체 연동 엔진을 구축한다.

### 1.1 백엔드 패키지 레이아웃 (`winterholic-ttakkari/back`)
```
src/ttakkari/notebooklm/
├── __init__.py                # 패키지 진입점 및 주요 클래스 re-export
├── constants.py               # RPC ID 매핑, URL 상수, 제한값 정의
├── errors.py                  # 비즈니스/통신 예외 계층 구조
├── models.py                  # Pydantic DTO 및 요청/응답 데이터 모델
├── client.py                  # 저수준 batchexecute RPC 및 HTTP 클라이언트
├── auth.py                    # 전용 프로필 관리, CDP 쿠키 추출, CSRF 갱신
└── service.py                 # 고수준 도메인 서비스 (싱글톤, 동시성/캐시 제어)

src/ttakkari/api/
└── notebooklm.py              # 프론트엔드 연동용 FastAPI REST 라우터 (/api/notebooklm)

src/ttakkari/orchestrator/
└── mcp_server.py              # 따까리 에이전트용 ttakkari MCP 도구 어댑터 등록
```

### 1.2 프론트엔드 컴포넌트 레이아웃 (`winterholic-ttakkari/front`)
```
src/pages/
├── NotebookLMPage.tsx         # 플랫폼 메인 페이지 (노트북 목록/선택 및 뷰 전환)
├── NotebookLMDetailPage.tsx   # 노트북 상세 (소스 관리 + Q&A 대화 + 인용 뷰어)
└── notebooklm.css             # 플랫폼 전용 스타일 (tk-* 디자인 시스템 토큰 활용)

src/features/notebooklm/
├── AuthStatusCard.tsx         # 연결 상태, 만료 알림, 1회 로그인 팝업 트리거
├── NotebookList.tsx           # 노트북 카드 그리드 및 생성/삭제 모달
├── SourceManager.tsx          # 소스 등록(URL, 유튜브, 파일 업로드) 및 목록
├── ChatPanel.tsx              # RAG 질의응답 스트리밍 대화창
├── CitationViewer.tsx         # 인용 클릭 시 출처 원문 하이라이트 표시 모달/사이드 패널
└── KnowledgeSyncModal.tsx     # Q&A 결과를 따까리 지식 원장으로 내보내는 모달
```

---

## 2. 데이터 모델 및 스키마 (`models.py`)

```python
from __future__ import annotations
from enum import Enum
from pathlib import Path
from typing import Any
from pydantic import BaseModel, Field


class SourceType(str, Enum):
    URL = "url"
    YOUTUBE = "youtube"
    TEXT = "text"
    FILE = "file"
    DRIVE = "drive"


class AuthStatus(BaseModel):
    state: str = Field(..., description="ready | login_required | expired | error")
    message: str
    logged_in: bool = False
    checked_at: str
    cookies_present: bool = False
    profile_dir: str


class NotebookSummary(BaseModel):
    id: str
    title: str
    source_count: int = 0
    updated_at: str | None = None
    is_shared: bool = False


class SourceInfo(BaseModel):
    id: str
    title: str
    source_type: SourceType
    url: str | None = None
    content_preview: str | None = None
    created_at: str | None = None
    status: str = "ready"  # ready | processing | error


class NotebookDetail(NotebookSummary):
    sources: list[SourceInfo] = Field(default_factory=list)
    summary: str | None = None


class CitationSpan(BaseModel):
    source_index: int
    source_id: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    quote_text: str = Field(..., description="인용된 원문 발췌 텍스트")


class QueryResponse(BaseModel):
    turn_id: str
    query: str
    answer: str
    citations: list[CitationSpan] = Field(default_factory=list)
    created_at: str


class KnowledgeSyncRequest(BaseModel):
    notebook_id: str
    turn_id: str
    subject: str = Field(..., description="주장 주어")
    predicate: str = Field(..., description="주장 관계/술어")
    scope: str = Field(default="personal", description="personal | work")
```

---

## 3. 내부 서비스 계층 시그니처 (`NotebookLMService`)

공통 비즈니스 로직을 제공하며, 상위 에이전트와 REST API는 이 서비스를 직접 호출한다.

```python
class NotebookLMService:
    """NotebookLM 연동 비즈니스 로직 및 세션 수명주기 관리자."""

    def __init__(self, settings: Settings): ...

    # 1. 인증 및 세션 관리
    async def get_auth_status(self) -> AuthStatus:
        """현재 저장된 쿠키 및 세션 유효성을 경량 HTTP로 확인."""
        ...

    async def open_login_window(self) -> dict[str, Any]:
        """사용자가 1회 로그인할 수 있도록 전용 Chrome 창을 실행."""
        ...

    async def extract_and_save_session(self) -> AuthStatus:
        """실행된 Chrome의 CDP WebSocket에 연결하여 쿠키를 메모리로 추출 후 저장."""
        ...

    # 2. 노트북 관리
    async def list_notebooks(self) -> list[NotebookSummary]:
        """노트북 목록 조회 (캐시 적용, P50 < 400ms)."""
        ...

    async def create_notebook(self, title: str) -> NotebookDetail:
        """새 노트북 생성."""
        ...

    async def get_notebook(self, notebook_id: str) -> NotebookDetail:
        """노트북 상세 정보 및 등록된 소스 목록 조회."""
        ...

    async def delete_notebook(self, notebook_id: str) -> bool:
        """노트북 영구 삭제."""
        ...

    # 3. 소스 관리
    async def add_source_url(self, notebook_id: str, url: str) -> SourceInfo:
        """웹페이지 또는 유튜브 URL 소스 추가 (YouTube URL은 metadata[5][0] 자동 파싱)."""
        ...

    async def add_source_text(self, notebook_id: str, title: str, text: str) -> SourceInfo:
        """직접 텍스트 소스 추가."""
        ...

    async def add_source_file(self, notebook_id: str, file_path: Path, title: str) -> SourceInfo:
        """로컬 문서를 Resumable Upload 엔드포인트로 전송하여 소스로 등록."""
        ...

    async def delete_source(self, notebook_id: str, source_id: str) -> bool:
        """특정 소스 삭제."""
        ...

    # 4. Q&A 질의응답 및 지식 추출
    async def query(
        self,
        notebook_id: str,
        query_text: str,
        conversation_id: str | None = None
    ) -> QueryResponse:
        """RAG 질의를 수행하고 인용(citations)이 파싱된 답변을 반환."""
        ...

    async def export_to_knowledge_ledger(
        self,
        req: KnowledgeSyncRequest,
        query_res: QueryResponse
    ) -> dict[str, Any]:
        """Q&A 결과 및 인용 스팬을 따까리 지식 원장(0031)의 Unit -> Evidence -> Assertion으로 저장."""
        ...
```

---

## 4. REST API 명세 (`src/ttakkari/api/notebooklm.py`)

FastAPI 라우터 경로: `/api/notebooklm` (인증: Bearer 소유자 JWT)

| Method | Endpoint | 설명 |
| :--- | :--- | :--- |
| `GET` | `/auth/status` | 인증 연결 상태 확인 (브라우저 미실행) |
| `POST` | `/auth/login-window` | Mac Studio 화면에 전용 프로필 Chrome 창 띄우기 |
| `POST` | `/auth/refresh` | 세션 유효성 갱신 및 토큰 재추출 |
| `GET` | `/notebooks` | 노트북 목록 조회 |
| `POST` | `/notebooks` | 새 노트북 생성 (`{ title: string }`) |
| `GET` | `/notebooks/{id}` | 노트북 상세 및 소스 목록 조회 |
| `DELETE`| `/notebooks/{id}` | 노트북 삭제 |
| `POST` | `/notebooks/{id}/sources/url` | 웹/유튜브 URL 소스 추가 |
| `POST` | `/notebooks/{id}/sources/text` | 텍스트 소스 추가 |
| `POST` | `/notebooks/{id}/sources/upload` | 파일 업로드 (multipart/form-data) |
| `DELETE`| `/notebooks/{id}/sources/{sourceId}` | 소스 삭제 |
| `POST` | `/notebooks/{id}/query` | Q&A 질문 전송 및 답변/인용 수신 |
| `POST` | `/notebooks/{id}/sync-knowledge` | 지식 원장으로 분석 결과 전달 |

---

## 5. 따까리 MCP 도구 어댑터 명세

상위 에이전트가 작업 중 지식 수집 및 문서 분석을 위해 호출할 수 있는 ttakkari MCP 도구:

1. **`notebooklm_status`**:
   - 설명: NotebookLM 연결 상태(로그인 여부, 세션 만료 여부)를 확인한다.
   - 파라미터: 없음.
2. **`notebooklm_list`**:
   - 설명: 등록된 모든 노트북의 목록(ID, 제목, 소스 수)을 조회한다.
   - 파라미터: 없음.
3. **`notebooklm_get`**:
   - 설명: 특정 노트북의 상세 정보와 포함된 소스 목록을 조회한다.
   - 파라미터: `notebook_id: str`
4. **`notebooklm_create`**:
   - 설명: 새로운 노트북을 생성한다.
   - 파라미터: `title: str`
5. **`notebooklm_add_source`**:
   - 설명: 노트북에 웹 URL, 유튜브 링크, 또는 텍스트 소스를 추가한다.
   - 파라미터: `notebook_id: str`, `source_type: "url"|"youtube"|"text"`, `url: str | None`, `text: str | None`, `title: str | None`
6. **`notebooklm_query`**:
   - 설명: 노트북에 등록된 소스를 기반으로 RAG 질문을 수행하고, 답변과 출처 인용 목록을 받는다.
   - 파라미터: `notebook_id: str`, `query: str`
7. **`notebooklm_export_knowledge`**:
   - 설명: 노트북의 특정 분석/질의 결과를 따까리 지식 원장으로 동기화한다.
   - 파라미터: `notebook_id: str`, `turn_id: str`, `subject: str`, `predicate: str`, `scope: "personal"|"work"`

---

## 6. 프론트엔드 'NotebookLM 플랫폼' 화면 구성

### 6.1 라우팅 및 셸 등록
- `front/src/shell/features.ts`:
  ```ts
  {
    root: "notebooklm",
    to: "/notebooklm",
    icon: "library",  // 또는 전용 아이콘
    label: "NotebookLM",
    description: "노트북LM 플랫폼: 지식 연동·자료 수집·노트북 분석",
    mobileMore: true,
  }
  ```
- `front/src/App.tsx`:
  `<Route path="/notebooklm" element={<NotebookLMPage />} />`  
  `<Route path="/notebooklm/:notebookId" element={<NotebookLMDetailPage />} />`

### 6.2 화면 레이아웃 (반응형 모바일/데스크톱)
```
+-------------------------------------------------------------+
| PageHeader ("NotebookLM 플랫폼")               [로그인 상태 칩] |
+-------------------------------------------------------------+
| [연결 상태 배너] (준비 완료 / 1회 로그인 필요 시 안내 및 버튼)      |
+-------------------------------------------------------------+
| [좌측 / 상단: 노트북 목록]       | [우측 / 메인: 선택된 노트북]         |
| + [새 노트북 만들기]             | - 노트북 제목: "기술 문서 분석"     |
|                                |                               |
| 1. AI 논문 리서치 (3 소스)       | [탭: 소스 관리 (3)] [탭: Q&A 질의] |
| 2. 2026 트렌드 (5 소스)        |                               |
| 3. 토스증권 아키텍처 (2 소스)    | [소스 등록 버튼] (+URL, +파일)  |
|                                | - Source 1: 유튜브 영상 (URL)  |
|                                | - Source 2: 백서 PDF          |
|                                | ----------------------------- |
|                                | [Q&A 채팅 영역]                |
|                                | User: 이 논문의 핵심 주장은?    |
|                                | LM: 이 논문의 주장은 ... [1]    |
|                                | [지식 원장으로 보내기 버튼]       |
+-------------------------------------------------------------+
```

---

## 7. 운영 환경 14개 검증 계획 매핑표

| 번호 | 요구사항 검증 항목 | 검증 방법 및 성공 기준 | 구현 단계 |
| :---: | :--- | :--- | :---: |
| **1** | 기존 Google 계정 인증 | 1회 로그인 창에서 로그인 후 CDP 웹소켓으로 쿠키 정상 저장 확인 | 2단계 |
| **2** | 별도 유료 API 미사용 | Gemini API 키 미설정 상태에서 HTTP 네트워크 패킷 검사(종량제 엔드포인트 미호출 확인) | 2단계 |
| **3** | 노트북 생성·조회 | RPC `CCqFvf`(생성), `wXbhsf`(목록) 호출 후 DB 및 웹 UI와 일치 확인 | 2단계 |
| **4** | 유튜브 URL 등록 | YouTube 링크 소스 추가 후 `metadata[5][0]`에서 원본 URL 파싱 확인 | 2단계 |
| **5** | 웹페이지·문서 등록 | 임의 웹페이지 URL 및 로컬 PDF 업로드 후 상태 `ready` 확인 | 3단계 |
| **6** | 질의응답·결과 추출 | `GenerateFreeFormStreamed` 호출하여 답변 텍스트 수신 확인 | 3단계 |
| **7** | 출처·인용 보존 | 응답 페이로드 내 `citations` 블록에서 인용 텍스트 및 오프셋 추출 확인 | 3단계 |
| **8** | 인증 세션 재사용 | 브라우저 종료 후 24시간 뒤 재요청 시 HTTP GET CSRF 토큰 자동 갱신 확인 | 3단계 |
| **9** | 중복 요청 방지 | 동일 노트북 대상 동시 질의 시 세마포어(8건) 제한 및 동일 요청 중복 방지 확인 | 4단계 |
| **10** | 오류·타임아웃 처리 | 네트워크 단절 및 타임아웃 시 `TransientBackendError` 안전 처리 확인 | 4단계 |
| **11** | 요청 제한 시 안전한 처리 | HTTP 429 및 RESOURCE_EXHAUSTED 발생 시 지수 백오프 대기 후 복구 확인 | 4단계 |
| **12** | 따까리 MCP 및 내부 도구 호출 | `notebooklm_query` MCP 도구 실행 및 결과 반환 확인 | 4단계 |
| **13** | 장시간 운영 리소스 누수 | 브라우저 미상주 확인, 백엔드 프로세스 메모리 증가량 < 50MB 유지 | 6단계 |
| **14** | 재배포 이후 정상 동작 | 백엔드 재기동 후 저장된 세션 쿠키로 즉시 연속 쿼리 성공 확인 | 6단계 |

---

## 8. 다음 단계(구현) 작업 분해 (WBS 세부 태스크)

다음 구현자가 착수할 작업 트리를 6개 세부 단계로 분해한다:

### Phase 1: 연동 엔진 코어 개발 (`src/ttakkari/notebooklm/core`)
- [ ] 21.1: `constants.py`, `errors.py`, `models.py` 구현
- [ ] 21.2: `auth.py` 구현 (전용 Chrome 프로필 생성, 1회 로그인 팝업, CDP 웹소켓 쿠키 추출, CSRF 갱신)
- [ ] 21.3: `client.py` 구현 (batchexecute RPC 페이로드 직렬화, Anti-XSSI 역직렬화, `httpx` 비동기 통신)
- [ ] 21.4: `NOTEBOOKLM_RPC_OVERRIDES` 핫패치 메커니즘 통합

### Phase 2: 백엔드 도메인 서비스 및 REST API
- [ ] 21.5: `service.py` (`NotebookLMService`) 구현: 노트북 CRUD, 소스 등록, Q&A 스트리밍
- [ ] 21.6: 동시성 제어(세마포어 8건 제한) 및 지수 백오프 재시도 로직 구현
- [ ] 21.7: `src/ttakkari/api/notebooklm.py` REST 라우터 구현 및 FastAPI 앱에 등록
- [ ] 21.8: 단위 테스트 작성 (`tests/test_notebooklm_unit.py`)

### Phase 3: ttakkari MCP 도구 어댑터 등록
- [ ] 21.9: `src/ttakkari/orchestrator/mcp_server.py`에 `notebooklm_*` 도구 7개 등록
- [ ] 21.10: `src/ttakkari/api/assistant.py`의 `TOOLS` 목록에 등록 및 동기화 테스트 통과

### Phase 4: 프론트엔드 'NotebookLM 플랫폼' 페이지 개발
- [ ] 21.11: `front/src/shell/features.ts` 및 `App.tsx` 라우트 등록
- [ ] 21.12: `NotebookLMPage.tsx` 및 대시보드 레이아웃 구현 (노트북 목록, 연결 상태 카드)
- [ ] 21.13: `SourceManager.tsx` (URL/유튜브/파일 업로드 인터페이스)
- [ ] 21.14: `ChatPanel.tsx` 및 `CitationViewer.tsx` (스트리밍 Q&A 및 인용 칩 클릭 원문 하이라이트)

### Phase 5: 지식 원장(Knowledge Ledger) 파이프라인 연계
- [ ] 21.15: Q&A 결과의 citations 블록을 `Unit -> Evidence -> Assertion` 모델로 매핑하는 변환기 구현
- [ ] 21.16: UI에서 '지식 원장으로 보내기' 액션 구현 및 DB 장부 적재 검증

### Phase 6: 실환경 E2E 검증 및 성능 기준선 측정
- [ ] 21.17: 사용자 1회 로그인 수행 후 실제 노트북 생성·Q&A 1회 실행
- [ ] 21.18: 14개 검증 항목 통과 여부 및 P50/P95 응답 시간 실측 기록
