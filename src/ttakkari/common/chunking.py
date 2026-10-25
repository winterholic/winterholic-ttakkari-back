"""Structured chunking preserving exact offsets, heading hierarchy, and locator metadata."""

from __future__ import annotations

import dataclasses
import hashlib
import re
from typing import Any

HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
FENCE_CODE_PATTERN = re.compile(
    r"^```[^\n]*\n[\s\S]*?\n```|^~~~[^\n]*\n[\s\S]*?\n~~~", re.MULTILINE
)
TABLE_ROW_PATTERN = re.compile(r"^\|.*\|[ \t]*$", re.MULTILINE)


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


def parse_markdown_spans(text: str) -> list[LocatorSpan]:
    """Parse raw markdown text into structural LocatorSpans tracking heading trail and full text coverage."""
    if not text:
        return []

    spans: list[LocatorSpan] = []
    trail: list[tuple[int, str]] = []  # (level, title)

    # First find all code blocks so we don't misinterpret markdown inside fences
    code_intervals: list[tuple[int, int]] = []
    for m in FENCE_CODE_PATTERN.finditer(text):
        code_intervals.append((m.start(), m.end()))

    def is_in_code(idx: int) -> tuple[int, int] | None:
        for s, e in code_intervals:
            if s <= idx < e:
                return (s, e)
        return None

    # Line-by-line scanning while tracking positions
    lines = text.splitlines(keepends=True)
    pos = 0
    cur_block_start: int | None = None
    cur_block_kind = "prose"
    cur_section_path: list[str] = []

    def flush_block(end_pos: int):
        nonlocal cur_block_start, cur_block_kind
        if cur_block_start is not None and end_pos > cur_block_start:
            raw = text[cur_block_start:end_pos]
            if raw.strip():
                lead = len(raw) - len(raw.lstrip())
                trail_ws = len(raw) - len(raw.rstrip())
                s = cur_block_start + lead
                e = end_pos - trail_ws
                if e > s:
                    spans.append(
                        LocatorSpan(
                            start_offset=s,
                            end_offset=e,
                            section_path=list(cur_section_path),
                            chunk_kind=cur_block_kind,
                            extra={"exact_text": text[s:e]},
                        )
                    )
        cur_block_start = None
        cur_block_kind = "prose"

    i = 0
    while i < len(lines):
        line = lines[i]
        line_start = pos
        line_end = pos + len(line)

        code_box = is_in_code(line_start)
        if code_box:
            flush_block(line_start)
            c_start, c_end = code_box
            raw = text[c_start:c_end]
            lead = len(raw) - len(raw.lstrip())
            trail_ws = len(raw) - len(raw.rstrip())
            s = c_start + lead
            e = c_end - trail_ws
            # Extract opening fence prefix (e.g. ```py) for contextual prefixing
            first_line = raw.split("\n")[0].strip()
            fence_match = re.match(r"^(`{3,}|~{3,})(\S*)", first_line)
            fence_prefix = first_line if fence_match else "```"
            if e > s:
                spans.append(
                    LocatorSpan(
                        start_offset=s,
                        end_offset=e,
                        section_path=list(cur_section_path),
                        chunk_kind="code",
                        context_prefix=fence_prefix,
                        extra={"exact_text": text[s:e]},
                    )
                )
            # Advance pos and i past the code block
            while i < len(lines) and pos < c_end:
                pos += len(lines[i])
                i += 1
            continue

        # Check heading
        hm = HEADING_PATTERN.match(line)
        if hm:
            flush_block(line_start)
            level = len(hm.group(1))
            title = hm.group(2).strip()
            while trail and trail[-1][0] >= level:
                trail.pop()
            trail.append((level, title))
            cur_section_path = [t for _, t in trail]

            # Preserve heading as span so non-whitespace text is covered
            lead = len(line) - len(line.lstrip())
            trail_ws = len(line) - len(line.rstrip())
            s = line_start + lead
            e = line_end - trail_ws
            if e > s:
                spans.append(
                    LocatorSpan(
                        start_offset=s,
                        end_offset=e,
                        section_path=list(cur_section_path),
                        chunk_kind="heading",
                        extra={"exact_text": text[s:e]},
                    )
                )
            pos = line_end
            i += 1
            continue

        # Check table row
        if TABLE_ROW_PATTERN.match(line):
            if cur_block_kind != "table":
                flush_block(line_start)
                cur_block_start = line_start
                cur_block_kind = "table"
        else:
            # Blank line separates paragraphs
            if not line.strip():
                flush_block(line_start)
            else:
                if cur_block_start is None:
                    cur_block_start = line_start
                    cur_block_kind = (
                        "list" if re.match(r"^\s*([-*+]|\d+\.)\s+", line) else "prose"
                    )

        pos = line_end
        i += 1

    flush_block(pos)
    return spans


def _force_hard_split(
    body: str,
    span: LocatorSpan,
    max_chars: int,
    context_prefix: str | None = None,
) -> list[LocatorSpan]:
    """Force split any span strictly into pieces <= max_chars without gaps in non-whitespace characters."""
    total_len = span.end_offset - span.start_offset
    if total_len <= max_chars:
        return [span]

    sub_spans: list[LocatorSpan] = []
    offset = span.start_offset
    prefix = context_prefix if context_prefix is not None else span.context_prefix

    while offset < span.end_offset:
        target_end = min(offset + max_chars, span.end_offset)
        # Attempt word break if not at the very end and feasible
        if target_end < span.end_offset:
            # Look for whitespace or boundary to break cleanly
            break_pt = body.rfind(" ", offset, target_end)
            if break_pt > offset + (max_chars // 2):
                target_end = break_pt + 1

        sub_spans.append(
            dataclasses.replace(
                span,
                start_offset=offset,
                end_offset=target_end,
                context_prefix=prefix,
                extra={**span.extra, "exact_text": body[offset:target_end]},
            )
        )
        offset = target_end

    return sub_spans


def _split_long_span(
    body: str, span: LocatorSpan, *, target_chars: int, max_chars: int
) -> list[LocatorSpan]:
    """Split a single span exceeding max_chars into valid sub-spans adhering to KB v3 contracts."""
    span_text = body[span.start_offset : span.end_offset]
    if len(span_text) <= max_chars:
        return [span]

    sub_spans: list[LocatorSpan] = []

    if span.chunk_kind == "table":
        # Split table by rows preserving headers in context_prefix and covering all rows
        lines = span_text.splitlines(keepends=True)
        header_lines: list[str] = []
        for idx, l in enumerate(lines):
            if idx < 2 and ("---" in l or idx == 0):
                header_lines.append(l.strip())

        header_prefix = "\n".join(header_lines).strip()
        rel_pos = 0
        cur_start: int | None = None
        cur_end: int = 0
        cur_len = 0
        chunk_idx = 0

        for l in lines:
            l_len = len(l)
            l_start = rel_pos
            l_end = rel_pos + l_len
            rel_pos = l_end

            # If a single row exceeds max_chars, flush current and hard split the row
            if l_len > max_chars:
                if cur_start is not None and cur_end > cur_start:
                    s = span.start_offset + cur_start
                    e = span.start_offset + cur_end
                    prefix = header_prefix if chunk_idx > 0 else span.context_prefix
                    sub_spans.append(
                        dataclasses.replace(
                            span,
                            start_offset=s,
                            end_offset=e,
                            context_prefix=prefix,
                            extra={**span.extra, "exact_text": body[s:e]},
                        )
                    )
                    chunk_idx += 1
                    cur_start = None
                    cur_len = 0

                row_span = dataclasses.replace(
                    span,
                    start_offset=span.start_offset + l_start,
                    end_offset=span.start_offset + l_end,
                )
                prefix = header_prefix if chunk_idx > 0 else span.context_prefix
                sub_spans.extend(
                    _force_hard_split(
                        body, row_span, max_chars=max_chars, context_prefix=prefix
                    )
                )
                chunk_idx += 1
                continue

            if cur_start is None:
                cur_start = l_start
                cur_end = l_end
                cur_len = l_len
            elif cur_len + l_len <= max_chars:
                cur_end = l_end
                cur_len += l_len
            else:
                s = span.start_offset + cur_start
                e = span.start_offset + cur_end
                prefix = header_prefix if chunk_idx > 0 else span.context_prefix
                sub_spans.append(
                    dataclasses.replace(
                        span,
                        start_offset=s,
                        end_offset=e,
                        context_prefix=prefix,
                        extra={**span.extra, "exact_text": body[s:e]},
                    )
                )
                chunk_idx += 1
                cur_start = l_start
                cur_end = l_end
                cur_len = l_len

        if cur_start is not None and cur_end > cur_start:
            s = span.start_offset + cur_start
            e = span.start_offset + cur_end
            prefix = header_prefix if chunk_idx > 0 else span.context_prefix
            sub_spans.append(
                dataclasses.replace(
                    span,
                    start_offset=s,
                    end_offset=e,
                    context_prefix=prefix,
                    extra={**span.extra, "exact_text": body[s:e]},
                )
            )

    elif span.chunk_kind == "code":
        # Split code by lines or hard characters, providing fence language in context_prefix
        lines = span_text.splitlines(keepends=True)
        fence_prefix = span.context_prefix
        if not fence_prefix and lines:
            first_line = lines[0].strip()
            if first_line.startswith(("```", "~~~")):
                fence_prefix = first_line

        rel_pos = 0
        cur_start: int | None = None
        cur_end: int = 0
        cur_len = 0

        for l in lines:
            l_len = len(l)
            l_start = rel_pos
            l_end = rel_pos + l_len
            rel_pos = l_end

            if l_len > max_chars:
                if cur_start is not None and cur_end > cur_start:
                    s = span.start_offset + cur_start
                    e = span.start_offset + cur_end
                    sub_spans.append(
                        dataclasses.replace(
                            span,
                            start_offset=s,
                            end_offset=e,
                            context_prefix=fence_prefix,
                            extra={**span.extra, "exact_text": body[s:e]},
                        )
                    )
                    cur_start = None
                    cur_len = 0

                row_span = dataclasses.replace(
                    span,
                    start_offset=span.start_offset + l_start,
                    end_offset=span.start_offset + l_end,
                )
                sub_spans.extend(
                    _force_hard_split(
                        body, row_span, max_chars=max_chars, context_prefix=fence_prefix
                    )
                )
                continue

            if cur_start is None:
                cur_start = l_start
                cur_end = l_end
                cur_len = l_len
            elif cur_len + l_len <= max_chars:
                cur_end = l_end
                cur_len += l_len
            else:
                s = span.start_offset + cur_start
                e = span.start_offset + cur_end
                sub_spans.append(
                    dataclasses.replace(
                        span,
                        start_offset=s,
                        end_offset=e,
                        context_prefix=fence_prefix,
                        extra={**span.extra, "exact_text": body[s:e]},
                    )
                )
                cur_start = l_start
                cur_end = l_end
                cur_len = l_len

        if cur_start is not None and cur_end > cur_start:
            s = span.start_offset + cur_start
            e = span.start_offset + cur_end
            sub_spans.append(
                dataclasses.replace(
                    span,
                    start_offset=s,
                    end_offset=e,
                    context_prefix=fence_prefix,
                    extra={**span.extra, "exact_text": body[s:e]},
                )
            )

    else:
        # Generic text splitting by sentences or paragraphs
        pattern = re.compile(r"(\.\s+|\.\n|\!\s+|\?\s+|다\.\s+|요\.\s+|\n\n+)")
        pieces: list[tuple[int, int]] = []
        last_idx = 0
        for m in pattern.finditer(span_text):
            m_end = m.end()
            pieces.append((last_idx, m_end))
            last_idx = m_end
        if last_idx < len(span_text):
            pieces.append((last_idx, len(span_text)))

        cur_start = None
        cur_end = 0

        for p_start, p_end in pieces:
            piece_len = p_end - p_start
            if cur_start is None:
                cur_start = p_start
                cur_end = p_end
            elif (cur_end - cur_start) + piece_len <= max_chars:
                cur_end = p_end
            else:
                s = span.start_offset + cur_start
                e = span.start_offset + cur_end
                raw = body[s:e]
                if raw.strip():
                    sub_spans.append(
                        dataclasses.replace(
                            span,
                            start_offset=s,
                            end_offset=e,
                            extra={**span.extra, "exact_text": body[s:e]},
                        )
                    )
                cur_start = p_start
                cur_end = p_end

        if cur_start is not None and cur_end > cur_start:
            s = span.start_offset + cur_start
            e = span.start_offset + cur_end
            raw = body[s:e]
            if raw.strip():
                sub_spans.append(
                    dataclasses.replace(
                        span,
                        start_offset=s,
                        end_offset=e,
                        extra={**span.extra, "exact_text": body[s:e]},
                    )
                )

    # Final guarantee: ensure strictly <= max_chars for all sub-spans
    guaranteed: list[LocatorSpan] = []
    for sp in sub_spans:
        if (sp.end_offset - sp.start_offset) > max_chars:
            guaranteed.extend(_force_hard_split(body, sp, max_chars=max_chars))
        else:
            guaranteed.append(sp)

    return (
        guaranteed if guaranteed else _force_hard_split(body, span, max_chars=max_chars)
    )


def chunk_structured_document(
    body: str,
    locator_spans: list[LocatorSpan] | None = None,
    *,
    target_chars: int = 750,
    max_chars: int = 1200,
    min_chars: int = 180,
) -> list[StructuredChunk]:
    """Chunk document body adhering to ledger invariants: exact offsets, SHA-256, and structure."""
    if not body:
        return []

    # If no locator_spans provided, discover markdown structure
    if not locator_spans:
        base_spans = parse_markdown_spans(body)
        if not base_spans:
            # Fallback for plain unformatted string
            raw = body.strip()
            if not raw:
                return []
            s = body.find(raw)
            base_spans = [LocatorSpan(start_offset=s, end_offset=s + len(raw))]
    else:
        # Sort spans by start_offset
        base_spans = sorted(locator_spans, key=lambda s: (s.start_offset, s.end_offset))

    # Split spans exceeding max_chars
    normalized_spans: list[LocatorSpan] = []
    for sp in base_spans:
        if (sp.end_offset - sp.start_offset) > max_chars:
            normalized_spans.extend(
                _split_long_span(
                    body, sp, target_chars=target_chars, max_chars=max_chars
                )
            )
        else:
            # Trim whitespace offsets while preserving exact slice
            raw = body[sp.start_offset : sp.end_offset]
            if raw.strip():
                lead = len(raw) - len(raw.lstrip())
                trail_ws = len(raw) - len(raw.rstrip())
                s = sp.start_offset + lead
                e = sp.end_offset - trail_ws
                if e > s:
                    normalized_spans.append(
                        dataclasses.replace(sp, start_offset=s, end_offset=e)
                    )

    if not normalized_spans:
        return []

    # Merge adjacent short spans that share context
    merged_spans: list[LocatorSpan] = []
    cur_span: LocatorSpan | None = None

    for sp in normalized_spans:
        if cur_span is None:
            cur_span = sp
            continue

        cur_len = cur_span.end_offset - cur_span.start_offset

        # Check if merge is eligible
        can_merge = (
            cur_len < min_chars
            and cur_span.section_path == sp.section_path
            and cur_span.page_no == sp.page_no
            and cur_span.slide_no == sp.slide_no
            and (
                cur_span.chunk_kind == sp.chunk_kind
                or (cur_span.chunk_kind == "heading" and sp.chunk_kind == "prose")
            )
            and cur_span.chunk_kind not in ("code", "table")
            and sp.chunk_kind not in ("code", "table")
            and (sp.end_offset - cur_span.start_offset) <= max_chars
            and body[cur_span.end_offset : sp.start_offset].strip() == ""
        )

        if can_merge:
            cur_span = dataclasses.replace(
                cur_span,
                end_offset=sp.end_offset,
                chunk_kind=sp.chunk_kind,
                time_end_ms=sp.time_end_ms or cur_span.time_end_ms,
            )
        else:
            merged_spans.append(cur_span)
            cur_span = sp

    if cur_span is not None:
        merged_spans.append(cur_span)

    # Final hard limit enforcement ensuring strictly <= max_chars for every chunk
    final_spans: list[LocatorSpan] = []
    for sp in merged_spans:
        if (sp.end_offset - sp.start_offset) > max_chars:
            final_spans.extend(_force_hard_split(body, sp, max_chars=max_chars))
        else:
            final_spans.append(sp)

    # Convert to StructuredChunk and compute invariants
    chunks: list[StructuredChunk] = []
    for idx, sp in enumerate(final_spans):
        chunk_body = body[sp.start_offset : sp.end_offset]
        digest = hashlib.sha256(
            chunk_body.encode("utf-8", errors="replace")
        ).hexdigest()
        tokens = _estimate_tokens(chunk_body)
        search_txt = _build_search_text(chunk_body, sp.section_path, sp.context_prefix)

        chunk = StructuredChunk(
            ord=idx,
            char_start=sp.start_offset,
            char_end=sp.end_offset,
            body=chunk_body,
            sha256=digest,
            section_path=list(sp.section_path),
            chunk_kind=sp.chunk_kind,
            page_no=sp.page_no,
            slide_no=sp.slide_no,
            time_start_ms=sp.time_start_ms,
            time_end_ms=sp.time_end_ms,
            token_estimate=tokens,
            search_text=search_txt,
            context_prefix=sp.context_prefix,
            extra=dict(sp.extra),
        )
        chunks.append(chunk)

    return chunks
