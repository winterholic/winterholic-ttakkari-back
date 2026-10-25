"""KB v3 통합 등록(ingest): 텍스트·URL·여러 파일을 하나의 입구로 받아 자동 지식화 파이프라인에 넣는다(KB3-402).

원칙
- 유형은 확장자·MIME·바이트 모양으로 자동 감지한다. 지원 파서가 없거나 파서가 보류(OCR 필요, STT 없음, 구형 Office)를 알려 주면
  성공처럼 보이지 않는다: 원본은 보관하고 파이프라인 작업을 held(+사유)로 만든다. 검색 가능한 패시지는 만들지 않는다.
- 항목마다 독립이다. 한 항목의 실패가 다른 항목의 등록을 막지 않고, 항목별 상태(registered/duplicate/held/failed)를 돌려준다.
- 같은 바이트를 다시 올리면 duplicate 로 기존 문서·작업을 돌려준다(멱등).
- 접근 범위는 웹 세션 기준(personal 출처, global 가시성)이다. 회사 범위로 올리는 입구가 없다.
- URL 은 사용자가 직접 지정한 한 페이지를 SSRF 방어 fetcher(공개 주소만, 크기·시간 제한, 리다이렉트 재검증)로 가져온다. 수집기의 권리
  정책(source policy) 경로가 아니라 사용자가 저장하라고 한 단일 페이지이며 robots.txt 는 확인하지 않는다.
"""

from __future__ import annotations

import hashlib
import logging
import posixpath
import re
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector import parse as collector_parse
from ttakkari.collector import textproc as collector_textproc
from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_parsers import DEFAULT_LIMITS, ParseStatus, create_default_registry
from ttakkari.kb_pipeline import enqueue_pipeline_job
from ttakkari.kb_pipeline.types import (
    JOB_STATUS_HELD,
    STAGE_PARSING,
    STAGE_STATUS_HELD,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.kb_store import KbInputError, ingest_source, segment_revision
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

MAX_ITEMS_PER_BATCH = 20
MAX_FILE_BYTES = DEFAULT_LIMITS.max_file_bytes
MAX_TEXT_CHARS = 2_000_000
HELD_PARSER_VERSION = "held-v1"
URL_FETCH_USER_AGENT = "ttakkari-kb-ingest/1.0 (+user-directed single page save)"

STATUS_REGISTERED = "registered"
STATUS_DUPLICATE = "duplicate"
STATUS_HELD = "held"
STATUS_FAILED = "failed"

# 보류 사유 -> 사용자에게 보여 줄 한국어 문구(본문 스텁과 응답에 쓴다)
HOLD_REASON_TEXT = {
    "ocr_required": "스캔 이미지라 글자를 읽으려면 OCR이 필요한데 아직 지원하지 않아요",
    "stt_runtime_unavailable": "음성·영상 전사에 필요한 로컬 STT 런타임이 설치되어 있지 않아요",
    "unsupported_legacy_office": "구형 Office 형식(.doc/.ppt/.xls/.hwp)은 아직 지원하지 않아요",
    "unsupported_format": "이 파일 형식은 아직 지원하지 않아요",
}
_SOURCE_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,38}$")
_GENERIC_CONTENT_TYPES = {"", "application/octet-stream", "binary/octet-stream", "application/x-www-form-urlencoded"}
_HTML_SNIFF = re.compile(rb"^\s*(?:<!doctype\s+html|<html|<head|<body)", re.IGNORECASE)


@dataclass
class IngestInput:
    """ingest 한 항목. kind 는 text|file|url 이다."""

    kind: str
    name: str
    data: bytes = b""
    content_type: str | None = None
    url: str | None = None
    # URL 을 가져오지 못한 항목: 배치 안의 순서를 유지하려고 실패 사유를 들고 다니며 항목별 failed 로 보고한다.
    error: str | None = None


@dataclass
class ItemOutcome:
    name: str
    status: str
    document_id: uuid.UUID | None = None
    revision_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    detected_type: str | None = None
    hold_reason: str | None = None
    error: str | None = None
    detail: str | None = None
    created: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "status": self.status,
            "document_id": str(self.document_id) if self.document_id else None,
            "revision_id": str(self.revision_id) if self.revision_id else None,
            "job_id": str(self.job_id) if self.job_id else None,
            "detected_type": self.detected_type,
        }
        if self.hold_reason:
            out["hold_reason"] = self.hold_reason
            out["hold_message"] = HOLD_REASON_TEXT.get(self.hold_reason, self.hold_reason)
        if self.error:
            out["error"] = self.error
        if self.detail:
            out["detail"] = self.detail
        if self.status in (STATUS_REGISTERED, STATUS_DUPLICATE, STATUS_HELD):
            out["created"] = self.created
        out.update(self.extra)
        return out


@dataclass
class Detected:
    """유형 감지 결과. route 는 html|parser|text|unsupported 이다."""

    route: str
    label: str
    media_type: str
    parser: Any = None
    filename: str | None = None
    content_type: str | None = None  # 파서에 넘길 MIME(일반 MIME 은 None)


def guess_lang(text: str) -> str | None:
    sample = text[:4000]
    letters = [c for c in sample if c.isalpha()]
    if not letters:
        return None
    hangul = sum(1 for c in letters if "가" <= c <= "힣")
    return "ko" if hangul / len(letters) > 0.1 else "en"


def _clean_filename(name: str | None) -> str:
    base = posixpath.basename((name or "").replace("\\", "/")).strip()
    base = unicodedata.normalize("NFC", base).replace("\x00", "")
    return base[:200] or "upload"


def _looks_like_text(data: bytes) -> bool:
    if not data or b"\x00" in data[:8192]:
        return False
    sample = data[:8192]
    try:
        decoded = sample.decode("utf-8")
    except UnicodeDecodeError as exc:
        # 8192 바이트 경계에서 멀티바이트 문자가 잘린 경우만 허용한다.
        if exc.start < len(sample) - 4:
            return False
        decoded = sample[: exc.start].decode("utf-8", errors="ignore")
    printable = sum(1 for c in decoded if c.isprintable() or c in "\n\r\t")
    return printable / max(1, len(decoded)) > 0.95


def detect_type(filename: str | None, content_type: str | None, data: bytes) -> Detected:
    """확장자·MIME·바이트 모양으로 처리 경로를 정한다. 확장자가 가장 믿을 만하고, 일반 MIME(octet-stream)은 무시한다."""
    registry = create_default_registry()
    ct = (content_type or "").split(";")[0].strip().lower()
    name = filename or ""
    suffix = PurePosixPath(name).suffix.lower()
    if suffix in {".html", ".htm"} or ct in {"text/html", "application/xhtml+xml"} or (
        not suffix and _HTML_SNIFF.match(data[:512])
    ):
        return Detected("html", "html", "text/html", filename=name)
    if not suffix and data[:5] == b"%PDF-":
        name, suffix = (name or "upload") + ".pdf", ".pdf"
    mime = None if ct in _GENERIC_CONTENT_TYPES else ct
    parser = registry.find_parser(content_type=mime, filename=name or None, sample_bytes=data[:1024])
    if parser is not None:
        label = suffix.lstrip(".") or parser.name
        return Detected("parser", label, mime or _media_for(suffix), parser, filename=name or None, content_type=mime)
    if _looks_like_text(data):
        return Detected("text", "text", "text/plain", filename="note.txt", content_type="text/plain")
    return Detected("unsupported", suffix.lstrip(".") or "binary", ct or "application/octet-stream")


def _media_for(suffix: str) -> str:
    return {
        ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".rst": "text/x-rst",
        ".pdf": "application/pdf", ".csv": "text/csv", ".tsv": "text/tab-separated-values",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".srt": "application/x-subrip", ".vtt": "text/vtt",
    }.get(suffix, "application/octet-stream")


def default_title(item: IngestInput, text: str | None = None) -> str:
    if item.kind == "text":
        for line in (text or "").splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                return stripped[:80]
        return "텍스트 메모"
    stem = PurePosixPath(item.name).stem or item.name
    return stem[:200] or "문서"


async def _ensure_access(db: AsyncSession, document_id: uuid.UUID) -> None:
    if await db.get(KbDocumentAccess, document_id) is None:
        # 웹에서 올린 자료: 개인 출처, 모든 세션이 볼 수 있는 일반 지식(global). 회사 범위 입구는 없다.
        db.add(KbDocumentAccess(
            document_id=document_id, tenant_id="owner", scope="personal", origin_scope="personal", visibility="global",
        ))
        await db.flush()


async def _job_for(db: AsyncSession, revision_id: uuid.UUID) -> KbPipelineJob | None:
    return await db.scalar(select(KbPipelineJob).where(KbPipelineJob.revision_id == revision_id))


async def _mark_job_held(db: AsyncSession, job: KbPipelineJob, reason: str, detail: dict[str, Any]) -> None:
    """접수 시점에 이미 보류가 확정된 작업을 held 로 만든다. 처리기가 돌지 않으므로 parsing 단계도 held 로 기록한다."""
    now = utcnow()
    job.status = JOB_STATUS_HELD
    job.current_stage = STAGE_PARSING
    job.hold_reason = reason[:64]
    job.error_detail = HOLD_REASON_TEXT.get(reason, reason)
    job.completed_at = None
    stage = await db.scalar(
        select(KbPipelineStage).where(KbPipelineStage.job_id == job.id, KbPipelineStage.stage == STAGE_PARSING)
    )
    if stage is not None:
        stage.status = STAGE_STATUS_HELD
        stage.started_at = now
        stage.finished_at = now
        stage.duration_ms = 0
        stage.detail = {"missing_capability": reason, "decided_at": "intake", **detail}
        stage.error_message = HOLD_REASON_TEXT.get(reason, reason)
    await db.flush()


def _held_body(name: str, reason: str, detail: str | None = None) -> str:
    # 파일 내용이 아니라 접수 사실만 적은 스텁이다. 패시지·색인을 만들지 않으므로 검색 결과에 나오지 않는다.
    lines = [f"[보류] {name}", f"원본은 보관했지만 지식화하지 않았습니다: {HOLD_REASON_TEXT.get(reason, reason)}."]
    if detail:
        lines.append(detail)
    return "\n".join(lines)


async def _register_parsed(
    db: AsyncSession,
    *,
    item: IngestInput,
    body: str,
    parser_version: str,
    parser_name: str,
    media_type: str,
    title: str,
    kind: str,
    locator: str | None,
    source_meta: dict[str, Any],
    origin_meta: dict[str, Any],
    locator_spans: list | None = None,
    collected_at=None,
    binary: bool = True,
) -> ItemOutcome:
    digest = hashlib.sha256(item.data).hexdigest()
    key = f"upload:{digest[:16]}"
    if item.kind == "url" and item.url:
        key = "url:" + hashlib.sha256(item.url.encode()).hexdigest()
    result = await ingest_source(
        db, source_key=key, data=item.data, kind=kind, locator=locator, media_type=media_type[:120], title=title,
        lang=guess_lang(body), parser_version=parser_version[:60], normalized_body=body, collected_at=collected_at,
        origin_meta=origin_meta, allow_binary=binary, original_filename=item.name if item.kind != "text" else None,
        parser_name=parser_name, source_meta=source_meta,
    )
    await _ensure_access(db, result.document_id)
    job = await _job_for(db, result.revision_id)
    outcome = ItemOutcome(
        name=item.name, status=STATUS_REGISTERED, document_id=result.document_id, revision_id=result.revision_id,
        created=result.created,
    )
    if job is not None and not result.created:
        outcome.status, outcome.job_id = STATUS_DUPLICATE, job.id
        return outcome
    spans = locator_spans
    if spans is not None:
        from ttakkari.kb_models import KbRevision

        rev = await db.get(KbRevision, result.revision_id)
        if rev is None or rev.body != body:
            spans = None  # 저장 정규화가 본문을 바꿨다면 파서 오프셋은 더 이상 맞지 않는다
    await segment_revision(db, result.revision_id, locator_spans=spans)
    job = await enqueue_pipeline_job(db, document_id=result.document_id, revision_id=result.revision_id)
    outcome.job_id = job.id
    if not result.created:
        outcome.status = STATUS_DUPLICATE
    return outcome


async def _register_held(
    db: AsyncSession, *, item: IngestInput, reason: str, detected: Detected, title: str,
    source_meta: dict[str, Any], origin_meta: dict[str, Any], detail: str | None = None,
) -> ItemOutcome:
    body = _held_body(item.name, reason, detail)
    digest = hashlib.sha256(item.data).hexdigest()
    key = "url:" + hashlib.sha256((item.url or "").encode()).hexdigest() if item.kind == "url" and item.url else f"upload:{digest[:16]}"
    result = await ingest_source(
        db, source_key=key, data=item.data, kind="file" if item.kind != "url" else "web", locator=item.url,
        media_type=detected.media_type[:120], title=title, lang=None, parser_version=HELD_PARSER_VERSION,
        normalized_body=body, origin_meta={**origin_meta, "held": reason}, allow_binary=True,
        original_filename=item.name, parser_name="held", source_meta={**source_meta, "hold_reason": reason},
    )
    await _ensure_access(db, result.document_id)
    job = await enqueue_pipeline_job(db, document_id=result.document_id, revision_id=result.revision_id)
    if job.status != JOB_STATUS_HELD:
        await _mark_job_held(db, job, reason, {"detected_type": detected.label})
    return ItemOutcome(
        name=item.name, status=STATUS_HELD, document_id=result.document_id, revision_id=result.revision_id,
        job_id=job.id, detected_type=detected.label, hold_reason=reason, created=result.created,
    )


async def ingest_item(
    db: AsyncSession, item: IngestInput, *, title: str | None, domain: str | None, source_kind: str | None,
    batch_id: str,
) -> ItemOutcome:
    """항목 하나를 등록한다. 입력 오류는 failed ItemOutcome 으로 돌려주고 예외는 DB 오류만 올린다."""
    meta: dict[str, Any] = {}
    if domain:
        meta["domain"] = domain
    origin = {"via": "kb_ingest", "batch_id": batch_id, "input": item.kind}
    if source_kind and not _SOURCE_KIND_RE.match(source_kind):
        return ItemOutcome(item.name, STATUS_FAILED, error="invalid_source_kind")
    if not item.data and item.kind != "text":
        return ItemOutcome(item.name, STATUS_FAILED, error="empty_content")
    if len(item.data) > MAX_FILE_BYTES:
        return ItemOutcome(item.name, STATUS_FAILED, error="file_too_large", detail=f"최대 {MAX_FILE_BYTES // (1024 * 1024)}MiB")

    if item.kind == "text":
        text = item.data.decode("utf-8", errors="replace")
        if not text.strip():
            return ItemOutcome(item.name, STATUS_FAILED, error="empty_content")
        if "\x00" in text:
            return ItemOutcome(item.name, STATUS_FAILED, error="unsupported_nul")
        if len(text) > MAX_TEXT_CHARS:
            return ItemOutcome(item.name, STATUS_FAILED, error="text_too_long")
        # 텍스트는 기존 등록 경로(register_text)와 같은 키·정규화를 쓴다.
        from ttakkari import kb_library as lib

        result = await lib.register_text(
            db, title=title or default_title(item, text), text=text, source_key=None, lang=guess_lang(text),
            locator=None, scope="personal", source_kind=source_kind, source_meta=meta or None, origin_extra=origin,
        )
        job = await _job_for(db, result.revision_id)
        return ItemOutcome(
            name=item.name, status=STATUS_REGISTERED if result.created else STATUS_DUPLICATE,
            document_id=result.document_id, revision_id=result.revision_id, job_id=job.id if job else None,
            detected_type="text", created=result.created,
        )

    detected = detect_type(item.name, item.content_type, item.data)
    doc_title = title or default_title(item)
    kind = source_kind or ("web" if item.kind == "url" else "file")
    if detected.route == "unsupported":
        return await _register_held(
            db, item=item, reason="unsupported_format", detected=detected, title=doc_title, source_meta=meta, origin_meta=origin,
        )

    if detected.route == "html":
        try:
            extracted = collector_parse.extract_document(item.data, url=item.url or f"https://upload.invalid/{item.name}", kind="html")
        except ValueError as exc:
            return ItemOutcome(item.name, STATUS_FAILED, error="parse_empty_extraction", detail=str(exc)[:200], detected_type="html")
        normalized = collector_textproc.normalize_text(extracted.text)
        outcome = await _register_parsed(
            db, item=item, body=normalized.text, parser_version=collector_parse.PARSER_VERSION, parser_name="collector_html",
            media_type="text/html", title=title or (extracted.title or doc_title)[:500], kind=kind, locator=item.url,
            source_meta={**meta, "heading_structure": extracted.meta.get("heading_structure")},
            origin_meta={**origin, "heading_structure": extracted.meta.get("heading_structure")}, binary=False,
        )
        outcome.detected_type = "html"
        return outcome

    # 지원 파서(PDF/Office/CSV/코드/자막/마크다운·텍스트). 레지스트리가 크기 제한과 파서 예외를 FAILED 로 바꿔 준다.
    parsed = create_default_registry().parse(item.data, filename=detected.filename, content_type=detected.content_type)
    if parsed.status == ParseStatus.HELD.value:
        return await _register_held(
            db, item=item, reason=parsed.hold_reason or "unsupported_format", detected=detected, title=doc_title,
            source_meta=meta, origin_meta=origin, detail=parsed.error_detail,
        )
    if parsed.status != ParseStatus.SUCCEEDED.value:
        if parsed.error_code == "unsupported_format":
            return await _register_held(
                db, item=item, reason="unsupported_format", detected=detected, title=doc_title, source_meta=meta, origin_meta=origin,
            )
        return ItemOutcome(
            item.name, STATUS_FAILED, error=f"parse_{parsed.error_code or 'failed'}", detail=(parsed.error_detail or "")[:300],
            detected_type=detected.label,
        )
    if not parsed.normalized_body.strip():
        return ItemOutcome(item.name, STATUS_FAILED, error="parse_empty_text", detected_type=detected.label)
    parser_label = (parsed.metadata or {}).get("parser") or getattr(detected.parser, "name", "markdown_v1")
    parser_ver = (parsed.metadata or {}).get("version") or getattr(detected.parser, "version", "1.0")
    outcome = await _register_parsed(
        db, item=item, body=parsed.normalized_body, parser_version=f"{parser_label}-{parser_ver}", parser_name=str(parser_label),
        media_type=detected.media_type, title=doc_title, kind=kind, locator=item.url,
        source_meta=meta, origin_meta=origin, locator_spans=parsed.locator_spans, binary=detected.route == "parser",
    )
    outcome.detected_type = detected.label
    return outcome


async def fetch_url(url: str) -> IngestInput:
    """사용자가 지정한 단일 URL 을 SSRF 방어 fetcher 로 한 번 가져온다. 실패는 KbInputError(코드)로 올린다."""
    from ttakkari.collector.fetcher import FetchError, SafeFetcher
    from ttakkari.collector.netguard import UrlRejected, normalize_url

    try:
        parsed = normalize_url(url, allowed_schemes=("https", "http"), allowed_ports=None, strip_tracking=False)
    except UrlRejected as exc:
        raise KbInputError(f"url_rejected:{exc.code}") from exc
    fetcher = SafeFetcher(user_agent=URL_FETCH_USER_AGENT)
    try:
        result = await fetcher.fetch(parsed, allowed_schemes=("https", "http"), allowed_ports=None)
    except UrlRejected as exc:
        raise KbInputError(f"url_rejected:{exc.code}") from exc
    except FetchError as exc:
        raise KbInputError(f"url_fetch_failed:{exc.code}") from exc
    finally:
        await fetcher.aclose()
    path_name = unquote(urlsplit(result.final_url).path.rsplit("/", 1)[-1]) or urlsplit(result.final_url).netloc
    return IngestInput(kind="url", name=_clean_filename(path_name), data=result.body, content_type=result.media_type, url=result.final_url)


async def ingest_batch(
    db: AsyncSession, items: list[IngestInput], *, title: str | None, domain: str | None, source_kind: str | None,
) -> dict[str, Any]:
    """항목별 savepoint 로 등록한다. 호출자가 마지막에 commit 한다."""
    if not items:
        raise KbInputError("ingest_empty")
    if len(items) > MAX_ITEMS_PER_BATCH:
        raise KbInputError("ingest_too_many_items")
    batch_id = str(uuid.uuid4())
    # 제목은 항목이 하나일 때만 그대로 쓰고, 여러 개면 항목별 이름에서 만든다(같은 제목이 모든 문서에 붙는 것을 막는다).
    shared_title = title if len(items) == 1 else None
    out: list[ItemOutcome] = []
    for item in items:
        if item.error:
            out.append(ItemOutcome(item.name, STATUS_FAILED, error=item.error[:120], detected_type="url"))
            continue
        outcome: ItemOutcome | None = None
        for attempt in (1, 2):
            try:
                async with db.begin_nested():
                    outcome = await ingest_item(db, item, title=shared_title, domain=domain, source_kind=source_kind, batch_id=batch_id)
                break
            except KbInputError as exc:
                outcome = ItemOutcome(item.name, STATUS_FAILED, error=str(exc)[:120])
                break
            except IntegrityError:
                # 같은 바이트를 동시에 올리면 한쪽이 unique 충돌이다. 한 번 다시 시도하면 기존 행을 받는다(멱등).
                outcome = ItemOutcome(item.name, STATUS_FAILED, error="conflict_retry_later")
            except Exception as exc:  # 한 항목의 예기치 못한 실패가 배치 전체를 막지 않는다
                log.exception("kb ingest item failed: %s", item.name)
                outcome = ItemOutcome(item.name, STATUS_FAILED, error=f"internal_{type(exc).__name__}")
                break
        out.append(outcome)
    return {"batch_id": batch_id, "items": [o.as_dict() for o in out]}
