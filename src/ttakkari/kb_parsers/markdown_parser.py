"""Markdown, plain text, and HTML document parser."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, ClassVar

from ttakkari.common.chunking import parse_markdown_spans
from ttakkari.kb_parsers.base import BaseParser, ParsedDocument, ParseStatus
from ttakkari.kb_parsers.resource_limits import (
    DEFAULT_LIMITS,
    ParserResourceLimits,
)


def normalize_text_body(raw_bytes: bytes) -> str:
    """Detect encoding, normalize newlines, strip NULs, and apply Unicode NFC normalization."""
    # Try UTF-8 first
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            import charset_normalizer

            res = charset_normalizer.from_bytes(raw_bytes).best()
            text = str(res) if res else raw_bytes.decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            text = raw_bytes.decode("utf-8", errors="replace")

    # Normalize CR-LF / CR to LF
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip PostgreSQL invalid NUL bytes
    text = text.replace("\x00", "")
    # Standard Unicode NFC normalization
    return unicodedata.normalize("NFC", text)


class MarkdownParser(BaseParser):
    name = "markdown_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {
        ".md",
        ".markdown",
        ".txt",
        ".rst",
        ".log",
        ".html",
        ".htm",
    }
    supported_mime_types: ClassVar[set[str]] = {
        "text/markdown",
        "text/plain",
        "text/html",
        "text/x-rst",
    }

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        limits: ParserResourceLimits = DEFAULT_LIMITS,
        **kwargs: Any,
    ) -> ParsedDocument:
        normalized = normalize_text_body(data)

        if len(normalized) > limits.max_text_chars:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="limits_exceeded",
                error_detail=f"Text body length {len(normalized)} exceeds limit {limits.max_text_chars}",
            )

        # Basic HTML tag stripping if it's html
        is_html = (filename and filename.lower().endswith((".html", ".htm"))) or (
            content_type and "html" in content_type.lower()
        )
        if is_html:
            try:
                import trafilatura

                extracted = trafilatura.extract(
                    normalized, output_format="markdown", include_links=True
                )
                if extracted and extracted.strip():
                    normalized = extracted.strip()
            except Exception:  # noqa: BLE001
                # Fallback: strip tags with regex while keeping line structure
                normalized = re.sub(r"<[^>]+>", "", normalized)

        spans = parse_markdown_spans(normalized)

        return ParsedDocument(
            normalized_body=normalized,
            metadata={
                "parser": self.name,
                "version": self.version,
                "filename": filename,
                "content_type": content_type,
            },
            locator_spans=spans,
            status=ParseStatus.SUCCEEDED.value,
        )
