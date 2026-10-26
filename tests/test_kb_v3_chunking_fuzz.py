"""Chunking contract: exact slices, size cap, ordered/disjoint ranges, every non-space char covered once.

Seeded fuzzing over markdown, fences, tables, headings, CRLF, combining marks, astral characters and
lone surrogates, with and without parser supplied spans.
"""

from __future__ import annotations

import random

import pytest

from ttakkari.common.chunking import (
    LocatorSpan,
    chunk_structured_document,
    parse_markdown_spans,
)


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    """Override session DB fixture from conftest for pure unit testing without DB connection."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """Override per-test DB clean fixture from conftest."""
    yield


SEED = 20261025
FUZZ_DOCS = 12_000
SPAN_FUZZ_DOCS = 3_000

ATOMS = [
    "alpha",
    "beta",
    "한글",
    "가나다라",
    "é",
    "à́̂",
    "각́",
    " ",
    "  ",
    "\t",
    "\n",
    "\n\n",
    "\r\n",
    "\r\n\r\n",
    ".",
    ". ",
    "! ",
    "? ",
    "다. ",
    "。",
    " ",
    "\x85",
    " ",
    "\x0b",
    "```",
    "```py\n",
    "\n```\n",
    "~~~",
    "~~~md\n",
    "\n~~~\n",
    "````",
    "|a|b|",
    "|---|---|",
    "| h1 | h2 |\n|---|:--:|\n",
    "\n| x | y |",
    "# Heading",
    "\n## Sub\n",
    "### ",
    "- item ",
    "1. item ",
    "🙂",
    "👨‍👩‍👧",
    "👍\U0001f3fd",
    "\ud800",
    "\udc00",
    "\ud83d",
    "x" * 40,
    "y" * 130,
]


def _random_body(rng: random.Random) -> str:
    parts = []
    for _ in range(rng.randrange(1, 260)):
        if rng.random() < 0.86:
            parts.append(rng.choice(ATOMS))
        else:
            parts.append(chr(rng.randrange(0x20, 0x400)))
    return "".join(parts)


def assert_contract(body: str, chunks, max_chars: int) -> None:
    covered = [0] * len(body)
    last_end = 0
    for c in chunks:
        # (a) the body is exactly the slice
        assert c.body == body[c.char_start : c.char_end]
        # (b) size cap
        assert 0 < len(c.body) <= max_chars, (len(c.body), max_chars)
        # (d) ordered and disjoint
        assert c.char_start >= last_end, (last_end, c.char_start)
        assert 0 <= c.char_start < c.char_end <= len(body)
        last_end = c.char_end
        for i in range(c.char_start, c.char_end):
            covered[i] += 1
    # (c) every non-whitespace character belongs to exactly one chunk
    for i, ch in enumerate(body):
        if not ch.isspace():
            assert covered[i] == 1, (
                i,
                repr(ch),
                covered[i],
                repr(body[max(0, i - 20) : i + 20]),
            )


def test_minimal_counterexample_last_period_is_not_dropped():
    body = "```\n\n```."
    chunks = chunk_structured_document(body, target_chars=1, max_chars=2, min_chars=1)
    assert_contract(body, chunks, 2)
    assert "".join(c.body for c in chunks).replace("\n", "").endswith(".")
    # default sizes keep the whole fenced block and the trailing period together as code
    whole = chunk_structured_document(body)
    assert_contract(body, whole, 1200)


@pytest.mark.parametrize(
    "body",
    [
        "```\n\n```.",
        "```py\nx = 1\n```trailing text",
        "~~~\n```\ninner\n```\n~~~\nafter",
        "para\n```\ncode",  # unclosed fence runs to the end of the document
        "# T\r\n\r\n```\r\ncode\r\n```\r\n\r\nend.",
        "|a|b|\ntext right after table\n|c|d|",
        "𐀀 lone surrogates \ud83d",
        "각" + "́" * 60,
    ],
)
def test_structural_edge_cases_cover_everything(body: str):
    for max_chars in (1, 3, 8, 24, 1200):
        chunks = chunk_structured_document(
            body, target_chars=max(1, max_chars // 2), max_chars=max_chars, min_chars=1
        )
        assert_contract(body, chunks, max_chars)


def test_markdown_spans_partition_nonspace_text_exactly_once():
    rng = random.Random(SEED + 1)
    for _ in range(2_000):
        body = _random_body(rng)
        spans = parse_markdown_spans(body)
        covered = [0] * len(body)
        last_end = 0
        for sp in spans:
            assert sp.start_offset >= last_end
            assert body[sp.start_offset : sp.end_offset] == sp.extra["exact_text"]
            assert not body[sp.start_offset].isspace()
            assert not body[sp.end_offset - 1].isspace()
            last_end = sp.end_offset
            for i in range(sp.start_offset, sp.end_offset):
                covered[i] += 1
        for i, ch in enumerate(body):
            if not ch.isspace():
                assert covered[i] == 1


def test_fuzz_contract_without_spans():
    rng = random.Random(SEED)
    total_chunks = 0
    for _ in range(FUZZ_DOCS):
        body = _random_body(rng)
        max_chars = rng.randrange(1, 90)
        min_chars = rng.choice([1, max_chars // 3, max_chars])
        chunks = chunk_structured_document(
            body,
            target_chars=max(1, max_chars // 2),
            max_chars=max_chars,
            min_chars=min_chars,
        )
        assert_contract(body, chunks, max_chars)
        if body.strip():
            assert chunks
        total_chunks += len(chunks)
    assert total_chunks > FUZZ_DOCS


def test_fuzz_contract_with_arbitrary_parser_spans():
    """Spans may overlap, be unsorted, run out of range or leave gaps; the contract still holds."""
    rng = random.Random(SEED + 2)
    kinds = ["prose", "code", "table", "list", "heading", "transcript"]
    for _ in range(SPAN_FUZZ_DOCS):
        body = _random_body(rng)
        spans = []
        for _ in range(rng.randrange(1, 8)):
            a = rng.randrange(-5, len(body) + 5)
            b = a + rng.randrange(-3, max(2, len(body) // 2))
            spans.append(
                LocatorSpan(
                    a,
                    b,
                    section_path=[rng.choice("AB")],
                    chunk_kind=rng.choice(kinds),
                    page_no=rng.choice([None, 1, 2]),
                )
            )
        max_chars = rng.randrange(1, 90)
        chunks = chunk_structured_document(
            body,
            locator_spans=spans,
            target_chars=max(1, max_chars // 2),
            max_chars=max_chars,
            min_chars=rng.choice([1, max_chars]),
        )
        assert_contract(body, chunks, max_chars)


def test_table_header_prefix_only_with_delimiter_row():
    rows = "\n".join(f"| r{i} | {'v' * 20} |" for i in range(12))
    with_header = "| name | value |\n|---|---|\n" + rows
    without_header = rows
    kwargs = {"target_chars": 60, "max_chars": 120, "min_chars": 1}

    chunks = chunk_structured_document(with_header, **kwargs)
    assert len(chunks) > 1
    assert_contract(with_header, chunks, 120)
    assert chunks[0].context_prefix is None
    assert all(c.context_prefix == "| name | value |\n|---|---|" for c in chunks[1:])

    chunks = chunk_structured_document(without_header, **kwargs)
    assert len(chunks) > 1
    assert_contract(without_header, chunks, 120)
    # the first data row must not be promoted to a header
    assert all(c.context_prefix is None for c in chunks)
    assert all("r0" not in (c.context_prefix or "") for c in chunks)


def test_prose_line_after_table_is_not_swallowed_into_the_table():
    spans = parse_markdown_spans("|a|b|\n|---|---|\n|1|2|\nplain text\n")
    assert [(s.chunk_kind, s.extra["exact_text"]) for s in spans] == [
        ("table", "|a|b|\n|---|---|\n|1|2|"),
        ("prose", "plain text"),
    ]
