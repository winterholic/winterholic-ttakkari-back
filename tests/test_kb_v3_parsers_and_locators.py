"""Tests for multi-format document parsers, locator spans, and content-addressed blob store."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from ttakkari.common.chunking import chunk_structured_document
from ttakkari.kb_parsers import (
    BlobStore,
    ParserRegistry,
    ParserResourceLimits,
    ParseStatus,
    create_default_registry,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "kb_v3"


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    """Override session DB fixture from conftest for pure unit testing without DB connection."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """Override per-test DB clean fixture from conftest."""
    yield


class TestBlobStore:
    def test_put_get_exists_verify(self, tmp_path: Path):
        store = BlobStore(root_dir=tmp_path / "blobs")
        data = b"Hello, Knowledge Library Blob Store!"
        digest, size, path = store.put(data)

        expected_digest = hashlib.sha256(data).hexdigest()
        assert digest == expected_digest
        assert size == len(data)
        assert store.exists(digest)
        assert store.get(digest) == data
        assert store.verify(digest)

        # Content-addressed fanout path check
        expected_rel = Path("sha256") / digest[:2] / digest[2:4] / f"{digest}.blob"
        assert path.relative_to(store.root_dir) == expected_rel

    def test_idempotent_put(self, tmp_path: Path):
        store = BlobStore(root_dir=tmp_path / "blobs")
        data = b"Duplicate content test"
        digest1, _size1, path1 = store.put(data)
        digest2, _size2, path2 = store.put(data)
        assert digest1 == digest2
        assert path1 == path2
        assert store.get(digest1) == data

    def test_max_bytes_rejection(self, tmp_path: Path):
        store = BlobStore(root_dir=tmp_path / "blobs", max_bytes=10)
        with pytest.raises(ValueError, match="Blob size 15 exceeds maximum 10"):
            store.put(b"123456789012345")

    def test_symlink_traversal_rejection_and_isolation(self, tmp_path: Path):
        root = tmp_path / "store"
        sibling = tmp_path / "store-evil"
        root.mkdir()
        sibling.mkdir()

        # Adversarial symlink inside store pointing outside
        (root / "sha256").symlink_to(sibling, target_is_directory=True)

        store = BlobStore(root)
        escaped = store.path_for("a" * 64)

        # Enforces target stays strictly within resolved root
        assert escaped.is_relative_to(root.resolve())

        # Puts remain idempotent and never escape into sibling
        a = store.put(b"repeat")
        b = store.put(b"repeat")
        assert a[:2] == b[:2]
        assert a[2] == b[2]
        assert store.verify(a[0])
        assert list(sibling.iterdir()) == []


class TestPdfParser:
    def test_sample_spec_pdf_text_and_locators(self):
        registry = create_default_registry()
        pdf_bytes = (FIXTURES_DIR / "sample_spec.pdf").read_bytes()

        parsed = registry.parse(
            pdf_bytes, filename="sample_spec.pdf", content_type="application/pdf"
        )
        assert parsed.status == ParseStatus.SUCCEEDED.value
        assert parsed.metadata["page_count"] == 3
        assert len(parsed.locator_spans) == 3

        # Verify page numbers and exact substring slice
        for idx, span in enumerate(parsed.locator_spans):
            assert span.page_no == idx + 1
            slice_text = parsed.normalized_body[span.start_offset : span.end_offset]
            assert f"Page {idx + 1}:" in slice_text

    def test_scanned_pdf_held_ocr_required(self):
        registry = create_default_registry()
        scan_bytes = (FIXTURES_DIR / "scanned_empty.pdf").read_bytes()

        parsed = registry.parse(scan_bytes, filename="scanned_empty.pdf")
        assert parsed.status == ParseStatus.HELD.value
        assert parsed.hold_reason == "ocr_required"

    def test_corrupt_pdf_failure_isolation(self):
        registry = create_default_registry()
        corrupt_bytes = (FIXTURES_DIR / "corrupt.pdf").read_bytes()

        parsed = registry.parse(corrupt_bytes, filename="corrupt.pdf")
        assert parsed.status == ParseStatus.FAILED.value
        assert parsed.error_code == "corrupt_pdf"


class TestOfficeParsers:
    def test_docx_parser_headings_and_tables(self):
        registry = create_default_registry()
        docx_bytes = (FIXTURES_DIR / "arch_notes.docx").read_bytes()

        parsed = registry.parse(docx_bytes, filename="arch_notes.docx")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        assert "Architecture Notes" in parsed.normalized_body
        assert "Service Registry" in parsed.normalized_body

        table_spans = [s for s in parsed.locator_spans if s.chunk_kind == "table"]
        assert len(table_spans) >= 1
        t_slice = parsed.normalized_body[
            table_spans[0].start_offset : table_spans[0].end_offset
        ]
        assert "Gateway" in t_slice
        assert "Embedding" in t_slice

    def test_pptx_parser_slides_and_notes(self):
        registry = create_default_registry()
        pptx_bytes = (FIXTURES_DIR / "review_deck.pptx").read_bytes()

        parsed = registry.parse(pptx_bytes, filename="review_deck.pptx")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        assert parsed.metadata["slide_count"] == 4
        assert len(parsed.locator_spans) == 4

        slide_nos = [s.slide_no for s in parsed.locator_spans]
        assert slide_nos == [1, 2, 3, 4]

        for s in parsed.locator_spans:
            slice_text = parsed.normalized_body[s.start_offset : s.end_offset]
            assert f"Slide {s.slide_no}" in slice_text

    def test_xlsx_parser_sheets_and_tables(self):
        registry = create_default_registry()
        xlsx_bytes = (FIXTURES_DIR / "config_matrix.xlsx").read_bytes()

        parsed = registry.parse(xlsx_bytes, filename="config_matrix.xlsx")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        assert parsed.metadata["sheet_count"] == 2
        assert len(parsed.locator_spans) == 2

        sheets_found = [s.section_path[0] for s in parsed.locator_spans]
        assert "Cluster" in sheets_found
        assert "Database" in sheets_found

        for s in parsed.locator_spans:
            assert s.chunk_kind == "table"
            slice_text = parsed.normalized_body[s.start_offset : s.end_offset]
            assert "|" in slice_text

    def test_csv_parser(self):
        registry = create_default_registry()
        csv_bytes = (FIXTURES_DIR / "sample.csv").read_bytes()

        parsed = registry.parse(csv_bytes, filename="sample.csv")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        assert len(parsed.locator_spans) == 1
        assert parsed.locator_spans[0].chunk_kind == "table"
        assert "Lead Architect" in parsed.normalized_body

    def test_legacy_office_held_rejection(self):
        registry = create_default_registry()
        doc_bytes = (FIXTURES_DIR / "legacy.doc").read_bytes()
        hwp_bytes = (FIXTURES_DIR / "legacy.hwp").read_bytes()

        res_doc = registry.parse(doc_bytes, filename="legacy.doc")
        assert res_doc.status == ParseStatus.HELD.value
        assert res_doc.hold_reason == "unsupported_legacy_office"

        res_hwp = registry.parse(hwp_bytes, filename="legacy.hwp")
        assert res_hwp.status == ParseStatus.HELD.value
        assert res_hwp.hold_reason == "unsupported_legacy_office"


class TestCodeParser:
    def test_python_ast_symbols(self):
        registry = create_default_registry()
        code_bytes = (FIXTURES_DIR / "sample_code.py").read_bytes()

        parsed = registry.parse(code_bytes, filename="sample_code.py")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        symbols = [s.extra.get("symbol_path") for s in parsed.locator_spans]
        assert "KnowledgeExtractor" in symbols
        assert "KnowledgeExtractor.extract_assertions" in symbols
        assert "main" in symbols

        # Exact offset slice check
        for s in parsed.locator_spans:
            slice_text = parsed.normalized_body[s.start_offset : s.end_offset]
            assert s.extra["symbol_path"].split(".")[-1] in slice_text

    def test_typescript_symbols(self):
        registry = create_default_registry()
        ts_bytes = (FIXTURES_DIR / "sample_code.ts").read_bytes()

        parsed = registry.parse(ts_bytes, filename="sample_code.ts")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        symbols = [s.extra.get("symbol_path") for s in parsed.locator_spans]
        assert "IngestItem" in symbols
        assert "PipelineManager" in symbols


class TestMediaParsers:
    def test_srt_timecodes_ms(self):
        registry = create_default_registry()
        srt_bytes = (FIXTURES_DIR / "sample.srt").read_bytes()

        parsed = registry.parse(srt_bytes, filename="sample.srt")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        assert len(parsed.locator_spans) == 2

        # 00:00:01,000 -> 1000ms, 00:00:04,000 -> 4000ms
        assert parsed.locator_spans[0].time_start_ms == 1000
        assert parsed.locator_spans[0].time_end_ms == 4000
        assert (
            "안녕하세요"
            in parsed.normalized_body[
                parsed.locator_spans[0].start_offset : parsed.locator_spans[
                    0
                ].end_offset
            ]
        )

        # 00:00:04,500 -> 4500ms, 00:00:08,000 -> 8000ms
        assert parsed.locator_spans[1].time_start_ms == 4500
        assert parsed.locator_spans[1].time_end_ms == 8000

    def test_vtt_timecodes_ms(self):
        registry = create_default_registry()
        vtt_bytes = (FIXTURES_DIR / "sample.vtt").read_bytes()

        parsed = registry.parse(vtt_bytes, filename="sample.vtt")
        assert parsed.status == ParseStatus.SUCCEEDED.value
        assert len(parsed.locator_spans) == 2
        assert parsed.locator_spans[0].time_start_ms == 1000
        assert parsed.locator_spans[0].time_end_ms == 4000
        assert parsed.locator_spans[1].time_start_ms == 4500
        assert parsed.locator_spans[1].time_end_ms == 9000


class TestRegistryAndChunkingIntegration:
    def test_unsupported_format_failure(self):
        registry = create_default_registry()
        data = (FIXTURES_DIR / "unsupported.xyz").read_bytes()

        parsed = registry.parse(data, filename="unsupported.xyz")
        assert parsed.status == ParseStatus.FAILED.value
        assert parsed.error_code == "unsupported_format"

    def test_empty_content_failure(self):
        registry = create_default_registry()
        parsed = registry.parse(b"", filename="empty.txt")
        assert parsed.status == ParseStatus.FAILED.value
        assert parsed.error_code == "empty_content"

    def test_chunking_pdf_preserves_page_numbers(self):
        registry = create_default_registry()
        pdf_bytes = (FIXTURES_DIR / "sample_spec.pdf").read_bytes()
        parsed = registry.parse(pdf_bytes, filename="sample_spec.pdf")

        chunks = chunk_structured_document(
            parsed.normalized_body,
            locator_spans=parsed.locator_spans,
            target_chars=200,
            max_chars=400,
            min_chars=30,
        )
        assert len(chunks) == 3
        for c in chunks:
            assert c.page_no in (1, 2, 3)
            # Invariant: body slice and digest
            assert parsed.normalized_body[c.char_start : c.char_end] == c.body
            assert hashlib.sha256(c.body.encode("utf-8")).hexdigest() == c.sha256

    def test_chunking_pptx_preserves_slide_numbers(self):
        registry = create_default_registry()
        pptx_bytes = (FIXTURES_DIR / "review_deck.pptx").read_bytes()
        parsed = registry.parse(pptx_bytes, filename="review_deck.pptx")

        chunks = chunk_structured_document(
            parsed.normalized_body,
            locator_spans=parsed.locator_spans,
            target_chars=200,
            max_chars=400,
            min_chars=30,
        )
        assert len(chunks) == 4
        for c in chunks:
            assert c.slide_no in (1, 2, 3, 4)
            assert parsed.normalized_body[c.char_start : c.char_end] == c.body
            assert hashlib.sha256(c.body.encode("utf-8")).hexdigest() == c.sha256

    def test_chunking_subtitles_preserves_timecodes(self):
        registry = create_default_registry()
        srt_bytes = (FIXTURES_DIR / "sample.srt").read_bytes()
        parsed = registry.parse(srt_bytes, filename="sample.srt")

        chunks = chunk_structured_document(
            parsed.normalized_body,
            locator_spans=parsed.locator_spans,
            target_chars=100,
            max_chars=300,
            min_chars=10,
        )
        assert len(chunks) >= 1
        for c in chunks:
            assert c.time_start_ms is not None
            assert c.time_end_ms is not None
            assert parsed.normalized_body[c.char_start : c.char_end] == c.body


class TestLocatorsAndResourceSecurity:
    def test_decomposed_subtitle_nfc_locator_integrity(self):
        registry = create_default_registry()
        # Decomposed combining character (e + U+0301) and Hangul Jamo with blank line separator
        raw_srt = (
            "1\n00:00:01,000 --> 00:00:02,000\ne\u0301 text\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n\u1100\u1161\u11a8\n"
        ).encode("utf-8")
        parsed = registry.parse(raw_srt, filename="decomposed.srt")

        assert parsed.status == ParseStatus.SUCCEEDED.value
        b_len = len(parsed.normalized_body)
        assert len(parsed.locator_spans) == 2

        # Invariant: 0 <= start <= end <= len(body) and exact slice matches
        for span in parsed.locator_spans:
            assert 0 <= span.start_offset <= span.end_offset <= b_len
            slice_text = parsed.normalized_body[span.start_offset : span.end_offset]
            assert slice_text == span.extra.get("exact_text")

    def test_zip_bomb_prevention_on_office_parsers(self):
        import io
        import zipfile

        # Synthesize a zip bomb with > 10MB uncompressed content
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("word/document.xml", b"A" * (15 * 1024 * 1024))
        bomb_bytes = buf.getvalue()

        registry = create_default_registry()
        # Custom limits to trigger limit check
        custom_limits = ParserResourceLimits(max_zip_uncompressed_bytes=5 * 1024 * 1024)
        reg_limited = ParserRegistry(limits=custom_limits)
        for p in registry._parsers:
            reg_limited.register(p)

        parsed_docx = reg_limited.parse(bomb_bytes, filename="bomb.docx")
        assert parsed_docx.status == ParseStatus.FAILED.value
        assert parsed_docx.error_code == "limits_exceeded"

        parsed_pptx = reg_limited.parse(bomb_bytes, filename="bomb.pptx")
        assert parsed_pptx.status == ParseStatus.FAILED.value
        assert parsed_pptx.error_code == "limits_exceeded"

        parsed_xlsx = reg_limited.parse(bomb_bytes, filename="bomb.xlsx")
        assert parsed_xlsx.status == ParseStatus.FAILED.value
        assert parsed_xlsx.error_code == "limits_exceeded"

    def test_pdf_page_limit_enforcement(self):
        registry = create_default_registry()
        pdf_bytes = (FIXTURES_DIR / "sample_spec.pdf").read_bytes()  # 3 pages

        # Limit to max 2 pages
        reg_limited = ParserRegistry(limits=ParserResourceLimits(max_pdf_pages=2))
        for p in registry._parsers:
            reg_limited.register(p)

        parsed = reg_limited.parse(pdf_bytes, filename="spec.pdf")
        assert parsed.status == ParseStatus.FAILED.value
        assert parsed.error_code == "limits_exceeded"

    def test_xxe_external_entity_not_expanded(self):
        # XML with XXE external entity injection
        xxe_xml = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE root [ <!ENTITY test SYSTEM "file:///etc/passwd"> ]>'
            b"<root>&test;</root>"
        )
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("word/document.xml", xxe_xml)
            zf.writestr("[Content_Types].xml", b'<?xml version="1.0"?><Types/>')
        doc_bytes = buf.getvalue()

        registry = create_default_registry()
        parsed = registry.parse(doc_bytes, filename="xxe.docx")
        # Either safely rejected or entity not expanded into system files
        assert "/etc/passwd" not in parsed.normalized_body
        assert "root:x:" not in parsed.normalized_body
