"""L1 seed dictionary matching and key passage selector (cost 0~low).

Scans passage text against known entity/alias dictionaries and scores passages
to select top critical passages for L2 LLM deep extraction under doc/daily budgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ttakkari.common.entity_norm import norm_name
from ttakkari.common.lexical import strip_korean_suffix, tokenize
from ttakkari.kb_models import KbPassage
from ttakkari.kb_pipeline.quote_verifier import CandidateMention

# Key qualification/constraint/comparison terms for L2 selection
IMPORTANCE_KEYWORDS = {
    # Korean
    "반드시",
    "권장",
    "deprecated",
    "지원하지",
    "지원하지않",
    "버전",
    "기본값",
    "필수",
    "제약",
    "주의",
    "차이",
    "비교",
    "구조",
    "아키텍처",
    "설정",
    # English
    "must",
    "required",
    "should",
    "recommended",
    "unsupported",
    "default",
    "limitation",
    "architecture",
    "difference",
    "versus",
    "vs",
}

OVERVIEW_HEADINGS = {
    "개요",
    "소개",
    "아키텍처",
    "요약",
    "overview",
    "introduction",
    "architecture",
    "summary",
    "background",
}


@dataclass
class ScoredPassage:
    passage: KbPassage
    score: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class L1ExtractionResult:
    """Artifacts produced by L1 matching and screening."""

    selected_passages: list[KbPassage] = field(default_factory=list)
    scored_passages: list[ScoredPassage] = field(default_factory=list)
    mentions: list[CandidateMention] = field(default_factory=list)
    budget_exhausted: bool = False
    budget_remaining: int | None = None


def score_passage_for_l2(passage: KbPassage, *, is_first: bool = False) -> tuple[float, list[str]]:
    """Score a passage based on definition/constraint keywords, code density, and heading."""
    score = 0.0
    reasons: list[str] = []
    body = passage.body or ""

    # 1. Overview / Introduction heading or first passage
    if is_first or passage.ord == 0:
        score += 5.0
        reasons.append("first_passage")

    if passage.heading:
        h_norm = norm_name(passage.heading)
        if any(kw in h_norm for kw in OVERVIEW_HEADINGS):
            score += 4.0
            reasons.append("overview_heading")

    if passage.section_path:
        for sec in passage.section_path:
            s_norm = norm_name(sec)
            if any(kw in s_norm for kw in OVERVIEW_HEADINGS):
                score += 3.0
                reasons.append(f"overview_section({sec})")
                break

    # 2. Importance and constraint keywords
    kw_hits = 0
    tokens = tokenize(body)
    for tok in tokens:
        stripped = strip_korean_suffix(tok)
        if stripped in IMPORTANCE_KEYWORDS or tok in IMPORTANCE_KEYWORDS:
            kw_hits += 1

    if kw_hits > 0:
        added = min(15.0, kw_hits * 3.0)
        score += added
        reasons.append(f"keywords_hit({kw_hits})")

    # 3. Code symbol and backtick density
    backticks = body.count("`") // 2
    if backticks >= 2:
        added = min(6.0, backticks * 1.5)
        score += added
        reasons.append(f"code_symbols({backticks})")

    # 4. Table chunk kind
    if passage.chunk_kind == "table" or "| ---" in body:
        score += 4.0
        reasons.append("structured_table")

    return score, reasons


def match_known_entities(
    passages: list[KbPassage],
    known_aliases: dict[str, str],  # norm_alias -> canonical_name
) -> list[CandidateMention]:
    """Scan passages for known entity aliases and create mentions."""
    if not known_aliases:
        return []

    mentions: list[CandidateMention] = []
    for p in passages:
        body = p.body or ""
        if not body.strip():
            continue

        tokens = tokenize(body)
        seen_in_passage: set[str] = set()

        for tok in tokens:
            stripped = strip_korean_suffix(tok)
            # Try both stripped and raw token
            candidates = [norm_name(stripped), norm_name(tok)]
            for c_norm in candidates:
                if c_norm and c_norm in known_aliases:
                    canonical = known_aliases[c_norm]
                    if canonical in seen_in_passage:
                        continue
                    seen_in_passage.add(canonical)

                    # Find position in passage body
                    pos = body.find(tok)
                    if pos == -1:
                        pos = body.find(stripped)

                    start, end = -1, -1
                    q = ""
                    if pos != -1:
                        matched_str = tok if body.find(tok) != -1 else stripped
                        start, end = pos, pos + len(matched_str)
                        q = body[start:end]

                    mentions.append(
                        CandidateMention(
                            passage_id=p.id,
                            entity_name=canonical,
                            role="mentioned",
                            salience=0.6,
                            evidence_quote=q,
                            span_start=start,
                            span_end=end,
                        )
                    )
    return mentions


def extract_l1_screen(
    passages: list[KbPassage],
    *,
    known_aliases: dict[str, str] | None = None,
    max_calls_per_doc: int = 8,
    daily_budget_remaining: int | None = None,
) -> L1ExtractionResult:
    """Run L1 dictionary matching and select top passages for L2 extraction."""
    # 1. Dictionary matching
    mentions = match_known_entities(passages, known_aliases or {})

    # 2. Check budget
    if daily_budget_remaining is not None and daily_budget_remaining <= 0:
        return L1ExtractionResult(
            selected_passages=[],
            scored_passages=[],
            mentions=mentions,
            budget_exhausted=True,
            budget_remaining=0,
        )

    # 3. Score all passages
    scored: list[ScoredPassage] = []
    for i, p in enumerate(passages):
        score, reasons = score_passage_for_l2(p, is_first=(i == 0))
        scored.append(ScoredPassage(passage=p, score=score, reasons=reasons))

    # Sort descending by score, tie-break by lower passage.ord
    scored.sort(key=lambda sp: (-sp.score, sp.passage.ord))

    # Determine selection limit
    limit = max_calls_per_doc
    if daily_budget_remaining is not None:
        limit = min(limit, daily_budget_remaining)

    selected = [sp.passage for sp in scored[:limit] if sp.score > 0.0]

    # If all scores were 0 (e.g. flat plain text), pick the first few up to limit
    if not selected and passages and limit > 0:
        selected = passages[:limit]

    return L1ExtractionResult(
        selected_passages=selected,
        scored_passages=scored,
        mentions=mentions,
        budget_exhausted=False,
        budget_remaining=daily_budget_remaining - len(selected) if daily_budget_remaining is not None else None,
    )
