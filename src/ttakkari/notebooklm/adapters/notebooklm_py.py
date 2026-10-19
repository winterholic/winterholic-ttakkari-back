"""`notebooklm-py` 0.8.5 공개 API 어댑터(ADR 0045). NotebookLMBackend 를 구현한다.

- 상류 공개 API(`notebooklm.NotebookLMClient`·`notebooklm.types`·`notebooklm.exceptions`)만 import 한다. private 모듈(`_*`)은 쓰지 않는다.
- 세션은 `from_storage(path=...)` 로 주입한다. 우리가 쓴 storage_state.json 을 상류가 읽고 순환된 쿠키는 같은 파일에 되쓴다.
- 상류 자동 재시도(429·5xx)는 끈다. 재시도 정책은 서비스 계층이 읽기/쓰기를 구분해 소유한다. 429 를 자동으로 다시 두드리지 않는다.
- headless 재인증(브라우저 복구)은 켜지 않는다(`allow_headless` 기본 False). master-token·TLS 위장 extra 는 설치하지 않았다.
- 상류 예외는 도메인 오류로 바꾸고 상류 메시지·응답 원문은 밖으로 내보내지 않는다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from contextlib import AsyncExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ttakkari.google_account import redact
from ttakkari.notebooklm import errors, session
from ttakkari.notebooklm.ports import (
    Capability,
    Citation,
    NotebookDetail,
    NotebookSummary,
    QueryResult,
    SourceInfo,
    SourceRef,
    Span,
)

log = logging.getLogger(__name__)

PIN = "0.8.5"
_PROTECTION = re.compile(r"(?i)captcha|recaptcha|unusual traffic|/sorry/|verify it.?s you|automated queries")


def build_client_config(timeout: float, chat_timeout: float):
    """상류 ClientConfig 하나에 모든 조정값을 담는다.

    from_storage 는 config= 와 낱개 조정 인자(timeout·chat_timeout 등)를 함께 받으면 비기본값이 하나라도 있을 때
    네트워크에 나가기 전에 TypeError 를 낸다(0.8.5). 그래서 낱개 인자는 쓰지 않고 전부 config 로 넘긴다.
    """
    from notebooklm.options import (
        ClientConfig,
        FeatureOptions,
        RetryOptions,
        WebBackendConfig,
        WebTransportOptions,
    )

    return ClientConfig(
        backend=WebBackendConfig(transport=WebTransportOptions(
            read_timeout=timeout, write_timeout=timeout, pool_timeout=timeout)),
        retry=RetryOptions(rate_limit_max_retries=0, server_error_max_retries=0),
        features=FeatureOptions(chat_timeout=chat_timeout),
    )


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat() if isinstance(dt, datetime) else None


def _kind(src_type: Any) -> str:
    v = getattr(src_type, "value", str(src_type or "")).lower()
    if v == "youtube":
        return "youtube"
    if v == "web_page":
        return "web_page"
    if v == "pasted_text":
        return "pasted_text"
    if v == "pdf":
        return "pdf"
    if v in {"google_docs", "google_slides", "google_spreadsheet", "google_drive", "markdown", "docx", "powerpoint",
             "csv", "epub", "excel"}:
        return "document"
    if v in {"google_drive_audio", "google_drive_video", "media", "image"}:
        return "media"
    return "other"


def _state(status: Any) -> str:
    return {"PROCESSING": "processing", "PREPARING": "processing", "READY": "ready", "ERROR": "error"}.get(
        getattr(status, "name", ""), "unknown"
    )


def to_source(s: Any) -> SourceInfo:
    return SourceInfo(
        id=s.id,
        title=s.title,
        kind=_kind(s.kind),  # type: ignore[arg-type]
        url=s.url,
        status=_state(s.status),  # type: ignore[arg-type]
        created_at=_iso(s.created_at),
        word_count=s.word_count,
    )


def to_notebook(n: Any) -> NotebookSummary:
    return NotebookSummary(
        id=n.id,
        title=n.title,
        source_count=n.sources_count or 0,
        created_at=_iso(n.created_at),
        updated_at=_iso(getattr(n, "modified_at", None)),
        is_owner=bool(getattr(n, "is_owner", True)),
    )


def to_query_result(notebook_id: str, question: str, r: Any) -> QueryResult:
    citations: list[Citation] = []
    for ref in r.references or []:
        # answer_start_char/answer_end_char 는 상류가 폐기 예정으로 표시한 별칭이라 쓰지 않는다.
        source_span = (
            Span(start=ref.start_char, end=ref.end_char)
            if ref.start_char is not None and ref.end_char is not None else None
        )
        answer_span = (
            Span(start=ref.answer_anchor_start, end=ref.answer_anchor_end)
            if ref.answer_anchor_start is not None and ref.answer_anchor_end is not None else None
        )
        citations.append(Citation(
            number=ref.citation_number, source_id=ref.source_id, quoted_text=ref.cited_text,
            source_span=source_span, answer_span=answer_span, chunk_id=ref.chunk_id, score=ref.score))
    numbers: dict[str, list[int]] = {}
    for c in citations:
        nums = numbers.setdefault(c.source_id, [])
        if c.number is not None and c.number not in nums:
            nums.append(c.number)
    return QueryResult(
        notebook_id=notebook_id, query=question, answer=r.answer or "", conversation_id=r.conversation_id,
        turn_number=r.turn_number, citations=citations,
        sources=[SourceRef(id=sid, citation_numbers=sorted(nums)) for sid, nums in numbers.items()],
        created_at=datetime.now(UTC).isoformat())


def map_exception(exc: BaseException, *, write: bool = False) -> errors.NotebookLMError:
    """상류·전송 예외를 도메인 오류로. 상류 메시지는 쓰지 않는다(쿠키·응답 조각이 섞일 수 있다)."""
    import notebooklm as up

    if isinstance(exc, errors.NotebookLMError):
        return exc
    text = str(exc)
    if _PROTECTION.search(text):
        return errors.ProtectionSignal("Google 보안 확인(CAPTCHA 등) 신호가 감지되어 요청을 멈췄습니다. 직접 확인해 주세요.")
    if isinstance(exc, (up.AuthError, up.AuthExtractionError)):
        return errors.AuthRequired("NotebookLM 로그인이 필요합니다. 로그인 창에서 다시 로그인해 주세요.")
    if isinstance(exc, up.RateLimitError):
        return errors.RateLimited("NotebookLM 사용 제한에 걸렸습니다. 잠시 쉬었다가 다시 시도합니다.",
                                  retry_after=getattr(exc, "retry_after", None))
    if isinstance(exc, up.NotFoundError):
        return errors.NotFound("대상을 찾지 못했습니다(삭제됐거나 ID가 틀림).")
    if isinstance(exc, up.UnsupportedOperationError):
        return errors.Unsupported("상류 라이브러리가 이 기능을 지원하지 않습니다.")
    if isinstance(exc, (up.ValidationError, up.SourceAddError)):
        return errors.Invalid("NotebookLM이 요청 내용을 받아들이지 않았습니다.")
    if isinstance(exc, (up.RPCTimeoutError, up.WaitTimeoutError, TimeoutError, asyncio.TimeoutError, httpx.TimeoutException)):
        return errors.Timeout("NotebookLM 응답을 기다리다 시간이 지났습니다.", outcome_unknown=write)
    if isinstance(exc, (up.NetworkError, httpx.TransportError)):
        return errors.Transient("NotebookLM과 연결하지 못했습니다.", outcome_unknown=write)
    if isinstance(exc, up.ServerError):
        return errors.Upstream("NotebookLM 서버 오류입니다.", outcome_unknown=write)
    if isinstance(exc, up.ClientError):
        return errors.UpstreamRejected("NotebookLM이 요청을 거절했습니다.")
    detail = redact.safe_error(exc)
    if isinstance(exc, up.NotebookLMError):
        log.warning("notebooklm upstream error: %s", detail)
        return errors.Upstream(f"NotebookLM 처리 중 오류가 발생했습니다({type(exc).__name__}).",
                               outcome_unknown=write, detail=detail)
    # 상류·전송 계층 예외가 아니면 Google 쪽 문제가 아니라 우리 연동 코드나 설정의 결함이다. 원인이 묻히지 않게 남긴다.
    log.error("notebooklm integration error: %s", detail, exc_info=exc)
    return errors.IntegrationError(f"따까리의 NotebookLM 연동 코드 오류입니다({type(exc).__name__}). Google 요청은 보내지 않았거나 "
                                   "결과를 알 수 없습니다.", outcome_unknown=write, detail=detail)


class NotebookLMPyBackend:
    """계정 1개의 세션 파일로 상류 클라이언트를 하나 열어 두고 재사용한다(요청마다 홈페이지 토큰 조회를 반복하지 않는다)."""

    name = f"notebooklm-py=={PIN}"

    def __init__(self, account_id: str, *, timeout: float = 30.0, chat_timeout: float = 120.0) -> None:
        self.account_id = account_id
        self._timeout = timeout
        self._chat_timeout = chat_timeout
        self._stack: AsyncExitStack | None = None
        self._client: Any = None
        self._lock = asyncio.Lock()

    async def _open(self) -> Any:
        async with self._lock:
            if self._client is not None:
                return self._client
            if not session.exists(self.account_id):
                raise errors.AuthRequired("NotebookLM 로그인 세션이 없습니다. 로그인 창에서 먼저 로그인해 주세요.")
            session.load_secrets(self.account_id)
            from notebooklm import NotebookLMClient

            stack = AsyncExitStack()
            try:
                client = await stack.enter_async_context(NotebookLMClient.from_storage(
                    path=str(session.storage_path(self.account_id)),
                    config=build_client_config(self._timeout, self._chat_timeout)))
            except BaseException as e:  # noqa: BLE001 -- 열기 실패는 도메인 오류로 바꾸되 취소는 그대로 전파한다
                with contextlib.suppress(Exception):
                    await stack.aclose()
                raise map_exception(e) from None
            self._stack, self._client = stack, client
            return client

    async def aclose(self) -> None:
        async with self._lock:
            stack, self._stack, self._client = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def _run(self, coro_fn, *, write: bool = False):
        client = await self._open()
        try:
            return await coro_fn(client)
        except asyncio.CancelledError:
            raise
        except BaseException as e:  # noqa: BLE001 -- 상류 예외를 모두 도메인 오류로 바꾼다(취소는 위에서 전파)
            raise map_exception(e, write=write) from None

    async def verify_session(self) -> None:
        # 클라이언트를 여는 것만으로 홈페이지 토큰 조회가 일어난다(로그인 상태가 아니면 AuthError).
        await self._open()

    async def list_notebooks(self) -> list[NotebookSummary]:
        return [to_notebook(n) for n in await self._run(lambda c: c.notebooks.list())]

    async def get_notebook(self, notebook_id: str) -> NotebookDetail:
        async def go(c):
            nb = await c.notebooks.get(notebook_id)
            srcs = await c.sources.list(notebook_id)
            return nb, srcs

        nb, srcs = await self._run(go)
        base = to_notebook(nb)
        return NotebookDetail(**base.model_dump(), sources=[to_source(s) for s in srcs])

    async def create_notebook(self, title: str) -> NotebookSummary:
        return to_notebook(await self._run(lambda c: c.notebooks.create(title), write=True))

    async def rename_notebook(self, notebook_id: str, title: str) -> NotebookSummary:
        return to_notebook(await self._run(lambda c: c.notebooks.rename(notebook_id, title), write=True))

    async def delete_notebook(self, notebook_id: str) -> None:
        await self._run(lambda c: c.notebooks.delete(notebook_id), write=True)

    async def list_sources(self, notebook_id: str) -> list[SourceInfo]:
        return [to_source(s) for s in await self._run(lambda c: c.sources.list(notebook_id))]

    async def add_url(self, notebook_id: str, url: str, *, title: str | None = None) -> SourceInfo:
        return to_source(await self._run(
            lambda c: c.sources.add_url(notebook_id, url, wait=False, title=title), write=True))

    async def add_text(self, notebook_id: str, title: str, text: str) -> SourceInfo:
        # idempotent=False: 같은 본문 재전송 방지는 서비스의 중복 원장이 맡고, 상류의 자동 재전송 경로를 켜지 않는다.
        return to_source(await self._run(
            lambda c: c.sources.add_text(notebook_id, title, text, wait=False, idempotent=False), write=True))

    async def add_file(self, notebook_id: str, path: Path, *, title: str | None = None) -> SourceInfo:
        return to_source(await self._run(
            lambda c: c.sources.add_file(notebook_id, path, wait=False, title=title), write=True))

    async def wait_source_ready(self, notebook_id: str, source_id: str, *, timeout: float) -> SourceInfo:
        import notebooklm as up

        async def go(c):
            try:
                return await c.sources.wait_until_ready(notebook_id, source_id, timeout=timeout)
            except up.SourceProcessingError:
                raise errors.UpstreamRejected("NotebookLM이 이 소스를 처리하지 못했습니다.") from None

        return to_source(await self._run(go))

    async def delete_source(self, notebook_id: str, source_id: str) -> None:
        await self._run(lambda c: c.sources.delete(notebook_id, source_id), write=True)

    async def ask(self, notebook_id: str, question: str, *, conversation_id: str | None = None,
                  source_ids: list[str] | None = None) -> QueryResult:
        # 질의는 서버 쪽 대화 기록과 할당량을 쓰므로 쓰기처럼 다룬다(결과 불명이면 재전송 금지).
        r = await self._run(
            lambda c: c.chat.ask(notebook_id, question, source_ids=source_ids, conversation_id=conversation_id),
            write=True)
        return to_query_result(notebook_id, question, r)

    def capabilities(self) -> list[Capability]:
        unavailable = "상류 공개 API에는 있으나 이번 단계 범위 밖이라 연결하지 않았습니다."
        return [
            Capability(name="notebooks", available=True),
            Capability(name="sources.url", available=True),
            Capability(name="sources.youtube", available=True),
            Capability(name="sources.text", available=True),
            Capability(name="sources.file", available=True),
            Capability(name="query.citations", available=True),
            Capability(name="audio_overview", available=False, reason=unavailable),
            Capability(name="video_overview", available=False, reason=unavailable),
            Capability(name="research", available=False, reason=unavailable),
            Capability(name="drive_sources", available=False, reason=unavailable),
            Capability(name="sharing", available=False, reason=unavailable),
        ]
