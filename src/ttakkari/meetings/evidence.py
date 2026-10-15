"""Conservative quote lookup. No paraphrase scores or generated timestamps."""

from __future__ import annotations

import math
import re
import unicodedata

_PREFIX = re.compile(
    r"^\s*\[\d+(?:\.\d+)?\s*[–-]\s*\d+(?:\.\d+)?\]\s*"
    r"화자\s*(?:\d+|미상)(?:\s*\[검증 필요\])?\s*:\s*"
)


def _normalize(text: str) -> str:
    text = " ".join(_PREFIX.sub("", line) for line in text.splitlines())
    text = unicodedata.normalize("NFC", text).casefold()
    # Keep decimals, signs, ratios and percent markers: 1.5 must not match 15.
    return "".join(
        char
        for i, char in enumerate(text)
        if char.isalnum()
        or char in "+-/%"
        or (
            char in ".,"
            and 0 < i < len(text) - 1
            and text[i - 1].isdigit()
            and text[i + 1].isdigit()
        )
    )


def locate_evidence(
    quote: str | None, utterances: list[dict]
) -> tuple[float | None, str | None]:
    """Unique normalized literal match, at most 3 turns/30s, gaps at most 2s.

    Returns the start of the first matched utterance, not a guessed word offset.
    Folded repetition cannot be an acoustic evidence anchor.
    """
    needle = _normalize(quote or "")
    if sum(char.isalnum() for char in needle) < 6:
        return None, None
    chunks, origins = [], []
    previous_end = None
    valid = {}
    for i, row in enumerate(utterances):
        start, end = row.get("start"), row.get("end")
        if (
            not isinstance(start, (int, float))
            or not isinstance(end, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end < start
            or "repetition_folded" in row.get("flags", [])
        ):
            chunks.append("|")
            origins.append(-1)
            previous_end = None
            continue
        piece = _normalize(row.get("text", ""))
        if previous_end is None or start - previous_end > 2:
            chunks.append("|")
            origins.append(-1)
        chunks.append(piece)
        origins.extend([i] * len(piece))
        previous_end = end
        valid[i] = row
    haystack = "".join(chunks)
    matches = set()
    cursor = 0
    while (position := haystack.find(needle, cursor)) >= 0:
        first, last = origins[position], origins[position + len(needle) - 1]
        if first in valid and last in valid and last - first <= 2:
            a, b = valid[first], valid[last]
            if b["end"] - a["start"] <= 30:
                matches.add((a["start"], str(a["id"])))
        cursor = position + 1
    return next(iter(matches)) if len(matches) == 1 else (None, None)
