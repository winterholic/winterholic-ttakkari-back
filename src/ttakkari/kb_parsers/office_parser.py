"""Office document parsers (DOCX, PPTX, XLSX, CSV) and legacy office rejection."""

from __future__ import annotations

import csv
import io
import unicodedata
from pathlib import Path
from typing import Any, ClassVar

from ttakkari.common.chunking import LocatorSpan
from ttakkari.kb_parsers.base import BaseParser, ParsedDocument, ParseStatus
from ttakkari.kb_parsers.resource_limits import (
    DEFAULT_LIMITS,
    ParserResourceLimits,
    validate_zip_safety,
)


class DocxParser(BaseParser):
    name = "office_docx_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {".docx"}
    supported_mime_types: ClassVar[set[str]] = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/docx",
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
        import docx

        try:
            validate_zip_safety(data, limits=limits)
        except ValueError as exc:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="limits_exceeded",
                error_detail=str(exc),
            )

        try:
            doc = docx.Document(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code=f"parser_error_{type(exc).__name__}",
                error_detail=str(exc),
            )

        body_parts: list[str] = []
        locator_spans: list[LocatorSpan] = []
        cur_pos = 0

        section_trail: list[tuple[int, str]] = []
        cur_section_path: list[str] = []

        for p in doc.paragraphs:
            text = unicodedata.normalize("NFC", p.text.strip())
            if not text:
                continue

            # Heading level detection
            style_name = p.style.name.lower() if p.style and p.style.name else ""
            if "heading 1" in style_name:
                level = 1
            elif "heading 2" in style_name:
                level = 2
            elif "heading 3" in style_name:
                level = 3
            elif "heading" in style_name:
                level = 4
            else:
                level = None

            if level is not None:
                while section_trail and section_trail[-1][0] >= level:
                    section_trail.pop()
                section_trail.append((level, text))
                cur_section_path = [t for _, t in section_trail]
                formatted = f"{'#' * level} {text}"
            else:
                formatted = text

            if body_parts:
                body_parts.append("\n\n")
                cur_pos += 2

            p_start = cur_pos
            body_parts.append(formatted)
            cur_pos += len(formatted)
            p_end = cur_pos

            locator_spans.append(
                LocatorSpan(
                    start_offset=p_start,
                    end_offset=p_end,
                    section_path=list(cur_section_path),
                    chunk_kind="heading" if level is not None else "prose",
                    extra={"exact_text": formatted},
                )
            )

        # Process tables in document
        for t in doc.tables:
            table_rows: list[list[str]] = []
            for row in t.rows:
                table_rows.append(
                    [
                        unicodedata.normalize(
                            "NFC", cell.text.strip().replace("\n", " ")
                        )
                        for cell in row.cells
                    ]
                )

            if not table_rows:
                continue

            md_lines: list[str] = []
            headers = table_rows[0]
            md_lines.append("| " + " | ".join(headers) + " |")
            md_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
            for r in table_rows[1:]:
                # Pad row to match header length
                padded = r + [""] * (len(headers) - len(r))
                md_lines.append("| " + " | ".join(padded[: len(headers)]) + " |")

            table_text = "\n".join(md_lines)
            if body_parts:
                body_parts.append("\n\n")
                cur_pos += 2

            t_start = cur_pos
            body_parts.append(table_text)
            cur_pos += len(table_text)
            t_end = cur_pos

            locator_spans.append(
                LocatorSpan(
                    start_offset=t_start,
                    end_offset=t_end,
                    section_path=list(cur_section_path),
                    chunk_kind="table",
                    extra={"exact_text": table_text},
                )
            )

        full_body = "".join(body_parts)
        return ParsedDocument(
            normalized_body=full_body,
            metadata={"parser": self.name, "filename": filename},
            locator_spans=locator_spans,
            status=ParseStatus.SUCCEEDED.value,
        )


class PptxParser(BaseParser):
    name = "office_pptx_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {".pptx"}
    supported_mime_types: ClassVar[set[str]] = {
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/pptx",
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
        import pptx

        try:
            validate_zip_safety(data, limits=limits)
        except ValueError as exc:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="limits_exceeded",
                error_detail=str(exc),
            )

        try:
            prs = pptx.Presentation(io.BytesIO(data))
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code=f"parser_error_{type(exc).__name__}",
                error_detail=str(exc),
            )

        body_parts: list[str] = []
        locator_spans: list[LocatorSpan] = []
        cur_pos = 0

        for idx, slide in enumerate(prs.slides):
            slide_no = idx + 1
            slide_title = ""
            slide_lines: list[str] = []

            for shape in slide.shapes:
                if shape.has_text_frame:
                    tf = shape.text_frame
                    text = unicodedata.normalize("NFC", tf.text.strip())
                    if text:
                        if not slide_title and shape == slide.shapes[0]:
                            slide_title = text.split("\n")[0]
                        slide_lines.append(text)

            # Slide notes
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes_text = unicodedata.normalize(
                    "NFC", slide.notes_slide.notes_text_frame.text.strip()
                )
                if notes_text:
                    slide_lines.append(f"[발표자 노트]\n{notes_text}")

            if not slide_lines:
                continue

            slide_body = "\n\n".join(slide_lines)
            if body_parts:
                body_parts.append("\n\n---\n\n")
                cur_pos += len("\n\n---\n\n")

            s_start = cur_pos
            body_parts.append(slide_body)
            cur_pos += len(slide_body)
            s_end = cur_pos

            section_path = [slide_title] if slide_title else [f"Slide {slide_no}"]
            locator_spans.append(
                LocatorSpan(
                    start_offset=s_start,
                    end_offset=s_end,
                    section_path=section_path,
                    slide_no=slide_no,
                    chunk_kind="prose",
                    extra={"exact_text": slide_body},
                )
            )

        full_body = "".join(body_parts)
        return ParsedDocument(
            normalized_body=full_body,
            metadata={
                "parser": self.name,
                "slide_count": len(prs.slides),
                "filename": filename,
            },
            locator_spans=locator_spans,
            status=ParseStatus.SUCCEEDED.value,
        )


class XlsxParser(BaseParser):
    name = "office_xlsx_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {".xlsx"}
    supported_mime_types: ClassVar[set[str]] = {
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/xlsx",
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
        import openpyxl

        try:
            validate_zip_safety(data, limits=limits)
        except ValueError as exc:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code="limits_exceeded",
                error_detail=str(exc),
            )

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        except Exception as exc:  # noqa: BLE001
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.FAILED.value,
                error_code=f"parser_error_{type(exc).__name__}",
                error_detail=str(exc),
            )

        body_parts: list[str] = []
        locator_spans: list[LocatorSpan] = []
        cur_pos = 0
        total_cells = 0

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            rows: list[list[str]] = []

            for sheet_rows_count, r in enumerate(
                ws.iter_rows(values_only=True), start=1
            ):
                if sheet_rows_count > limits.max_xlsx_rows_per_sheet:
                    return ParsedDocument(
                        normalized_body="",
                        status=ParseStatus.FAILED.value,
                        error_code="limits_exceeded",
                        error_detail=f"Sheet '{sheet_name}' row count exceeds maximum limit {limits.max_xlsx_rows_per_sheet}",
                    )

                str_row = [
                    unicodedata.normalize("NFC", str(c).strip())
                    if c is not None
                    else ""
                    for c in r
                ]
                total_cells += len(str_row)
                if total_cells > limits.max_xlsx_total_cells:
                    return ParsedDocument(
                        normalized_body="",
                        status=ParseStatus.FAILED.value,
                        error_code="limits_exceeded",
                        error_detail=f"XLSX total cell count exceeds maximum limit {limits.max_xlsx_total_cells}",
                    )

                if any(str_row):
                    rows.append(str_row)

            if not rows:
                continue

            headers = rows[0]
            max_cols = len(headers)
            md_lines: list[str] = [f"### 시트: {sheet_name}", ""]
            md_lines.append("| " + " | ".join(headers) + " |")
            md_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
            for r in rows[1:]:
                padded = r + [""] * (max_cols - len(r))
                md_lines.append("| " + " | ".join(padded[:max_cols]) + " |")

            sheet_text = "\n".join(md_lines)
            if body_parts:
                body_parts.append("\n\n")
                cur_pos += 2

            s_start = cur_pos
            body_parts.append(sheet_text)
            cur_pos += len(sheet_text)
            s_end = cur_pos

            locator_spans.append(
                LocatorSpan(
                    start_offset=s_start,
                    end_offset=s_end,
                    section_path=[sheet_name],
                    chunk_kind="table",
                    extra={
                        "sheet_name": sheet_name,
                        "row_range": [1, len(rows)],
                        "exact_text": sheet_text,
                    },
                )
            )

        full_body = "".join(body_parts)
        return ParsedDocument(
            normalized_body=full_body,
            metadata={
                "parser": self.name,
                "sheet_count": len(wb.sheetnames),
                "filename": filename,
            },
            locator_spans=locator_spans,
            status=ParseStatus.SUCCEEDED.value,
        )


class CsvParser(BaseParser):
    name = "csv_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {".csv", ".tsv"}
    supported_mime_types: ClassVar[set[str]] = {"text/csv", "text/tab-separated-values"}

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        limits: ParserResourceLimits = DEFAULT_LIMITS,
        **kwargs: Any,
    ) -> ParsedDocument:
        # Decode CSV text
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp949", errors="replace")

        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
        text = unicodedata.normalize("NFC", text)

        delimiter = "\t" if (filename and filename.endswith(".tsv")) else ","
        reader = csv.reader(io.StringIO(text), delimiter=delimiter)
        rows: list[list[str]] = []
        total_cells = 0

        for r in reader:
            if not any(c.strip() for c in r):
                continue
            cleaned_row = [unicodedata.normalize("NFC", c.strip()) for c in r]
            total_cells += len(cleaned_row)
            if total_cells > limits.max_xlsx_total_cells:
                return ParsedDocument(
                    normalized_body="",
                    status=ParseStatus.FAILED.value,
                    error_code="limits_exceeded",
                    error_detail=f"CSV cell count exceeds limit {limits.max_xlsx_total_cells}",
                )
            rows.append(cleaned_row)
            if len(rows) > limits.max_xlsx_rows_per_sheet:
                return ParsedDocument(
                    normalized_body="",
                    status=ParseStatus.FAILED.value,
                    error_code="limits_exceeded",
                    error_detail=f"CSV row count exceeds limit {limits.max_xlsx_rows_per_sheet}",
                )

        if not rows:
            return ParsedDocument(
                normalized_body="",
                status=ParseStatus.SUCCEEDED.value,
                metadata={"filename": filename},
            )

        headers = rows[0]
        max_cols = len(headers)
        md_lines: list[str] = []
        md_lines.append("| " + " | ".join(headers) + " |")
        md_lines.append("| " + " | ".join(["---"] * max_cols) + " |")
        for r in rows[1:]:
            padded = r + [""] * (max_cols - len(r))
            md_lines.append("| " + " | ".join(padded[:max_cols]) + " |")

        table_text = "\n".join(md_lines)
        span = LocatorSpan(
            start_offset=0,
            end_offset=len(table_text),
            section_path=[Path(filename).stem if filename else "Data"],
            chunk_kind="table",
            extra={"row_range": [1, len(rows)], "exact_text": table_text},
        )

        return ParsedDocument(
            normalized_body=table_text,
            metadata={
                "parser": self.name,
                "row_count": len(rows),
                "filename": filename,
            },
            locator_spans=[span],
            status=ParseStatus.SUCCEEDED.value,
        )


class LegacyOfficeParser(BaseParser):
    """Honest held rejection for legacy binary office documents (.doc, .ppt, .xls, .hwp)."""

    name = "legacy_office_v1"
    version = "1.0"
    supported_extensions: ClassVar[set[str]] = {".doc", ".ppt", ".xls", ".hwp"}
    supported_mime_types: ClassVar[set[str]] = {
        "application/msword",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-excel",
        "application/x-hwp",
    }

    def parse(
        self,
        data: bytes,
        *,
        filename: str | None = None,
        content_type: str | None = None,
        **kwargs: Any,
    ) -> ParsedDocument:
        return ParsedDocument(
            normalized_body="",
            status=ParseStatus.HELD.value,
            hold_reason="unsupported_legacy_office",
            metadata={"filename": filename, "content_type": content_type},
        )
