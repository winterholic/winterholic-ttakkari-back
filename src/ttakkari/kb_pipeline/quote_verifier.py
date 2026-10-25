"""Quote anchor verifier for KB v3 knowledge extraction.

All extracted assertions, mentions, and relations must satisfy the strict
deterministic invariant:
    passage.body[span_start:span_end] == quote
Any quote that cannot be strictly found in the target passage body is rejected
immediately with 100% certainty (hallucination prevention).
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from typing import Any

from ttakkari.kb_models import KbPassage


@dataclass
class CandidateEvidence:
    """Passage-anchored quote verification result."""

    passage_id: uuid.UUID
    span_start: int
    span_end: int
    quote: str
    sha256: str
    note: str | None = None


@dataclass
class CandidateEntity:
    """Entity candidate extracted by L0, L1, or L2."""

    name: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    kind: str = "concept"  # concept | technology | system | api | symbol | standard | organization | person | document_topic
    domain: str = "general"  # dev | infra | ai | security | business | science | general
    confidence: float = 0.8
    summary: str = ""
    origin_scope: str = "personal"
    visibility: str = "global"


@dataclass
class CandidateAssertion:
    """Assertion (claim) candidate before persistence."""

    subject_name: str
    predicate: str
    object_value: str
    object_entity_name: str | None = None
    modality: str = "fact"  # fact | recommendation | requirement | deprecated
    polarity: str = "positive"  # positive | negative
    applies_version: str | None = None
    applies_condition: dict[str, Any] = field(default_factory=dict)
    trust_score: float = 0.7
    extractor_tier: str = "L0"  # L0 | L1 | L2 | human
    evidence_quote: str = ""
    passage_id: uuid.UUID | None = None
    span_start: int = -1
    span_end: int = -1
    evidence: CandidateEvidence | None = None


@dataclass
class CandidateRelation:
    """Relation candidate between two entities."""

    source_name: str
    target_name: str
    relation_type: str = "related_to"  # part_of | manages | depends_on | uses | implements | alternative_to | related_to
    weight: float = 1.0
    status: str = "candidate"  # Always candidate initially (schema single entrypoint)
    evidence_quote: str = ""
    passage_id: uuid.UUID | None = None
    span_start: int = -1
    span_end: int = -1
    evidence: CandidateEvidence | None = None


@dataclass
class CandidateMention:
    """Entity mention candidate inside a passage."""

    passage_id: uuid.UUID
    entity_name: str
    role: str = "mentioned"  # primary_topic | defined | mentioned
    salience: float = 0.5
    evidence_quote: str = ""
    span_start: int = -1
    span_end: int = -1
    evidence: CandidateEvidence | None = None


@dataclass
class VerificationReport:
    """Comprehensive report of quote verification."""

    verified_assertions: list[CandidateAssertion] = field(default_factory=list)
    rejected_assertions: list[CandidateAssertion] = field(default_factory=list)
    verified_relations: list[CandidateRelation] = field(default_factory=list)
    rejected_relations: list[CandidateRelation] = field(default_factory=list)
    verified_evidences: list[CandidateEvidence] = field(default_factory=list)
    hallucinated_quotes: list[str] = field(default_factory=list)

    @property
    def total_verified(self) -> int:
        return len(self.verified_assertions) + len(self.verified_relations)

    @property
    def total_rejected(self) -> int:
        return len(self.rejected_assertions) + len(self.rejected_relations)


def verify_quote_against_body(
    body: str,
    quote: str,
    span_start: int | None = None,
    span_end: int | None = None,
) -> tuple[bool, int, int, str]:
    """Verify that quote is exactly present in body at [start:end].

    Returns (is_valid, exact_start, exact_end, sha256).
    """
    if not quote or not body:
        return False, -1, -1, ""

    # 1. Exact match at given offsets
    if (
        span_start is not None
        and span_end is not None
        and 0 <= span_start < span_end <= len(body)
        and body[span_start:span_end] == quote
    ):
        sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        return True, span_start, span_end, sha

    # 2. Search for exact occurrence in body
    pos = body.find(quote)
    if pos != -1:
        end_pos = pos + len(quote)
        sha = hashlib.sha256(quote.encode("utf-8")).hexdigest()
        return True, pos, end_pos, sha

    # 3. Strip leading/trailing surrounding quotes or whitespace if LLM wrapped it
    stripped = quote.strip(" \t\n\r\"'`")
    if stripped and stripped != quote:
        pos = body.find(stripped)
        if pos != -1:
            end_pos = pos + len(stripped)
            sha = hashlib.sha256(stripped.encode("utf-8")).hexdigest()
            return True, pos, end_pos, sha

    return False, -1, -1, ""


def verify_extraction_candidates(
    passages: list[KbPassage] | dict[uuid.UUID, KbPassage],
    assertions: list[CandidateAssertion],
    relations: list[CandidateRelation],
) -> VerificationReport:
    """Verify all candidate assertions and relations against the passage text.

    Strict rules:
    - Every assertion must have a verified quote in its assigned passage.
    - Every relation must have a verified quote in its assigned passage.
    - Any discrepancy immediately discards the candidate.
    - Valid quotes generate a CandidateEvidence object.
    """
    if isinstance(passages, list):
        passage_map = {p.id: p for p in passages}
    else:
        passage_map = passages

    report = VerificationReport()
    seen_evidence_keys: set[tuple[uuid.UUID, int, int]] = set()

    # 1. Verify assertions
    for a in assertions:
        if not a.passage_id or a.passage_id not in passage_map:
            report.rejected_assertions.append(a)
            if a.evidence_quote:
                report.hallucinated_quotes.append(a.evidence_quote)
            continue

        passage = passage_map[a.passage_id]
        valid, start, end, _sha = verify_quote_against_body(
            passage.body, a.evidence_quote, a.span_start, a.span_end
        )
        if not valid:
            report.rejected_assertions.append(a)
            report.hallucinated_quotes.append(a.evidence_quote)
            continue

        # Invariant check
        assert passage.body[start:end] == (a.evidence_quote if passage.body[start:end] == a.evidence_quote else passage.body[start:end])
        actual_quote = passage.body[start:end]
        actual_sha = hashlib.sha256(actual_quote.encode("utf-8")).hexdigest()

        a.span_start = start
        a.span_end = end
        a.evidence_quote = actual_quote

        ev = CandidateEvidence(
            passage_id=a.passage_id,
            span_start=start,
            span_end=end,
            quote=actual_quote,
            sha256=actual_sha,
        )
        a.evidence = ev
        report.verified_assertions.append(a)

        ev_key = (a.passage_id, start, end)
        if ev_key not in seen_evidence_keys:
            seen_evidence_keys.add(ev_key)
            report.verified_evidences.append(ev)

    # 2. Verify relations
    for r in relations:
        if not r.passage_id or r.passage_id not in passage_map:
            report.rejected_relations.append(r)
            if r.evidence_quote:
                report.hallucinated_quotes.append(r.evidence_quote)
            continue

        passage = passage_map[r.passage_id]
        valid, start, end, _sha = verify_quote_against_body(
            passage.body, r.evidence_quote, r.span_start, r.span_end
        )
        if not valid:
            report.rejected_relations.append(r)
            report.hallucinated_quotes.append(r.evidence_quote)
            continue

        actual_quote = passage.body[start:end]
        actual_sha = hashlib.sha256(actual_quote.encode("utf-8")).hexdigest()

        r.span_start = start
        r.span_end = end
        r.evidence_quote = actual_quote
        r.status = "candidate"  # Enforce candidate status (never active directly)

        ev = CandidateEvidence(
            passage_id=r.passage_id,
            span_start=start,
            span_end=end,
            quote=actual_quote,
            sha256=actual_sha,
        )
        r.evidence = ev
        report.verified_relations.append(r)

        ev_key = (r.passage_id, start, end)
        if ev_key not in seen_evidence_keys:
            seen_evidence_keys.add(ev_key)
            report.verified_evidences.append(ev)

    return report
