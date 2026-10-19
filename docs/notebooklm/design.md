# NotebookLM 연동 엔진 및 플랫폼 상세 설계서

작성일자: 2026-10-19  
작성자: 따까리(winterholic-ttakkari) 분석·설계 에이전트  
문서 상태: 승인 대기 / 1단계 산출물

---

## 1. 아키텍처 개요 및 모듈 구조

본 초안은 상류 `notebooklm-py` 공개 API를 따까리 내부 계약 뒤에 두는 방향을 제안한다. 전용 Chrome 프로필의 수동 로그인 및 CDP 세션 획득이 요구사항·보호 정책과 양립하는지는 구현 전 호환성 검증이 필요하다. 실제 비용, 성능, 정책 적합성은 미검증이다.

### 1.1 백엔드 패키지 레이아웃 (`winterholic-ttakkari/back`)
```
src/ttakkari/notebooklm/
├── __init__.py                # 내부 공개 계약
├── ports.py                   # NotebookLMBackend Protocol과 도메인 DTO
├── adapters/
│   └── notebooklm_py.py       # 고정한 상류 공개 API 어댑터
├── errors.py                  # 도메인/상류 오류 변환
├── auth.py                    # 수동 로그인/CDP 호환성 검증 뒤 범위 확정
└── service.py                 # 도메인 서비스 (job/정책은 별도 결정)

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
    # 로컬 내부 상태이며 API/MCP 응답에는 경로를 내보내지 않는다.


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
    citation_number: int
    source_fragment_start: int | None = None
    source_fragment_end: int | None = None
    quote_text: str | None = None
    # 답변 본문 내 인용 마커 범위와 원문 인용 범위는 다른 좌표계다.
    answer_anchor_start: int | None = None
    answer_anchor_end: int | None = None


class QueryResponse(BaseModel):
    turn_id: str
    query: str
    answer: str
    citations: list[CitationSpan] = Field(default_factory=list)
    created_at: str


class KnowledgeSyncRequest(BaseModel):
    notebook_id: str
    turn_id: str
    scope: str = Field(default="personal", description="personal | work")
    # assertion은 별도 추출·검토 결과로 받는다. NotebookLM 답변만으로
    # 사실을 확정하거나 사용자 확인 사실로 승격하지 않는다.
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
        """노트북 목록 조회. 캐시 정책과 성능 목표는 실측 뒤 결정."""
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
| `PATCH` | `/notebooks/{id}` | 노트북 제목 변경 (`{ title: string }`) |
| `DELETE`| `/notebooks/{id}` | 노트북 삭제 |
| `POST` | `/notebooks/{id}/sources/url` | 웹/유튜브 URL 소스 추가 |
| `POST` | `/notebooks/{id}/sources/text` | 텍스트 소스 추가 |
| `POST` | `/notebooks/{id}/sources/upload` | 파일 업로드 (multipart/form-data) |
| `DELETE`| `/notebooks/{id}/sources/{sourceId}` | 소스 삭제 |
| `GET` | `/jobs/{jobId}` | 장시간 작업 상태 조회 (저장 모델·전이 규칙 확정 필요) |
| `POST` | `/notebooks/{id}/query` | Q&A 질문 전송 및 답변/인용 수신 |
| `POST` | `/notebooks/{id}/sync-knowledge` | 지식 원장으로 분석 결과 전달 |

---

## 5. 따까리 MCP 도구 어댑터 명세

아래는 후보 목록이다. 상위 어댑터 계약과 구현 범위 확정 후 최종 도구 집합을 정한다. 삭제·업로드 등 쓰기 작업에는 확인·중복 방지 정책을 적용한다.

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
5. **`notebooklm_rename` / `notebooklm_delete`**:
   - 설명: 노트북 제목 변경 또는 노트북 삭제.
   - 파라미터: `notebook_id: str`, 변경 시 `title: str`.
6. **`notebooklm_add_source`**:
   - 설명: 노트북에 웹 URL, 유튜브 링크, 또는 텍스트 소스를 추가한다.
   - 파라미터: `notebook_id: str`, `source_type: "url"|"youtube"|"text"`, `url: str | None`, `text: str | None`, `title: str | None`
7. **`notebooklm_delete_source`**:
   - 설명: 특정 노트북 소스를 삭제한다.
   - 파라미터: `notebook_id: str`, `source_id: str`.
8. **`notebooklm_query`**:
   - 설명: 노트북에 등록된 소스를 기반으로 RAG 질문을 수행하고, 답변과 출처 인용 목록을 받는다.
   - 파라미터: `notebook_id: str`, `query: str`
9. **`notebooklm_export_knowledge`**:
   - 설명: 노트북의 특정 분석/질의 결과를 따까리 지식 원장으로 동기화한다.
   - 파라미터: `notebook_id: str`, `turn_id: str`, `scope: "personal"|"work"`; 별도 추출·검토된 assertion 데이터 계약이 필요하다.

후보 목록에는 파일 업로드와 작업 상태 조회 MCP 도구가 없다. 파일 업로드는 경로 권한·전송 방식을, 장시간 작업은 job lifecycle을 먼저 정한다. MCP와 REST의 기능 차이, 쓰기 도구의 확인 정책을 확정하기 전에는 완전한 도구 명세로 보지 않는다.

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
| **1** | 기존 Google 계정 인증 | 요구한 수동 로그인 흐름이 보호 정책에 맞고 상류 공개 API에 연결되는지 먼저 검증. 통과 시에만 비밀값을 출력하지 않는 저장·권한 검증 | 2단계 |
| **2** | 별도 유료 API 미사용 | 의존성·환경 설정·코드 경로를 감사해 Gemini API 및 유료 생성 API 호출이 없음을 확인. 실제 제품 요금/할당량은 별도 미검증 | 2단계 |
| **3** | 노트북 생성·조회 | 상류 공개 API로 생성·목록을 수행하고 반환 ID/제목을 NotebookLM UI와 비교. 내부 RPC ID는 테스트 계약으로 고정하지 않음 | 2단계 |
| **4** | 유튜브 URL 등록 | 공개 API로 YouTube URL 소스를 추가하고 NotebookLM 반환 소스와 웹 UI를 대조 | 2단계 |
| **5** | 웹페이지·문서 등록 | 지원되는 URL/파일 유형별 등록·처리 완료·오류 상태를 확인. 지원되지 않는 유형은 unavailable 처리 | 3단계 |
| **6** | 질의응답·결과 추출 | 공개 API로 질문하고 답변을 수신·취소·타임아웃 처리 | 3단계 |
| **7** | 출처·인용 보존 | 반환 citation/reference에서 source ID·source span·answer marker span을 분리해 보존 | 3단계 |
| **8** | 인증 세션 재사용 | 브라우저 종료 후 재시작 전/후 요청을 검사하고, 계정·세션 만료 시 자동 복구가 보호 신호를 만들지 않는지 확인. 24시간은 합의된 만료 기준이 아님 | 3단계 |
| **9** | 중복 요청 방지 | 계정별 정한 동시성 한도와 동일 job/idempotency key 처리·다중 프로세스 동작 확인 | 4단계 |
| **10** | 오류·타임아웃 처리 | 네트워크 단절 및 타임아웃 시 `TransientBackendError` 안전 처리 확인 | 4단계 |
| **11** | 요청 제한 시 안전한 처리 | 429/RESOURCE_EXHAUSTED에서 정한 유한 대기·취소 동작 확인. 반복 재시도가 한도나 보호 정책을 우회하지 않는지 확인 | 4단계 |
| **12** | 따까리 MCP 및 내부 도구 호출 | `notebooklm_query` MCP 도구 실행 및 결과 반환 확인 | 4단계 |
| **13** | 장시간 운영 리소스 누수 | 시험 시간·요청 부하·RSS 기준선을 정한 뒤 브라우저 프로세스 생존 여부와 RSS 추이를 호스트에서 측정. `<50MB` 근거는 현재 없음 | 6단계 |
| **14** | 재배포 이후 정상 동작 | 안전한 세션 저장·복구와 계정 재인증 필요 상태를 구분해 재기동 검증. 성공 여부는 실계정 확인 전 미검증 | 6단계 |

## 9. 독립 검증으로 추가된 구현 전 미결정 사항

이 항목들은 아직 구현 보장이나 실측 결과가 아니다. 실제 계정 요청은 별도 승인/정책 검토와 단계별 검증 조건을 충족하기 전 수행하지 않는다.

1. **상류 경계**: `notebooklm-py` 안정 공개 API의 정확한 버전, 버전 고정·업그레이드 정책, RPC 드리프트 호환성 테스트 범위. private RPC 모듈 복사·import는 금지한다. 인증 세션은 공개 API에 주입할 수 있는지 spike로 확인한다.
2. **백엔드 교체 가능성**: 라우터·서비스가 직접 상류 타입을 노출하지 않도록 `NotebookLMBackend` Protocol을 정의한다. 상류 클라이언트와 향후 허용된 브라우저 구현은 이 계약을 구현하고, 상위 서비스/REST/MCP는 계약만 의존한다. 브라우저 구현은 Google 보호장치 우회 없이 가능한 범위만 제공한다.
3. **세션 저장**: 한 Google 계정만 지원할지, 계정 ID를 데이터/잠금/작업에 어떻게 결합할지 정한다. 쿠키 영속화 방법, 암호화 키의 수명·저장 위치, 파일/상위 디렉터리 권한(최소 소유자 전용), 원자적 교체·백업·삭제, 쿠키와 CSRF/세션 토큰의 로그·예외·응답 마스킹을 명세한다. 현재 ADR의 “암호화된 파일”은 키 관리가 없어 보장되지 않는다.
4. **로그인 수명주기**: Chrome 바이너리 경로, 전용 프로필 생성, 포트 바인딩 범위(루프백 전용), 준비/로그인 완료/취소/타임아웃 판정, 창 종료, 중복 실행 잠금과 재시작 복구를 정한다. 외부 네트워크·인증 감지·CAPTCHA 등 보호 신호에는 중단(fail closed)한다.
5. **작업 모델**: 노트북 생성·소스 처리·Q&A 중 어떤 요청을 동기/스트리밍/비동기 job으로 제공할지 정한다. 비동기 작업은 DB 모델, 상태 전이(queued/running/succeeded/failed/cancelled/unknown), 진행/결과/오류 마스킹, 재시작 복구와 조회 API를 정의한다. 이 문서에는 현재 작업 테이블이나 중복 제거 키가 없다.
6. **재시도·동시성**: 계정 단위 동시 실행 한도와 그 저장 범위(프로세스/DB)를 정한다. 읽기만 안전 재시도하고, 쓰기 요청은 idempotency key 또는 결과 판별 후 재전송한다. 429·RESOURCE_EXHAUSTED는 횟수·최대 대기·Retry-After·쿨다운·취소를 정하되 Google 사용 제한을 우회하려고 재시도하지 않는다. 고정 8건과 즉시 지수 재시도는 아직 근거가 없다.
7. **REST/MCP 계약**: `/auth/refresh`가 새 인증을 뜻하는지 캐시 상태 갱신인지 정정한다. notebook/source rename·delete, upload, job 조회와 오류 코드·페이징·요청 크기·취소·idempotency를 맞춘다. MCP 도구와 `api/assistant.py`의 사용자 노출 도구 메타데이터는 동기화 검증한다.
8. **인용·지식 원장**: 인용 응답에서 확인 가능한 source ID, 원문 스팬, 답변 내 마커 범위를 각각 보존한다. NotebookLM 답변(분석), 가져온 원문, 인용 연결, 자동 추출 assertion을 별도 source/revision/unit/evidence 레코드로 적재한다. 생성된 assertion은 검토 대기로 남기며 사용자 확인 사실로 자동 승격하지 않는다. 원문 접근 불가·인용 누락·offset 변환 실패 때 원문 하이라이트를 꾸며내지 않는다.
9. **지원 기능 목록**: 안정된 상류 공개 API가 제공하는 범위만 1차 UI/MCP에 노출한다. audio/video/research/Drive와 파일 유형별 지원은 릴리스별로 확인하고, 지원하지 않는 기능은 UI에서 숨기거나 명시적으로 unavailable로 표시한다.
10. **성능·검증**: 브라우저 비상주 메모리, P50/P95, 오류율은 기준 부하·기간·반복 수·계정 사용량·중단 기준을 정한 뒤 호스트에서 실측한다. 테스트에서 실제 Google 보호 신호 또는 사용자 계정 상태가 변하면 중단한다. 지금 문서의 14항목은 테스트 설계 초안이며 아직 통과 결과가 아니다.

---

## 10. 다음 단계(구현) 작업 분해 (WBS 세부 태스크)

다음 구현자가 착수할 작업 트리를 6개 세부 단계로 분해한다:

### Phase 1: 상류 의존성과 인증 호환성 검증
- [ ] 21.1: `notebooklm-py` 공개 API의 배포 버전과 고정 정책 확정
- [ ] 21.2: 임의 세션 입력/수동 CDP 로그인 호환성 spike. Google 계정 요청은 승인·보호 정책 게이트 이후 수행
- [ ] 21.3: `NotebookLMBackend` Protocol과 상류 오류/DTO 매핑 설계
- [ ] 21.4: 세션 저장·로그 마스킹·권한·계정 격리 결정 기록

### Phase 2: 백엔드 도메인 서비스 및 REST API
- [ ] 21.5: `service.py` (`NotebookLMService`) 구현: 확정된 상류 지원 범위의 노트북·소스·Q&A
- [ ] 21.6: 영속 job 필요 여부, idempotency, 계정 범위 동시성·유한 재시도 정책 구현
- [ ] 21.7: `src/ttakkari/api/notebooklm.py` REST 라우터 구현 및 FastAPI 앱에 등록
- [ ] 21.8: 단위 테스트 작성 (`tests/test_notebooklm_unit.py`)

### Phase 3: ttakkari MCP 도구 어댑터 등록
- [ ] 21.9: `src/ttakkari/orchestrator/mcp_server.py`에 범위 확정된 `notebooklm_*` MCP 도구 등록
- [ ] 21.10: `src/ttakkari/api/assistant.py`의 `TOOLS` 목록에 등록 및 동기화 테스트 통과

### Phase 4: 프론트엔드 'NotebookLM 플랫폼' 페이지 개발
- [ ] 21.11: `front/src/shell/features.ts` 및 `App.tsx` 라우트 등록
- [ ] 21.12: `NotebookLMPage.tsx` 및 대시보드 레이아웃 구현 (노트북 목록, 연결 상태 카드)
- [ ] 21.13: `SourceManager.tsx` (URL/유튜브/파일 업로드 인터페이스)
- [ ] 21.14: `ChatPanel.tsx` 및 `CitationViewer.tsx` (스트리밍 Q&A 및 인용 칩 클릭 원문 하이라이트)

### Phase 5: 지식 원장(Knowledge Ledger) 파이프라인 연계
- [ ] 21.15: 원문·NotebookLM 분석·인용·검토 대기 Assertion을 구분해 지식 원장 모델에 매핑
- [ ] 21.16: UI에서 '지식 원장으로 보내기' 액션 구현 및 DB 장부 적재 검증

### Phase 6: 실환경 E2E 검증 및 성능 기준선 측정
- [ ] 21.17: 정책/인증 게이트 통과 후 실계정 생성·Q&A를 최소 범위로 검증
- [ ] 21.18: 14개 검증을 실행하고 기준선·조건과 P50/P95를 실제 측정값으로 기록
