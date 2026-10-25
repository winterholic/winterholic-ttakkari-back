"""Base document parser interface, result containers, and parser registry."""

from __future__ import annotations

import abc
import dataclasses
import hashlib
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.common.chunking import LocatorSpan


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

    def __init__(self, max_bytes: int = 100 * 1024 * 1024):
        self._parsers: list[BaseParser] = []
        self.max_bytes = max_bytes

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

        if len(data) > self.max_bytes:
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
            return parser.parse(
                data, filename=filename, content_type=content_type, **kwargs
            )
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code=f"parser_error_{type(exc).__name__}",
                error_detail=str(exc)[:500],
            )
