"""Shared text, lexical, chunking, and entity normalization utilities for Ttakkari."""

from __future__ import annotations

from ttakkari.common.chunking import (
    LocatorSpan,
    StructuredChunk,
    chunk_structured_document,
)
from ttakkari.common.entity_norm import (
    cosine_similarity,
    duplicate_suspects,
    extract_explicit_definitions,
    jaro_winkler_similarity,
    norm_name,
    split_bilingual_label,
)
from ttakkari.common.lexical import (
    lexical_score,
    split_identifier,
    strip_korean_suffix,
    terms,
    tokenize,
)

__all__ = [
    "LocatorSpan",
    "StructuredChunk",
    "chunk_structured_document",
    "cosine_similarity",
    "duplicate_suspects",
    "extract_explicit_definitions",
    "jaro_winkler_similarity",
    "lexical_score",
    "norm_name",
    "split_bilingual_label",
    "split_identifier",
    "strip_korean_suffix",
    "terms",
    "tokenize",
]
