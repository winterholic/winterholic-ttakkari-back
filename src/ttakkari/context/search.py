"""Bounded lexical retrieval for Korean prose and mixed-language project identifiers."""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy import case, func, literal_column, or_

STOPWORDS = {"프로젝트", "관련", "알려줘", "찾아줘", "어디", "무엇", "어떻게", "대한", "관한", "the", "is", "a", "what"}


def terms(query: str) -> list[str]:
    words = re.findall(r"[\w]+(?:[-.][\w]+)*", unicodedata.normalize("NFKC", query).casefold())
    return list(dict.fromkeys(w for w in words if w not in STOPWORDS and len(w) > 1))[:16]


def lexical(column, query: str, words: list[str]):
    # 'simple' keeps Korean tokens intact; partial matches cover particles and compound nouns.
    config = literal_column("'simple'")
    tsquery = func.plainto_tsquery(config, " ".join(words))
    vector = func.to_tsvector(config, column)
    fts = vector.op("@@")(tsquery)
    matches = [column.icontains(w, autoescape=True) for w in words]
    coverage = sum((case((m, 1.0), else_=0.0) for m in matches), 0) / max(len(words), 1)
    fuzzy = func.word_similarity(query, func.coalesce(column, ""))
    predicate = or_(fts, *matches, fuzzy >= 0.6)
    score = coverage + func.ts_rank_cd(vector, tsquery) + 0.15 * fuzzy
    return predicate, score


def scope_clause(column, scope: str):
    return column.in_((scope, "shared")) if scope != "all" else column.is_not(None)
