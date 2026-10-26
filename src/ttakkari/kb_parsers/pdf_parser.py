"""PDF document parser using pypdf with page offset mapping and scanned PDF detection."""

from __future__ import annotations

import io
import unicodedata
from typing import Any, ClassVar

from ttakkari.common.chunking import LocatorSpan
from ttakkari.kb_parsers.base import BaseParser, ParsedDocument, ParseStatus
from ttakkari.kb_parsers.resource_limits import (
    DEFAULT_LIMITS,
    ParserResourceLimits,
)


class PdfParser(BaseParser):
    name = "pdf_pypdf_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {".pdf"}
    supported_mime_types: ClassVar[set[str]] = {"application/pdf"}

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        limits: ParserResourceLimits = DEFAULT_LIMITS,
        **kwargs: Any,
    ) -> ParsedDocument:
        import pypdf

        try:
            reader = pypdf.PdfReader(io.BytesIO(data))
            num_pages = len(reader.pages)
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="corrupt_pdf",
                error_detail=f"Failed to read PDF structure: {exc}",
            )

        if num_pages > limits.max_pdf_pages:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="limits_exceeded",
                error_detail=f"PDF page count {num_pages} exceeds maximum limit {limits.max_pdf_pages}",
            )

        if num_pages == 0:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.HELD.value,
                hold_reason="ocr_required",
                metadata={"page_count": 0, "filename": filename},
            )

        page_texts: list[str] = []
        locator_spans: list[LocatorSpan] = []
        cur_pos = 0

        for idx, page in enumerate(reader.pages):
            page_no = idx + 1
            try:
                raw_text = page.extract_text() or ""
            except Exception:  # noqa: BLE001
                raw_text = ""

            # Normalize text
            norm_text = (
                raw_text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
            )
            norm_text = unicodedata.normalize("NFC", norm_text).strip()

            if norm_text:
                if page_texts:
                    # Separate pages with double newline
                    page_texts.append("\n\n")
                    cur_pos += 2

                p_start = cur_pos
                page_texts.append(norm_text)
                cur_pos += len(norm_text)
                p_end = cur_pos

                locator_spans.append(
                    LocatorSpan(
                        start_offset=p_start,
                        end_offset=p_end,
                        section_path=[f"Page {page_no}"],
                        page_no=page_no,
                        chunk_kind="prose",
                        extra={"exact_text": norm_text},
                    )
                )

        full_body = "".join(page_texts)
        total_chars = len(full_body)

        if total_chars > limits.max_text_chars:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="limits_exceeded",
                error_detail=f"Extracted PDF text length {total_chars} exceeds limit {limits.max_text_chars}",
            )

        # Scanned PDF check: average characters per page < 20 or total_chars == 0
        avg_chars_per_page = total_chars / max(1, num_pages)
        if total_chars == 0 or avg_chars_per_page < 20:
            return ParsedDocument(
                normalized_body=full_body,
                metadata={
                    "page_count": num_pages,
                    "char_count": total_chars,
                    "avg_chars_per_page": round(avg_chars_per_page, 1),
                    "filename": filename,
                },
                locator_spans=locator_spans,
                status=ParseStatus.HELD.value,
                hold_reason="ocr_required",
            )

        return ParsedDocument(
            normalized_body=full_body,
            metadata={
                "parser": self.name,
                "version": self.version,
                "page_count": num_pages,
                "char_count": total_chars,
                "filename": filename,
            },
            locator_spans=locator_spans,
            status=ParseStatus.SUCCEEDED.value,
        )
