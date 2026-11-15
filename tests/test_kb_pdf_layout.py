"""PDF 레이아웃 복원(다단 읽기 순서, 표, 제목, 구조 태그) 시험. 좌표를 직접 지정한 합성 PDF 를 쓴다."""

from __future__ import annotations

import io

import pypdf
from pypdf.generic import ArrayObject, BooleanObject, DictionaryObject, NameObject

from ttakkari.kb_parsers import (
    create_default_registry,
    pdf_layout,
    validate_parsed_document,
)


def make_pdf(pages: list[list[tuple[float, float, float, str]]]) -> bytes:
    """(x, y, 글자 크기, 글) 목록을 쪽마다 그대로 찍는 PDF. 줄은 주어진 순서대로 콘텐츠 스트림에 들어간다."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    font_id = 3 + 2 * n
    for i, ops in enumerate(pages):
        stream = "\n".join(
            f"BT /F1 {size} Tf 1 0 0 1 {x} {y} Tm ({text.replace('(', '[').replace(')', ']')}) Tj ET" for x, y, size, text in ops
        ).encode()
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + body + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def parse(pdf: bytes):
    return validate_parsed_document(create_default_registry().parse(pdf, filename="doc.pdf", content_type="application/pdf"))


LEFT = [f"left column sentence number {i} with enough words" for i in range(1, 9)]
RIGHT = [f"right column sentence number {i} with enough words" for i in range(1, 9)]


def two_column_ops() -> list[tuple[float, float, float, str]]:
    ops = []
    for i in range(8):  # 줄 단위로 번갈아 적어 콘텐츠 스트림 순서가 읽기 순서와 다르게 한다.
        ops.append((50, 700 - 14 * i, 10, LEFT[i]))
        ops.append((320, 700 - 14 * i, 10, RIGHT[i]))
    return ops


def test_two_column_page_reads_left_column_before_right():
    parsed = parse(make_pdf([two_column_ops()]))
    assert parsed.status == "succeeded"
    assert parsed.metadata["parser"] == "pdf_layout_v1"
    assert parsed.metadata["layout_column_pages"] == 1
    body = parsed.normalized_body
    assert body.index(LEFT[7]) < body.index(RIGHT[0])
    assert body.index(LEFT[0]) < body.index(LEFT[7])


def test_plain_pdf_text_extraction_would_interleave_columns():
    reader = pypdf.PdfReader(io.BytesIO(make_pdf([two_column_ops()])))
    plain = reader.pages[0].extract_text()
    assert plain.index(RIGHT[0]) < plain.index(LEFT[7])  # 기본 추출은 줄이 섞인다(개선 전 상태 확인)


def test_table_rows_become_markdown_table_span():
    rows = [("Name", "Count", "Ratio"), ("pandas", "209", "44.0"), ("mypy", "193", "53.4"), ("scikit", "118", "52.5")]
    ops = [(50, 740, 10, "Intro paragraph that explains the table below in a normal sentence.")]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            ops.append((50 + 150 * c, 700 - 14 * r, 10, text))
    ops.append((50, 600, 10, "Closing paragraph after the table in a normal sentence of prose."))
    parsed = parse(make_pdf([ops]))
    tables = [s for s in parsed.locator_spans if s.chunk_kind == "table"]
    assert len(tables) == 1
    text = parsed.normalized_body[tables[0].start_offset : tables[0].end_offset]
    assert text.splitlines()[0] == "| Name | Count | Ratio |"
    assert "| pandas | 209 | 44.0 |" in text
    assert parsed.metadata["layout_tables"] == 1


def test_large_font_line_becomes_heading_with_section_path():
    ops = [(50, 740, 20, "Design Overview")]
    ops += [(50, 700 - 14 * i, 10, f"Body sentence number {i} that is long enough to count as prose text.") for i in range(6)]
    parsed = parse(make_pdf([ops]))
    heading = [s for s in parsed.locator_spans if s.chunk_kind == "heading"]
    assert len(heading) == 1 and parsed.normalized_body[heading[0].start_offset :].startswith("# Design Overview")
    prose = [s for s in parsed.locator_spans if s.chunk_kind == "prose"]
    assert prose and prose[0].section_path == ["Design Overview"] and prose[0].page_no == 1


def test_uniform_text_keeps_basic_extraction_tier():
    ops = [(50, 740 - 14 * i, 10, f"Plain line {i} of a document with a single uniform font size") for i in range(8)]
    parsed = parse(make_pdf([ops]))
    assert parsed.metadata["parser"] == "pdf_pypdf_v1"
    assert [s.page_no for s in parsed.locator_spans] == [1]


def _tagged_reader(table_on_page: int | None, *, marked: bool = True):
    writer = pypdf.PdfWriter()
    for _ in range(2):
        writer.add_blank_page(612, 792)
    root = writer._root_object
    if marked:
        root[NameObject("/MarkInfo")] = DictionaryObject({NameObject("/Marked"): BooleanObject(True)})
        kids = ArrayObject()
        if table_on_page is not None:
            table = DictionaryObject(
                {NameObject("/S"): NameObject("/Table"), NameObject("/Pg"): writer.pages[table_on_page].indirect_reference}
            )
            kids.append(writer._add_object(table))
        root[NameObject("/StructTreeRoot")] = writer._add_object(
            DictionaryObject({NameObject("/Type"): NameObject("/StructTreeRoot"), NameObject("/K"): kids})
        )
    buf = io.BytesIO()
    writer.write(buf)
    return pypdf.PdfReader(io.BytesIO(buf.getvalue()))


def test_structure_tags_report_table_pages():
    assert pdf_layout.structure_table_pages(_tagged_reader(1)) == {1}
    assert pdf_layout.structure_table_pages(_tagged_reader(None)) == set()
    assert pdf_layout.structure_table_pages(_tagged_reader(None, marked=False)) is None


def test_tagged_page_without_table_suppresses_table_detection():
    rows = [("Name", "Count"), ("pandas", "209"), ("mypy", "193"), ("scikit", "118")]
    ops = [(50 + 150 * c, 700 - 14 * r, 10, t) for r, row in enumerate(rows) for c, t in enumerate(row)]
    frags = pdf_layout.collect_fragments(pypdf.PdfReader(io.BytesIO(make_pdf([ops]))).pages[0])
    assert pdf_layout.analyze_page(frags, 612, table_allowed=True).tables == 1
    assert pdf_layout.analyze_page(frags, 612, table_allowed=False).tables == 0


def _run_worker(pdf: bytes) -> dict:
    import json
    import os
    import subprocess
    import sys
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[1] / "src")
    done = subprocess.run(
        [sys.executable, "-m", "ttakkari.collector.pdf_worker"], input=pdf, capture_output=True, timeout=60,
        env={**os.environ, "PYTHONPATH": src}, check=True,
    )
    return json.loads(done.stdout)


def test_collector_worker_restores_column_order_and_marks_new_parser_version():
    result = _run_worker(make_pdf([two_column_ops()]))
    assert result["parser_version"].endswith("+pdf-2")
    text = result["text"]
    assert text.index(LEFT[7]) < text.index(RIGHT[0])
    page = result["pages"][0]
    assert page["page"] == 1 and page["start"] == 0 and page["end"] == len(text)


def test_collector_worker_emits_markdown_heading_and_table_for_the_pipeline_parser():
    ops = [(50, 740, 20, "Design Overview")]
    ops += [(50, 700 - 14 * i, 10, f"Body sentence number {i} that is long enough to count as prose text.") for i in range(3)]
    rows = [("Name", "Count", "Ratio"), ("pandas", "209", "44.0"), ("mypy", "193", "53.4"), ("scikit", "118", "52.5")]
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            ops.append((50 + 150 * c, 640 - 14 * r, 10, text))
    result = _run_worker(make_pdf([ops]))
    assert result["text"].startswith("# Design Overview")
    assert "| Name | Count | Ratio |" in result["text"]


def test_collector_worker_keeps_plain_tier_for_uniform_text():
    ops = [(50, 740 - 14 * i, 10, f"Plain line {i} of a document with a single uniform font size") for i in range(8)]
    result = _run_worker(make_pdf([ops]))
    assert result["parser_version"].endswith("+pdf-1")
    assert "Plain line 7" in result["text"]


def test_collector_worker_falls_back_to_plain_when_cpu_budget_is_spent(monkeypatch):
    import time

    from ttakkari.collector import pdf_worker

    reader = pypdf.PdfReader(io.BytesIO(make_pdf([two_column_ops()])))
    plain = {0: reader.pages[0].extract_text()}
    assert pdf_worker._layout_pages(reader, plain, 1) is not None
    monkeypatch.setattr(time, "process_time", lambda: pdf_worker.LAYOUT_START_CPU + 1)
    assert pdf_worker._layout_pages(reader, plain, 1) is None
    monkeypatch.setattr(time, "process_time", lambda: 0.0)
    assert pdf_worker._layout_pages(reader, plain, pdf_worker.LAYOUT_MAX_PAGES + 1) is None
