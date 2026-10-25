"""Structured chunking preserving exact offsets, heading hierarchy, and locator metadata."""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import re
import unicodedata
from typing import Any

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
# Kept for import compatibility; fence detection is line based (see parse_markdown_spans).
FENCE_CODE_PATTERN = re.compile(
    r"^```[^\n]*\n[\s\S]*?\n```|^~~~[^\n]*\n[\s\S]*?\n~~~", re.MULTILINE
)
TABLE_ROW_PATTERN = re.compile(r"^\|.*\|[ \t]*$", re.MULTILINE)
# A table has a header only when its second row is a delimiter row such as |---|:--:|
TABLE_DELIMITER_PATTERN = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")

_LINE_PATTERN = re.compile(r"[^\n]*\n|[^\n]+")
_FENCE_OPEN_PATTERN = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_LIST_MARKER_PATTERN = re.compile(r"^\s*([-*+]|\d+\.)\s+")
_SENTENCE_BOUNDARY_PATTERN = re.compile(r"[.!?。！？][\"'”’)\]]*\s+|\n[ \t\r]*\n\s*")
_MAX_PREFIX_CHARS = 200


@dataclasses.dataclass(frozen=True)
class LocatorSpan:
    """A logical or physical span in the original normalized document body."""

    start_offset: int
    end_offset: int
    section_path: list[str] = dataclasses.field(default_factory=list)
    chunk_kind: str = "prose"  # prose | code | table | list | heading | transcript
    page_no: int | None = None
    slide_no: int | None = None
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    context_prefix: str | None = None
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class StructuredChunk:
    """A chunk derived from a structured document adhering to DB ledger invariants."""

    ord: int
    char_start: int
    char_end: int
    body: str
    sha256: str
    section_path: list[str] = dataclasses.field(default_factory=list)
    chunk_kind: str = "prose"
    page_no: int | None = None
    slide_no: int | None = None
    time_start_ms: int | None = None
    time_end_ms: int | None = None
    token_estimate: int = 0
    search_text: str = ""
    context_prefix: str | None = None
    extra: dict[str, Any] = dataclasses.field(default_factory=dict)


def _estimate_tokens(text: str) -> int:
    """Deterministic token estimate balancing CJK and latin characters."""
    if not text:
        return 0
    # Rule of thumb: ~3.2 characters per token on average for mixed Korean/English/code
    return max(1, int(len(text) / 3.2))


def _build_search_text(
    body: str, section_path: list[str], context_prefix: str | None
) -> str:
    parts: list[str] = []
    if section_path:
        parts.append(f"[{' > '.join(section_path)}]")
    if context_prefix:
        parts.append(f"({context_prefix.strip()})")
    parts.append(body)
    return "\n".join(parts)


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    """Shrink [start, end) past leading/trailing whitespace; empty result when only whitespace."""
    raw = text[start:end]
    stripped = raw.lstrip()
    if not stripped:
        return start, start
    lead = len(raw) - len(stripped)
    trail = len(stripped) - len(stripped.rstrip())
    return start + lead, end - trail


def parse_markdown_spans(text: str) -> list[LocatorSpan]:
    """Parse markdown into structural LocatorSpans (heading trail, code, table, list, prose).

    The text is first cut into whole lines and every line belongs to exactly one block, so every
    non-whitespace character lands in exactly one span; trimming only ever removes whitespace.
    """
    if not text:
        return []

    spans: list[LocatorSpan] = []
    trail: list[tuple[int, str]] = []  # (level, title)
    section_path: list[str] = []
    block_start: int | None = None
    block_kind = "prose"

    def emit(start: int, end: int, kind: str, prefix: str | None = None) -> None:
        s, e = _trim(text, start, end)
        if e > s:
            spans.append(
                LocatorSpan(
                    start_offset=s,
                    end_offset=e,
                    section_path=list(section_path),
                    chunk_kind=kind,
                    context_prefix=prefix,
                    extra={"exact_text": text[s:e]},
                )
            )

    def flush(end: int) -> None:
        nonlocal block_start
        if block_start is not None:
            emit(block_start, end, block_kind)
            block_start = None

    lines = [(m.start(), m.end()) for m in _LINE_PATTERN.finditer(text)]
    total = len(lines)
    k = 0
    while k < total:
        line_start, line_end = lines[k]
        content = text[line_start:line_end].rstrip("\r\n")

        fence = _FENCE_OPEN_PATTERN.match(content)
        if fence and not (fence.group(1)[0] == "`" and "`" in fence.group(2)):
            flush(line_start)
            marker = fence.group(1)
            closing = re.compile(
                rf"^ {{0,3}}{re.escape(marker[0])}{{{len(marker)},}}[ \t]*$"
            )
            k += 1
            while k < total:  # an unclosed fence runs to the end of the document
                inner = text[lines[k][0] : lines[k][1]].rstrip("\r\n")
                k += 1
                if closing.match(inner):
                    break
            emit(
                line_start,
                lines[k - 1][1],
                "code",
                content.strip()[:_MAX_PREFIX_CHARS],
            )
            continue

        heading = HEADING_PATTERN.match(content)
        if heading:
            flush(line_start)
            level = len(heading.group(1))
            while trail and trail[-1][0] >= level:
                trail.pop()
            trail.append((level, heading.group(2).strip()))
            section_path = [t for _, t in trail]
            emit(line_start, line_end, "heading")
        elif TABLE_ROW_PATTERN.match(content):
            if block_start is None or block_kind != "table":
                flush(line_start)
                block_start, block_kind = line_start, "table"
        elif not content.strip():
            flush(line_start)
        else:
            if block_kind == "table":
                flush(line_start)
            if block_start is None:
                block_start = line_start
                block_kind = "list" if _LIST_MARKER_PATTERN.match(content) else "prose"
        k += 1

    flush(len(text))
    return spans


def _is_unsafe_cut(body: str, pos: int) -> bool:
    """True when cutting between body[pos-1] and body[pos] would split a grapheme/surrogate pair."""
    if pos <= 0 or pos >= len(body):
        return False
    ch, prev = body[pos], body[pos - 1]
    if unicodedata.category(ch) in ("Mn", "Me", "Mc") or ch == "‍":
        return True
    if prev == "‍" or "\U0001f3fb" <= ch <= "\U0001f3ff":
        return True
    return "\udc00" <= ch <= "\udfff" and "\ud800" <= prev <= "\udbff"


def _hard_cuts(
    body: str, start: int, end: int, max_chars: int
) -> list[tuple[int, int]]:
    """Partition [start, end) into consecutive pieces of at most max_chars characters."""
    pieces: list[tuple[int, int]] = []
    pos = start
    while pos < end:
        hi = min(pos + max_chars, end)
        if hi < end:
            window_hi = hi
            # Prefer a whitespace break in the second half of the window, then a cluster-safe cut
            for q in range(window_hi, pos + max_chars // 2, -1):
                if body[q - 1].isspace():
                    hi = q
                    break
            else:
                while hi > pos + 1 and _is_unsafe_cut(body, hi):
                    hi -= 1
                if _is_unsafe_cut(body, hi):
                    hi = window_hi
        pieces.append((pos, hi))
        pos = hi
    return pieces


def _boundary_offsets(body: str, start: int, end: int, kind: str) -> list[int]:
    """Candidate cut offsets strictly inside (start, end) for the given block kind."""
    if kind in ("code", "table"):
        return [i + 1 for i in range(start, end - 1) if body[i] == "\n"]
    return [
        m.end() + start
        for m in _SENTENCE_BOUNDARY_PATTERN.finditer(body[start:end])
        if start < m.end() + start < end
    ]


def _split_ranges(
    body: str, start: int, end: int, kind: str, max_chars: int
) -> list[tuple[int, int]]:
    """Split [start, end) into a contiguous partition of ranges, each at most max_chars long.

    Boundary candidates are computed first, the whole range is cut into atoms at them, and atoms
    are packed greedily; an atom longer than max_chars falls back to hard cuts. The result is a
    partition of [start, end), so no character can be dropped or duplicated.
    """
    if end - start <= max_chars:
        return [(start, end)]
    edges = [start, *_boundary_offsets(body, start, end, kind), end]
    out: list[tuple[int, int]] = []
    cur_start: int | None = None
    cur_end = start
    for a, b in itertools.pairwise(edges):
        if b <= a:
            continue
        if b - a > max_chars:
            if cur_start is not None:
                out.append((cur_start, cur_end))
                cur_start = None
            parts = _hard_cuts(body, a, b, max_chars)
            out.extend(parts[:-1])
            cur_start, cur_end = parts[-1]
        elif cur_start is None:
            cur_start, cur_end = a, b
        elif b - cur_start <= max_chars:
            cur_end = b
        else:
            out.append((cur_start, cur_end))
            cur_start, cur_end = a, b
    if cur_start is not None:
        out.append((cur_start, cur_end))
    return out


def _table_header_prefix(text: str, max_chars: int) -> str | None:
    """Header rows to repeat on continuation chunks; only when row 2 is a |---| delimiter row."""
    lines = text.split("\n")
    if len(lines) < 2:
        return None
    first, second = lines[0].strip(), lines[1].strip()
    if not TABLE_ROW_PATTERN.match(first) or not TABLE_DELIMITER_PATTERN.match(second):
        return None
    prefix = f"{first}\n{second}"
    return prefix if len(prefix) <= max_chars else None


def _partition_spans(body: str, spans: list[LocatorSpan]) -> list[LocatorSpan]:
    """Turn arbitrary (unsorted, overlapping, gapped) spans into ordered disjoint segments.

    Overlapped parts are credited to the earlier span. A gap that holds non-whitespace text
    (e.g. a "---" slide separator a parser left outside its spans) is never dropped: it is absorbed
    by the previous (else the next) span unless that one is code/table, otherwise it becomes a
    metadata-less prose segment.
    """
    size = len(body)
    atomic = ("code", "table")
    out: list[LocatorSpan] = []
    cursor = 0

    def absorb_gap(upto: int, nxt: LocatorSpan | None) -> LocatorSpan | None:
        """Cover non-whitespace text in [cursor, upto); returns the adjusted next span."""
        if not body[cursor:upto].strip():
            return nxt
        if out and out[-1].chunk_kind not in atomic:
            out[-1] = dataclasses.replace(out[-1], end_offset=upto)
        elif nxt is not None and nxt.chunk_kind not in atomic:
            return dataclasses.replace(nxt, start_offset=cursor)
        else:
            out.append(LocatorSpan(start_offset=cursor, end_offset=upto))
        return nxt

    for sp in sorted(spans, key=lambda s: (s.start_offset, s.end_offset)):
        s, e = max(sp.start_offset, cursor), min(sp.end_offset, size)
        if e <= s:
            continue
        adjusted = absorb_gap(s, dataclasses.replace(sp, start_offset=s, end_offset=e))
        out.append(adjusted)
        cursor = e
    absorb_gap(size, None)
    return out


def chunk_structured_document(
    body: str,
    locator_spans: list[LocatorSpan] | None = None,
    *,
    target_chars: int = 750,
    max_chars: int = 1200,
    min_chars: int = 180,
) -> list[StructuredChunk]:
    """Chunk document body adhering to ledger invariants: exact offsets, SHA-256, and structure.

    Guarantees, for any body and any spans:
    - chunk.body == body[char_start:char_end] and len(chunk.body) <= max_chars
    - chunk ranges are ordered and disjoint
    - every non-whitespace character of body belongs to exactly one chunk
    """
    if not body:
        return []
    max_chars = max(1, max_chars)

    segments = _partition_spans(
        body, list(locator_spans) if locator_spans else parse_markdown_spans(body)
    )

    # Split oversized segments; pieces form a partition so non-whitespace text is never lost
    pieces: list[LocatorSpan] = []
    for seg in segments:
        s, e = _trim(body, seg.start_offset, seg.end_offset)
        if e <= s:
            continue
        prefix = seg.context_prefix
        if seg.chunk_kind == "code" and not prefix:
            first_line = body[s:e].split("\n", 1)[0].strip()
            if first_line.startswith(("```", "~~~")):
                prefix = first_line[:_MAX_PREFIX_CHARS]
        header = (
            _table_header_prefix(body[s:e], max_chars)
            if seg.chunk_kind == "table" and e - s > max_chars
            else None
        )
        first = True
        for ps, pe in _split_ranges(body, s, e, seg.chunk_kind, max_chars):
            ps, pe = _trim(body, ps, pe)
            if pe <= ps:
                continue
            if seg.chunk_kind == "table":
                piece_prefix = seg.context_prefix if first else header
            else:
                piece_prefix = prefix
            pieces.append(
                dataclasses.replace(
                    seg, start_offset=ps, end_offset=pe, context_prefix=piece_prefix
                )
            )
            first = False

    if not pieces:
        return []

    # Merge adjacent short pieces that share context (only whitespace lies between them)
    merged: list[LocatorSpan] = []
    cur: LocatorSpan | None = None
    for sp in pieces:
        if cur is None:
            cur = sp
            continue
        can_merge = (
            cur.end_offset - cur.start_offset < min_chars
            and cur.section_path == sp.section_path
            and cur.page_no == sp.page_no
            and cur.slide_no == sp.slide_no
            and (
                cur.chunk_kind == sp.chunk_kind
                or (cur.chunk_kind == "heading" and sp.chunk_kind == "prose")
            )
            and cur.chunk_kind not in ("code", "table")
            and sp.chunk_kind not in ("code", "table")
            and sp.end_offset - cur.start_offset <= max_chars
            and body[cur.end_offset : sp.start_offset].strip() == ""
        )
        if can_merge:
            cur = dataclasses.replace(
                cur,
                end_offset=sp.end_offset,
                chunk_kind=sp.chunk_kind,
                time_end_ms=sp.time_end_ms or cur.time_end_ms,
            )
        else:
            merged.append(cur)
            cur = sp
    if cur is not None:
        merged.append(cur)

    chunks: list[StructuredChunk] = []
    for idx, sp in enumerate(merged):
        chunk_body = body[sp.start_offset : sp.end_offset]
        chunks.append(
            StructuredChunk(
                ord=idx,
                char_start=sp.start_offset,
                char_end=sp.end_offset,
                body=chunk_body,
                sha256=hashlib.sha256(
                    chunk_body.encode("utf-8", errors="replace")
                ).hexdigest(),
                section_path=list(sp.section_path),
                chunk_kind=sp.chunk_kind,
                page_no=sp.page_no,
                slide_no=sp.slide_no,
                time_start_ms=sp.time_start_ms,
                time_end_ms=sp.time_end_ms,
                token_estimate=_estimate_tokens(chunk_body),
                search_text=_build_search_text(
                    chunk_body, sp.section_path, sp.context_prefix
                ),
                context_prefix=sp.context_prefix,
                extra={**sp.extra, "exact_text": chunk_body},
            )
        )
    return chunks
