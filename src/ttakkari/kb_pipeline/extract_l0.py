"""L0 deterministic rule-based extractor (cost 0, latency < 20ms/doc).

Extracts concept definitions, bilingual aliases, heading hierarchy relationships,
code symbols, and explicit version/date assertions using deterministic rules and
common lexical/entity normalization helpers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ttakkari.common.entity_norm import (
    extract_explicit_definitions,
    norm_name,
    split_bilingual_label,
)
from ttakkari.kb_models import KbPassage
from ttakkari.kb_pipeline.quote_verifier import (
    CandidateAssertion,
    CandidateEntity,
    CandidateMention,
    CandidateRelation,
)

# Inline code symbol pattern: `identifier` or `cli command`
INLINE_SYMBOL_PATTERN = re.compile(r"`([^`\n]{2,80})`")

# Version pattern: v1.31, 2.0.1, >=1.20, v0.72.0
VERSION_PATTERN = re.compile(
    r"\b(?:v\d+(?:\.\d+)+(?:-[a-zA-Z0-9.]+)?|버전\s*\d+(?:\.\d+)*|(?:\>=|\<=|==)\s*\d+(?:\.\d+)*)\b"
)

# Date pattern: YYYY-MM-DD
DATE_PATTERN = re.compile(r"\b(20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))\b")

# CLI command prefix heuristics
CLI_COMMANDS = {"kubectl", "docker", "git", "helm", "cargo", "npm", "uv", "pip", "python", "pnpm", "make"}


@dataclass
class L0ExtractionResult:
    """Artifacts extracted deterministically by L0."""

    entities: list[CandidateEntity] = field(default_factory=list)
    assertions: list[CandidateAssertion] = field(default_factory=list)
    relations: list[CandidateRelation] = field(default_factory=list)
    mentions: list[CandidateMention] = field(default_factory=list)


def extract_l0_from_passages(
    passages: list[KbPassage],
    *,
    origin_scope: str = "personal",
    visibility: str = "global",
) -> L0ExtractionResult:
    """Run L0 deterministic extraction on a list of passages."""
    result = L0ExtractionResult()
    seen_entities: dict[str, CandidateEntity] = {}

    def _register_entity(
        raw_name: str,
        kind: str = "concept",
        domain: str = "dev",
        summary: str = "",
        extra_aliases: list[str] | None = None,
    ) -> CandidateEntity:
        canonical, aliases = split_bilingual_label(raw_name)
        if extra_aliases:
            for a in extra_aliases:
                if a and a not in aliases:
                    aliases.append(a)

        key = (norm_name(canonical), kind)
        if key in seen_entities:
            existing = seen_entities[key]
            for a in aliases:
                if a not in existing.aliases:
                    existing.aliases.append(a)
            return existing

        entity = CandidateEntity(
            name=canonical,
            canonical_name=canonical,
            aliases=aliases,
            kind=kind,
            domain=domain,
            summary=summary,
            origin_scope=origin_scope,
            visibility=visibility,
            confidence=0.9,
        )
        seen_entities[key] = entity
        result.entities.append(entity)
        return entity

    # Iterate through all passages
    for p in passages:
        body = p.body or ""
        if not body.strip():
            continue

        # 1. Explicit definitions (Korean, English, table rows)
        defs = extract_explicit_definitions(body)
        for d in defs:
            term = d["term"]
            aliases = d.get("aliases", [])
            definition = d["definition"]
            quote = d["quote"]
            start = d["offset_start"]
            end = d["offset_end"]

            # Fix quote offsets if needed
            if body[start:end] != quote:
                idx = body.find(quote)
                if idx != -1:
                    start, end = idx, idx + len(quote)
                else:
                    # Strip markers
                    clean_q = quote.strip()
                    idx = body.find(clean_q)
                    if idx != -1:
                        start, end = idx, idx + len(clean_q)
                        quote = clean_q

            if idx := body.find(quote):
                pass

            entity = _register_entity(
                term,
                kind="concept",
                domain="dev",
                summary=definition,
                extra_aliases=aliases,
            )

            # Mention as defined
            result.mentions.append(
                CandidateMention(
                    passage_id=p.id,
                    entity_name=entity.canonical_name,
                    role="defined",
                    salience=1.0,
                    evidence_quote=quote,
                    span_start=start,
                    span_end=end,
                )
            )

            # Definition assertion
            predicate = d.get("predicate", "definition")
            if predicate not in ("refers to", "is defined as", "means", "definition"):
                predicate = "definition"

            # Check if version exists in definition
            v_match = VERSION_PATTERN.search(quote)
            version_str = v_match.group(0) if v_match else None

            result.assertions.append(
                CandidateAssertion(
                    subject_name=entity.canonical_name,
                    predicate=predicate,
                    object_value=definition,
                    modality="fact",
                    polarity="positive",
                    applies_version=version_str,
                    trust_score=0.95,
                    extractor_tier="L0",
                    evidence_quote=quote,
                    passage_id=p.id,
                    span_start=start,
                    span_end=end,
                )
            )

        # 2. Heading hierarchy relationships (section_path)
        if p.section_path and len(p.section_path) >= 2:
            # Pairwise part_of relations along the path: path[i] part_of path[i-1]
            for i in range(1, len(p.section_path)):
                parent = p.section_path[i - 1].strip()
                child = p.section_path[i].strip()
                if not parent or not child or parent == child:
                    continue

                parent_ent = _register_entity(parent, kind="concept")
                child_ent = _register_entity(child, kind="concept")

                # Locate evidence in body: child or heading
                evidence_quote = ""
                span_start, span_end = -1, -1
                if p.heading and p.heading in body:
                    pos = body.find(p.heading)
                    evidence_quote = p.heading
                    span_start, span_end = pos, pos + len(p.heading)
                elif child in body:
                    pos = body.find(child)
                    evidence_quote = child
                    span_start, span_end = pos, pos + len(child)
                else:
                    # Use the first line of passage as quote anchor if it exists
                    first_line = body.split("\n", 1)[0].strip()
                    if first_line and first_line in body:
                        pos = body.find(first_line)
                        evidence_quote = first_line
                        span_start, span_end = pos, pos + len(first_line)

                if evidence_quote and span_start >= 0:
                    result.relations.append(
                        CandidateRelation(
                            source_name=child_ent.canonical_name,
                            target_name=parent_ent.canonical_name,
                            relation_type="part_of",
                            weight=1.0,
                            status="candidate",
                            evidence_quote=evidence_quote,
                            passage_id=p.id,
                            span_start=span_start,
                            span_end=span_end,
                        )
                    )

        # 3. Code symbols and CLI commands (`symbol`)
        for m in INLINE_SYMBOL_PATTERN.finditer(body):
            sym = m.group(1).strip()
            # Ignore purely numeric or single char symbols
            if len(sym) < 2 or sym.isdigit():
                continue

            kind = "api" if "(" in sym or "()" in sym else "symbol"
            domain = "dev"

            # Clean symbol name for canonical representation
            clean_sym = sym.rstrip("()")
            ent = _register_entity(clean_sym, kind=kind, domain=domain)

            q = m.group(0)
            q_start, q_end = m.start(), m.end()

            result.mentions.append(
                CandidateMention(
                    passage_id=p.id,
                    entity_name=ent.canonical_name,
                    role="mentioned",
                    salience=0.5,
                    evidence_quote=q,
                    span_start=q_start,
                    span_end=q_end,
                )
            )

        # 4. Explicit version assertions
        for m in VERSION_PATTERN.finditer(body):
            v_str = m.group(0).strip()
            # Find surrounding sentence
            start_line = max(0, body.rfind("\n", 0, m.start()) + 1)
            end_line = body.find("\n", m.end())
            if end_line == -1:
                end_line = len(body)
            sentence = body[start_line:end_line].strip()
            if len(sentence) >= 10:
                # If passage has heading or topic, associate with it
                subj = p.heading or (p.section_path[-1] if p.section_path else None)
                if subj:
                    canonical_subj, _ = split_bilingual_label(subj)
                    ent = _register_entity(canonical_subj)
                    q_start = body.find(sentence)
                    if q_start != -1:
                        result.assertions.append(
                            CandidateAssertion(
                                subject_name=ent.canonical_name,
                                predicate="version",
                                object_value=v_str,
                                modality="fact",
                                polarity="positive",
                                applies_version=v_str,
                                trust_score=0.85,
                                extractor_tier="L0",
                                evidence_quote=sentence,
                                passage_id=p.id,
                                span_start=q_start,
                                span_end=q_start + len(sentence),
                            )
                        )

    return result
