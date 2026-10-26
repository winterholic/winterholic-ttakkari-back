"""Base document parser interface, result containers, and parser registry."""

from __future__ import annotations

import abc
import dataclasses
import hashlib
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.common.chunking import LocatorSpan
from ttakkari.kb_parsers.resource_limits import (
    DEFAULT_LIMITS,
    ParserResourceLimits,
)


class ParseStatus(str, Enum):
    SUCCEEDED = "succeeded"
    HELD = "held"
    FAILED = "failed"


@dataclasses.dataclass
class ParsedDocument:
    """Standard parsed document contract adhering to KB v3 ledger specifications."""

    normalized_body: str
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    locator_spans: list[LocatorSpan] = dataclasses.field(default_factory=list)
    status: str = ParseStatus.SUCCEEDED.value
    hold_reason: str | None = None
    error_code: str | None = None
    error_detail: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.normalized_body.encode("utf-8")).hexdigest()

    @property
    def char_count(self) -> int:
        return len(self.normalized_body)


def validate_parsed_document(
    doc: ParsedDocument,
    limits: ParserResourceLimits = DEFAULT_LIMITS,
) -> ParsedDocument:
    """Validate locator span integrity and slice consistency against normalized_body."""
    if doc.status != ParseStatus.SUCCEEDED.value:
        return doc

    body = doc.normalized_body
    body_len = len(body)

    # 1. Text character limit check
    if body_len > limits.max_text_chars:
        return dataclasses.replace(
            doc,
            status=ParseStatus.FAILED.value,
            error_code="limits_exceeded",
            error_detail=f"Parsed body length {body_len} exceeds maximum character limit {limits.max_text_chars}",
        )

    # 2. Locator span bounds and slice verification
    for idx, span in enumerate(doc.locator_spans):
        if not (0 <= span.start_offset <= span.end_offset <= body_len):
            return dataclasses.replace(
                doc,
                status=ParseStatus.FAILED.value,
                error_code="locator_out_of_bounds",
                error_detail=(
                    f"Locator span index {idx} ({span.start_offset}:{span.end_offset}) "
                    f"out of bounds for normalized body length {body_len}"
                ),
            )

        expected_text = span.extra.get("exact_text")
        if expected_text is not None:
            actual_text = body[span.start_offset : span.end_offset]
            if actual_text != expected_text:
                return dataclasses.replace(
                    doc,
                    status=ParseStatus.FAILED.value,
                    error_code="locator_slice_mismatch",
                    error_detail=(
                        f"Locator span index {idx} text mismatch: "
                        f"expected {expected_text[:30]!r}, got {actual_text[:30]!r}"
                    ),
                )

    return doc


class BaseParser(abc.ABC):
    """Abstract parser for specific file formats."""

    name: str = "base"
    version: str = "1.0"
    supported_extensions: ClassVar[set[str]] = set()
    supported_mime_types: ClassVar[set[str]] = set()

    def can_parse(
        self,
        content_type: str | None = None,
        filename: str | None = None,
        sample_bytes: bytes | None = None,
    ) -> bool:
        if filename:
            ext = Path(filename).suffix.lower()
            if ext in self.supported_extensions:
                return True
        if content_type:
            ct = content_type.lower().split(";")[0].strip()
            if ct in self.supported_mime_types:
                return True
        return False

    @abc.abstractmethod
    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        **kwargs: Any,
    ) -> ParsedDocument:
        """Parse raw bytes into a ParsedDocument."""
        raise NotImplementedError


class ParserRegistry:
    """Registry managing format detection and parser dispatch with safety checks."""

    def __init__(
        self,
        max_bytes: int = 100 * 1024 * 1024,
        limits: ParserResourceLimits | None = None,
    ):
        self._parsers: list[BaseParser] = []
        self.max_bytes = max_bytes
        self.limits = limits or DEFAULT_LIMITS

    def register(self, parser: BaseParser) -> None:
        self._parsers.append(parser)

    def find_parser(
        self,
        content_type: str | None = None,
        filename: str | None = None,
        sample_bytes: bytes | None = None,
    ) -> BaseParser | None:
        for p in self._parsers:
            if p.can_parse(
                content_type=content_type, filename=filename, sample_bytes=sample_bytes
            ):
                return p
        return None

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        **kwargs: Any,
    ) -> ParsedDocument:
        """Parse data safely. Returns ParsedDocument with status 'failed' on unhandled error or unknown format."""
        if not data:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="empty_content",
                error_detail="Cannot parse empty bytes",
            )

        if len(data) > self.max_bytes or len(data) > self.limits.max_file_bytes:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="file_size_exceeded",
                error_detail=f"File size {len(data)} exceeds maximum allowed {self.max_bytes}",
            )

        parser = self.find_parser(
            content_type=content_type, filename=filename, sample_bytes=data[:1024]
        )
        if not parser:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="unsupported_format",
                error_detail=f"No parser available for filename='{filename}', content_type='{content_type}'",
            )

        try:
            parsed = parser.parse(
                data,
                filename=filename,
                content_type=content_type,
                limits=self.limits,
                **kwargs,
            )
            return validate_parsed_document(parsed, limits=self.limits)
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code=f"parser_error_{type(exc).__name__}",
                error_detail=str(exc)[:500],
            )
