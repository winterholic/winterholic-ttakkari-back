"""Lexical processing, tokenization, identifier decomposition, and Korean suffix stripping."""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

STOPWORDS = {
    # Korean query particles, interrogatives, generic verbs
    "프로젝트",
    "관련",
    "알려줘",
    "찾아줘",
    "어디",
    "무엇",
    "어떻게",
    "대한",
    "관한",
    "통해",
    "대해",
    "위해",
    "에서",
    "으로",
    "있는",
    "없는",
    "하는",
    "되는",
    "하다",
    "한다",
    "이다",
    "있다",
    "없다",
    # English stopwords
    "the",
    "is",
    "are",
    "was",
    "were",
    "a",
    "an",
    "in",
    "on",
    "of",
    "for",
    "to",
    "and",
    "or",
    "with",
    "what",
    "how",
    "why",
    "which",
}

# Sorted by length descending for greedy suffix match
KOREAN_SUFFIXES = [
    # 4+ chars
    "에서부터",
    "에게서는",
    # 3 chars
    "에서는",
    "으로는",
    "에게서",
    "으로서",
    "으로써",
    "까지는",
    "부터는",
    "이라는",
    "한다는",
    "된다는",
    "하기에",
    "하면서",
    "하므로",
    "하도록",
    "하지만",
    "하였고",
    "되었고",
    "하는데",
    "되는데",
    # 2 chars
    "에게",
    "에서",
    "으로",
    "보다",
    "처럼",
    "같이",
    "마다",
    "까지",
    "부터",
    "마저",
    "조차",
    "하다",
    "한다",
    "이다",
    "된다",
    "하는",
    "되는",
    "있는",
    "없는",
    "하여",
    "되어",
    "하며",
    "되며",
    # 1 char
    "은",
    "는",
    "이",
    "가",
    "을",
    "를",
    "의",
    "에",
    "로",
    "과",
    "와",
    "도",
    "만",
]


def strip_korean_suffix(word: str) -> str:
    """Strip common Korean grammatical particles/endings while preserving root >= 2 chars."""
    if len(word) < 3:
        return word

    for suffix in KOREAN_SUFFIXES:
        if word.endswith(suffix):
            root = word[: -len(suffix)]
            # Preserve roots that have at least 2 characters if Korean
            if len(root) >= 2 or (
                len(root) >= 1 and not (0xAC00 <= ord(root[0]) <= 0xD7A3)
            ):
                return root
    return word


def split_identifier(identifier: str) -> list[str]:
    """Decompose camelCase, PascalCase, snake_case, kebab-case and dotted symbols."""
    # Replace separators with space
    s = re.sub(r"[\-_.·/\\:]+", " ", identifier)
    # Split camelCase / PascalCase
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", s)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1 \2", s)
    # Split word from number only if word is 2+ letters (e.g. endpoint2 -> endpoint 2, but v2 remains v2)
    s = re.sub(r"([a-zA-Z]{2,})(\d+)", r"\1 \2", s)
    s = re.sub(r"(\d+)([a-zA-Z]{2,})", r"\1 \2", s)

    words = s.lower().split()
    return [w for w in words if w]


def tokenize(
    text: str,
    *,
    preserve_case: bool = False,
    strip_particles: bool = True,
) -> list[str]:
    """Tokenize text into search and indexing terms with NFKC normalization and code symbol expansion."""
    if not text:
        return []

    normalized = unicodedata.normalize("NFKC", text)

    raw_words = re.findall(r"[\w]+(?:[-.][\w]+)*", normalized)
    tokens: list[str] = []

    for w in raw_words:
        # Korean suffix stripping before casefold to preserve CamelCase roots
        stripped = strip_korean_suffix(w) if strip_particles else w

        # Extract code identifier subwords (preserves CamelCase from raw case)
        for cand in (w, stripped):
            sub_words = split_identifier(cand)
            if len(sub_words) > 1:
                tokens.extend(sub_words)

        cand_lower = w if preserve_case else w.casefold()
        stripped_lower = stripped if preserve_case else stripped.casefold()

        if stripped_lower != cand_lower:
            tokens.append(stripped_lower)
        tokens.append(cand_lower)

    # Return distinct non-empty tokens
    seen = set()
    result = []
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            result.append(t)
    return result


def terms(query: str, *, max_terms: int = 16) -> list[str]:
    """Extract distinct high-signal terms from a query string for lexical retrieval."""
    if not query:
        return []

    raw_tokens = tokenize(query, preserve_case=False, strip_particles=True)
    filtered = [
        t for t in raw_tokens if t not in STOPWORDS and len(t) > 1 and not t.isdigit()
    ]
    return list(dict.fromkeys(filtered))[:max_terms]


def lexical_score(query_terms: list[str], text: str) -> float:
    """Compute bounded lexical matching score for query terms against target text."""
    if not query_terms or not text:
        return 0.0

    target_tokens = tokenize(text, preserve_case=False, strip_particles=True)
    if not target_tokens:
        return 0.0

    target_counts = Counter(target_tokens)
    target_len = len(target_tokens)

    matched_weight = 0.0
    for q in query_terms:
        count = target_counts.get(q, 0)
        if count > 0:
            # Term frequency log-dampened
            tf = 1.0 + math.log(count)
            matched_weight += tf
        elif q in text.lower():
            # Substring match fallback for particles or compound nouns
            matched_weight += 0.5

    # Coverage: ratio of distinct query terms matched
    coverage = sum(
        1 for q in query_terms if target_counts.get(q, 0) > 0 or q in text.lower()
    ) / len(query_terms)

    # Length normalization
    norm_len = math.sqrt(target_len + 10)
    score = (matched_weight / norm_len) + (0.5 * coverage)
    return round(score, 4)
