"""NotebookLM 내부 공통 계약(ADR 0045). 서비스·REST·MCP 는 이 모듈의 타입과 Protocol 만 안다.

상류 라이브러리(`notebooklm-py`)든 나중에 허용되는 다른 구현이든 NotebookLMBackend 를 만족하면 상위 코드는 바뀌지 않는다.
상류 타입은 어댑터 밖으로 나오지 않는다. 좌표 단위는 상류가 문서화한 그대로 두고 단위를 꾸며내지 않는다.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

SourceKind = Literal["web_page", "youtube", "pasted_text", "pdf", "document", "media", "other"]
SourceState = Literal["processing", "ready", "error", "unknown"]


class Span(BaseModel):
    start: int
    end: int


class NotebookSummary(BaseModel):
    id: str
    title: str
    source_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    is_owner: bool = True


class SourceInfo(BaseModel):
    id: str
    title: str | None = None
    kind: SourceKind = "other"
    url: str | None = None
    status: SourceState = "unknown"
    created_at: str | None = None
    word_count: int | None = None


class NotebookDetail(NotebookSummary):
    sources: list[SourceInfo] = Field(default_factory=list)


class Citation(BaseModel):
    """답변 속 인용 하나. 원문 좌표와 답변 좌표는 서로 다른 좌표계라 따로 둔다."""

    number: int | None = None  # 답변 본문의 [n] 마커 번호
    source_id: str
    quoted_text: str | None = None  # 상류가 돌려준 원문 인용 텍스트(없으면 None, 꾸며내지 않는다)
    source_span: Span | None = None  # 원문 문서 좌표(상류 문서화 기준 UTF-16 code unit)
    answer_span: Span | None = None  # 답변 본문에서 이 인용이 받치는 범위(상류 좌표, 단위 미검증)
    chunk_id: str | None = None
    score: float | None = None


class SourceRef(BaseModel):
    """답변이 인용한 출처 요약."""

    id: str
    title: str | None = None
    url: str | None = None
    kind: SourceKind = "other"
    citation_numbers: list[int] = Field(default_factory=list)


class QueryResult(BaseModel):
    """질의 결과. answer(NotebookLM 분석)·citations(인용)·sources(출처)를 섞지 않는다."""

    notebook_id: str
    query: str
    answer: str
    conversation_id: str | None = None
    turn_number: int | None = None
    citations: list[Citation] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    created_at: str


class Capability(BaseModel):
    name: str
    available: bool
    reason: str | None = None


@runtime_checkable
class NotebookLMBackend(Protocol):
    """NotebookLM 을 실제로 호출하는 구현의 계약. 모든 메서드는 errors.py 의 도메인 오류만 던진다."""

    name: str

    async def verify_session(self) -> None:
        """저장된 세션으로 NotebookLM 에 들어갈 수 있는지 가볍게 확인한다. 안 되면 AuthRequired."""

    async def list_notebooks(self) -> list[NotebookSummary]: ...
    async def get_notebook(self, notebook_id: str) -> NotebookDetail: ...
    async def create_notebook(self, title: str) -> NotebookSummary: ...
    async def rename_notebook(self, notebook_id: str, title: str) -> NotebookSummary: ...
    async def delete_notebook(self, notebook_id: str) -> None: ...

    async def list_sources(self, notebook_id: str) -> list[SourceInfo]: ...
    async def add_url(self, notebook_id: str, url: str, *, title: str | None = None) -> SourceInfo:
        """URL(웹·YouTube) 소스 등록 요청만 보낸다. 처리 완료 대기는 wait_source_ready."""

    async def add_text(self, notebook_id: str, title: str, text: str) -> SourceInfo: ...
    async def add_file(self, notebook_id: str, path: Path, *, title: str | None = None) -> SourceInfo: ...
    async def wait_source_ready(self, notebook_id: str, source_id: str, *, timeout: float) -> SourceInfo: ...
    async def delete_source(self, notebook_id: str, source_id: str) -> None: ...

    async def ask(
        self, notebook_id: str, question: str, *, conversation_id: str | None = None, source_ids: list[str] | None = None
    ) -> QueryResult: ...

    def capabilities(self) -> list[Capability]:
        """이 구현이 실제로 제공하는 기능과 제공하지 않는 이유. 지원하지 않는 기능을 지원하는 척하지 않는다."""

    async def aclose(self) -> None: ...
