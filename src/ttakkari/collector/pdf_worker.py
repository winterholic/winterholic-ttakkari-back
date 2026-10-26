"""Bounded PDF-to-text subprocess. Reads stdin bytes, returns JSON, never follows links."""
from __future__ import annotations

import io
import json
import logging
import resource
import sys


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
    sections, boundaries, offset = [], [], 0
    for number, page in enumerate(reader.pages):
        if number >= 300 or offset >= 1_000_000:
            break
        content = page.get_contents()
        if content and len(content.get_data()) > 10 * 1024 * 1024:
            return {"error": "pdf_content_stream_limit"}
        text = (page.extract_text() or "").strip()
        if text:
            text = text[:max(0, 1_000_000 - offset)]
            sections.append(text)
            boundaries.append({"page": number + 1, "start": offset, "end": offset + len(text)})
            offset += len(text) + 2
    if not sections:
        return {"error": "pdf_no_text_ocr_required"}
    metadata = reader.metadata or {}
    return {"text": "\n\n".join(sections), "pages": boundaries, "total_pages": total,
            "parser_version": f"pypdf-{__version__}+pdf-1",
            "title": str(metadata.get("/Title", ""))[:500] or None,
            "author": str(metadata.get("/Author", ""))[:300] or None,
            "truncated": total > 300 or offset >= 1_000_000}


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:  # noqa: BLE001 hostile PDFs must yield an inert failure record
        result = {"error": "pdf_invalid", "type": type(exc).__name__}
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
