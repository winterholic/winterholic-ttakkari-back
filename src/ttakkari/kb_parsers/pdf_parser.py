"""PDF document parser using pypdf with page offset mapping and scanned PDF detection."""

from __future__ import annotations

import io
import unicodedata
from typing import Any, ClassVar

from ttakkari.common.chunking import LocatorSpan
from ttakkari.kb_parsers import ocr, pdf_layout
from ttakkari.kb_parsers.base import BaseParser, ParsedDocument, ParseStatus
from ttakkari.kb_parsers.resource_limits import (
    DEFAULT_LIMITS,
    ParserResourceLimits,
)

# 쪽당 파싱 비용이 두 배가 되므로 아주 긴 문서는 기본 추출만 쓴다.
LAYOUT_MAX_PAGES = 400


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
            scanned = self._parse_scanned(data, num_pages, filename, limits)
            if scanned is not None:
                return scanned
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

        laid_out = self._parse_layout(reader, num_pages, limits) if num_pages <= LAYOUT_MAX_PAGES else None
        if laid_out is not None:
            body, spans, stats = laid_out
            return ParsedDocument(
                normalized_body=body,
                metadata={
                    "parser": "pdf_layout_v1",
                    "version": "1.0",
                    "page_count": num_pages,
                    "char_count": len(body),
                    "filename": filename,
                    **stats,
                },
                locator_spans=spans,
                status=ParseStatus.SUCCEEDED.value,
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

    def _parse_layout(
        self, reader: Any, num_pages: int, limits: ParserResourceLimits,
    ) -> tuple[str, list[LocatorSpan], dict[str, Any]] | None:
        """쪽마다 좌표 기반 구조(다단·표·제목)를 복원한다. 구조가 없는 쪽은 기본 추출 결과를 그대로 쓴다.

        어느 쪽에서든 예외가 나거나 구조가 하나도 없으면 None 을 돌려 호출자가 기본 경로를 쓰게 한다.
        """
        table_pages = pdf_layout.structure_table_pages(reader)
        parts: list[str] = []
        spans: list[LocatorSpan] = []
        trail: list[tuple[int, str]] = []
        pos = 0
        structured_pages = column_pages = table_count = heading_count = 0

        def add(text: str, kind: str, page_no: int, level: int = 0) -> None:
            nonlocal pos
            if not text.strip():
                return
            if kind == "heading":
                while trail and trail[-1][0] >= level:
                    trail.pop()
                trail.append((level, text))
                text = f"{'#' * level} {text}"
            if parts:
                parts.append("\n\n")
                pos += 2
            parts.append(text)
            path = [t for _, t in trail] or [f"Page {page_no}"]
            spans.append(
                LocatorSpan(
                    start_offset=pos, end_offset=pos + len(text), section_path=path, page_no=page_no,
                    chunk_kind=kind, extra={"exact_text": text},
                )
            )
            pos += len(text)

        for idx, page in enumerate(reader.pages):
            page_no = idx + 1
            try:
                frags = pdf_layout.collect_fragments(page)
                layout = pdf_layout.analyze_page(
                    frags, float(page.mediabox.width), table_allowed=table_pages is None or idx in table_pages,
                )
            except Exception:  # noqa: BLE001
                return None
            if layout.structured:
                structured_pages += 1
                column_pages += layout.columns
                table_count += layout.tables
                heading_count += layout.headings
                for block in layout.blocks:
                    add(unicodedata.normalize("NFC", block.text.replace("\x00", "")).strip(), block.kind, page_no, block.level)
            else:
                try:
                    plain = (page.extract_text() or "").replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
                except Exception:  # noqa: BLE001
                    plain = ""
                add(unicodedata.normalize("NFC", plain).strip(), "prose", page_no)
            if pos > limits.max_text_chars:
                return None

        if not structured_pages or not parts:
            return None
        stats = {
            "layout_pages": structured_pages,
            "layout_column_pages": column_pages,
            "layout_tables": table_count,
            "layout_headings": heading_count,
            "tagged_structure": table_pages is not None,
        }
        return "".join(parts), spans, stats

    def _parse_scanned(
        self, data: bytes, num_pages: int, filename: str | None, limits: ParserResourceLimits,
    ) -> ParsedDocument | None:
        """글자 레이어가 없는 PDF 를 OCR 로 읽는다. 읽지 못하면 None 을 돌려 기존 보류 경로를 탄다."""
        try:
            pages = ocr.ocr_pdf(data)
        except (ocr.OcrUnavailable, ocr.OcrFailed):
            return None

        parts: list[str] = []
        spans: list[LocatorSpan] = []
        pos = 0
        for page in pages:
            text = unicodedata.normalize("NFC", page.text.replace("\x00", "")).strip()
            if not text:
                continue
            if parts:
                parts.append("\n\n")
                pos += 2
            parts.append(text)
            spans.append(
                LocatorSpan(
                    start_offset=pos, end_offset=pos + len(text), section_path=[f"Page {page.page_no}"],
                    page_no=page.page_no, chunk_kind="prose", extra={"exact_text": text},
                )
            )
            pos += len(text)
        body = "".join(parts)
        if not body or len(body) > limits.max_text_chars:
            return None
        read = [p for p in pages if p.text.strip()]
        return ParsedDocument(
            normalized_body=body,
            metadata={
                "parser": "pdf_ocr_v1",
                "version": "1.0",
                "page_count": num_pages,
                "ocr_pages": len(pages),
                "ocr_truncated": len(pages) < num_pages,
                "ocr_mean_confidence": round(sum(p.confidence for p in read) / len(read), 3),
                "char_count": len(body),
                "filename": filename,
            },
            locator_spans=spans,
            status=ParseStatus.SUCCEEDED.value,
        )
