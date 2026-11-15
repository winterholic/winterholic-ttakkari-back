"""Bounded PDF-to-text subprocess. Reads stdin bytes, returns JSON, never follows links."""
from __future__ import annotations

import io
import json
import logging
import resource
import sys
import time

# 워커는 CPU 15초 제한 안에서 돈다. 레이아웃 복원은 여유가 있을 때만 시도하고 예산을 넘으면 기본 추출로 돌아간다.
LAYOUT_MAX_PAGES = 150
LAYOUT_START_CPU = 4.0
LAYOUT_ABORT_CPU = 9.0
MAX_PAGES = 300
MAX_CHARS = 1_000_000


def _layout_pages(reader, plain: dict[int, str], total: int) -> dict[int, str] | None:
    """쪽별 본문을 다단 읽기 순서·마크다운 표·# 제목으로 복원한다. 구조가 하나도 없거나 예산을 넘으면 None."""
    if total > LAYOUT_MAX_PAGES or time.process_time() > LAYOUT_START_CPU:
        return None
    try:
        from ttakkari.kb_parsers import pdf_layout

        table_pages = pdf_layout.structure_table_pages(reader)
        out: dict[int, str] = {}
        structured = False
        for number, page in enumerate(reader.pages):
            if number not in plain:
                continue
            if time.process_time() > LAYOUT_ABORT_CPU:
                return None
            layout = pdf_layout.analyze_page(
                pdf_layout.collect_fragments(page), float(page.mediabox.width),
                table_allowed=table_pages is None or number in table_pages,
            )
            if not layout.structured:
                out[number] = plain[number]
                continue
            structured = True
            out[number] = "\n\n".join(
                (f"{'#' * block.level} {block.text}" if block.kind == "heading" else block.text)
                for block in layout.blocks if block.text.strip()
            ) or plain[number]
        return out if structured else None
    except Exception:  # noqa: BLE001 구조 복원 실패는 기본 추출 결과로 대신한다
        return None


def main():
    _, hard_cpu = resource.getrlimit(resource.RLIMIT_CPU)
    resource.setrlimit(resource.RLIMIT_CPU, (15, hard_cpu))
    # Darwin rejects RLIMIT_AS even below an apparent unlimited hard limit.
    # Keep CPU/wall/input/page/decompression limits on macOS; Linux also caps VM.
    if sys.platform != "darwin":
        _, hard_memory = resource.getrlimit(resource.RLIMIT_AS)
        resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2, hard_memory))
    logging.getLogger("pypdf").setLevel(logging.CRITICAL)
    from pypdf import PdfReader, __version__, filters

    filters.ZLIB_MAX_OUTPUT_LENGTH = 10 * 1024 * 1024

    raw = sys.stdin.buffer.read(20 * 1024 * 1024 + 1)
    if len(raw) > 20 * 1024 * 1024:
        return {"error": "pdf_byte_limit"}
    reader = PdfReader(io.BytesIO(raw), strict=False, root_object_recovery_limit=2000)
    if reader.is_encrypted:
        return {"error": "pdf_encrypted"}
    total = len(reader.pages)
    plain: dict[int, str] = {}
    chars = 0
    for number, page in enumerate(reader.pages):
        if number >= MAX_PAGES or chars >= MAX_CHARS:
            break
        content = page.get_contents()
        if content and len(content.get_data()) > 10 * 1024 * 1024:
            return {"error": "pdf_content_stream_limit"}
        text = (page.extract_text() or "").strip()
        if text:
            plain[number] = text
            chars += len(text) + 2
    laid_out = _layout_pages(reader, plain, total) if plain else None
    chosen = laid_out or plain
    sections, boundaries, offset = [], [], 0
    for number in sorted(chosen):
        if offset >= MAX_CHARS:
            break
        text = chosen[number][:max(0, MAX_CHARS - offset)]
        sections.append(text)
        boundaries.append({"page": number + 1, "start": offset, "end": offset + len(text)})
        offset += len(text) + 2
    if not sections:
        return {"error": "pdf_no_text_ocr_required"}
    metadata = reader.metadata or {}
    return {"text": "\n\n".join(sections), "pages": boundaries, "total_pages": total,
            "parser_version": f"pypdf-{__version__}+pdf-{2 if laid_out else 1}",
            "title": str(metadata.get("/Title", ""))[:500] or None,
            "author": str(metadata.get("/Author", ""))[:300] or None,
            "truncated": total > MAX_PAGES or offset >= MAX_CHARS}


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:  # noqa: BLE001 hostile PDFs must yield an inert failure record
        result = {"error": "pdf_invalid", "type": type(exc).__name__}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
