"""Entity name normalization, bilingual label separation, and explicit definition extraction."""

from __future__ import annotations

import difflib
import re
import unicodedata
from typing import Any

# Table headers identifying definition rows
DEFINITION_TABLE_HEADERS = {
    ("용어", "설명"),
    ("용어", "정의"),
    ("항목", "설명"),
    ("항목", "내용"),
    ("term", "definition"),
    ("term", "description"),
    ("name", "description"),
}


def norm_name(name: str) -> str:
    """Normalize entity name for deduplication and dictionary keys."""
    if not name:
        return ""
    normalized = unicodedata.normalize("NFKC", name).casefold()
    return re.sub(r"[\s\-_.·/()[\]{}]+", "", normalized)


def is_mostly_latin(s: str) -> bool:
    """Check if string consists primarily of latin alphabet and digits."""
    cleaned = re.sub(r"[\s\d\-_.]+", "", s)
    if not cleaned:
        return False
    return all(ord(c) < 128 for c in cleaned)


def is_mostly_hangul(s: str) -> bool:
    """Check if string contains Hangul syllables."""
    return any(0xAC00 <= ord(c) <= 0xD7A3 for c in s)


def split_bilingual_label(label: str) -> tuple[str, list[str]]:
    """Split bilingual or alias-annotated label like '디플로이먼트(Deployment)' into canonical and aliases.

    Rules:
    - If label is 'Hangul (English)', prefer the standard English name as canonical_name for technical terms.
    - If label is 'English (Hangul)', English is canonical_name and Hangul is an alias.
    - If label is 'Name (Abbrev)', Name is canonical_name and Abbrev is an alias.
    """
    cleaned = label.strip()
    m = re.match(r"^([^(]+?)\s*\(([^)]+)\)$", cleaned)
    if not m:
        return cleaned, [cleaned] if cleaned else []

    outer = m.group(1).strip()
    inner = m.group(2).strip()

    aliases = [outer, inner]

    # Decide canonical
    if is_mostly_hangul(outer) and is_mostly_latin(inner):
        # e.g., "디플로이먼트(Deployment)" -> canonical: "Deployment"
        canonical = inner
    elif is_mostly_latin(outer) and is_mostly_hangul(inner):
        # e.g., "Kubernetes(쿠버네티스)" -> canonical: "Kubernetes"
        canonical = outer
    else:
        # e.g., "PostgreSQL (Postgres)" -> outer is canonical
        canonical = outer

    # Return distinct non-empty aliases with canonical included
    distinct_aliases: list[str] = []
    for a in aliases:
        if a and a not in distinct_aliases:
            distinct_aliases.append(a)
    if canonical not in distinct_aliases:
        distinct_aliases.insert(0, canonical)

    return canonical, distinct_aliases


def extract_explicit_definitions(text: str) -> list[dict[str, Any]]:
    """Extract only explicit, deterministic definitions and stated aliases without speculative inferences.

    All extracted entries guarantee quote anchor integrity: quote in text.
    """
    if not text:
        return []

    results: list[dict[str, Any]] = []

    # 1. Korean explicit definition pattern:
    # e.g., "디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 쿠버네티스 워크로드 리소스이다."
    ko_def_pattern = re.compile(
        r"(?m)^(?:\s*[-*]\s*)?([A-Za-z0-9가-힣_\-\s()]{2,50}?)\s*(?:은|는|이란|란)\s+([^.\n]+?이다\.)"
    )
    for m in ko_def_pattern.finditer(text):
        term_raw = m.group(1).strip()
        definition = m.group(2).strip()
        quote = m.group(0).strip()
        if quote.startswith(("- ", "* ")):
            quote = quote[2:].strip()

        canonical, aliases = split_bilingual_label(term_raw)
        results.append(
            {
                "term": canonical,
                "raw_term": term_raw,
                "aliases": aliases,
                "definition": definition,
                "quote": quote,
                "pattern": "korean_explicit_is",
                "offset_start": m.start(),
                "offset_end": m.end(),
            }
        )

    # 2. English explicit definition patterns:
    # e.g., "Kubernetes refers to an open-source container orchestration system."
    en_def_pattern = re.compile(
        r"(?m)^(?:\s*[-*]\s*)?([A-Za-z0-9_\-\s()]{2,50})\s+(refers to|is defined as|means)\s+([^.\n]+?\.)"
    )
    for m in en_def_pattern.finditer(text):
        term_raw = m.group(1).strip()
        predicate = m.group(2).strip()
        definition = m.group(3).strip()
        quote = m.group(0).strip()
        if quote.startswith(("- ", "* ")):
            quote = quote[2:].strip()

        canonical, aliases = split_bilingual_label(term_raw)
        results.append(
            {
                "term": canonical,
                "raw_term": term_raw,
                "aliases": aliases,
                "predicate": predicate,
                "definition": definition,
                "quote": quote,
                "pattern": "english_explicit_def",
                "offset_start": m.start(),
                "offset_end": m.end(),
            }
        )

    # 3. Table definition pattern:
    # Look for markdown tables with header matching DEFINITION_TABLE_HEADERS
    lines = text.splitlines(keepends=True)
    pos = 0
    i = 0
    while i < len(lines):
        line = lines[i]
        pos += len(line)

        if (
            line.strip().startswith("|")
            and line.strip().endswith("|")
            and i + 1 < len(lines)
            and "---" in lines[i + 1]
        ):
            headers = [h.strip().lower() for h in line.strip().strip("|").split("|")]
            if len(headers) >= 2:
                h_pair = (headers[0], headers[1])
                if h_pair in DEFINITION_TABLE_HEADERS or any(
                    headers[0].startswith(h0) and headers[1].startswith(h1)
                    for h0, h1 in DEFINITION_TABLE_HEADERS
                ):
                    # Consume table rows
                    row_idx = i + 2
                    cur_row_pos = pos + len(lines[i + 1])
                    while row_idx < len(lines):
                        r_line = lines[row_idx]
                        if not (
                            r_line.strip().startswith("|")
                            and r_line.strip().endswith("|")
                        ):
                            break
                        cols = [c.strip() for c in r_line.strip().strip("|").split("|")]
                        if len(cols) >= 2 and cols[0] and cols[1]:
                            t_raw = cols[0]
                            def_raw = cols[1]
                            quote = r_line.strip()
                            canonical, aliases = split_bilingual_label(t_raw)
                            results.append(
                                {
                                    "term": canonical,
                                    "raw_term": t_raw,
                                    "aliases": aliases,
                                    "definition": def_raw,
                                    "quote": quote,
                                    "pattern": "markdown_table_row",
                                    "offset_start": cur_row_pos,
                                    "offset_end": cur_row_pos + len(r_line),
                                }
                            )
                        cur_row_pos += len(r_line)
                        row_idx += 1
                    i = row_idx
                    pos = cur_row_pos
                    continue
        i += 1

    return results


def duplicate_suspects(
    entities: list[dict[str, Any]], threshold: float = 0.86
) -> list[dict[str, Any]]:
    """Identify duplicate or ambiguous entity candidates based on normalized keys and string similarity."""
    keys = [
        {
            norm_name(x.get("name", "")),
            *(norm_name(a) for a in x.get("aliases", []) if isinstance(a, str)),
        }
        - {""}
        for x in entities
    ]
    out: list[dict[str, Any]] = []

    for i, a in enumerate(entities):
        for j in range(i + 1, len(entities)):
            b = entities[j]
            na, nb = norm_name(a.get("name", "")), norm_name(b.get("name", ""))
            reason, score = None, 0.0

            if na and na == nb:
                reason, score = "identical_norm_name", 1.0
            elif keys[i] & keys[j]:
                reason, score = "shared_alias", 0.95
            else:
                ratio = (
                    difflib.SequenceMatcher(None, na, nb).ratio()
                    if min(len(na), len(nb)) >= 4
                    else 0.0
                )
                if ratio >= threshold:
                    reason, score = "high_similarity", round(ratio, 2)

            if reason:
                out.append(
                    {
                        "a": a.get("id"),
                        "b": b.get("id"),
                        "a_name": a.get("name"),
                        "b_name": b.get("name"),
                        "reason": reason,
                        "score": round(score, 2),
                    }
                )

    return sorted(out, key=lambda d: -d["score"])[:30]
