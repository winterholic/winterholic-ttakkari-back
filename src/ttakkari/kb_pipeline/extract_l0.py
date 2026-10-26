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
from ttakkari.kb_pipeline.entity_filter import (
    MAX_L0_ENTITIES,
    classify_inline_symbol,
    clean_heading,
    is_concept_heading,
    is_definable_term,
)
from ttakkari.kb_pipeline.quote_verifier import (
    CandidateAssertion,
    CandidateEntity,
    CandidateMention,
    CandidateRelation,
)

# 식별자 모양이 약한 인라인 코드가 엔터티로 남으려면 문서 안에서 최소 이만큼 나와야 한다.
WEAK_SYMBOL_MIN_MENTIONS = 4

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
    # 필터 전후 수치(품질 측정용): 필터가 버린 후보 수
    filtered: dict[str, int] = field(default_factory=dict)


def _anchor(body: str, quote: str, hint_start: int, hint_end: int) -> tuple[int, int] | None:
    """정의 규칙이 준 매치 구간 [hint_start, hint_end) 안에서 quote 의 정확한 위치를 찾는다. 구간 밖은 보지 않는다."""
    if not quote:
        return None
    pos = body.find(quote, max(hint_start, 0), hint_end)
    if pos == -1:
        return None
    return pos, pos + len(quote)


def extract_l0_from_passages(
    passages: list[KbPassage],
    *,
    origin_scope: str = "work",
    visibility: str = "work_only",
) -> L0ExtractionResult:
    """Run L0 deterministic extraction on a list of passages (fail-closed scope unless the caller derives it).

    노이즈 필터(entity_filter): 일반·번호 제목, 평범한 단어/경로/명령의 인라인 코드, 저빈도 약한 후보를 버리고
    문서당 MAX_L0_ENTITIES 개로 자른다. 버린 수는 result.filtered 에 남긴다.
    """
    result = L0ExtractionResult()
    seen_entities: dict[tuple[str, str], CandidateEntity] = {}
    defined_keys: set[tuple[str, str]] = set()
    strong_keys: set[tuple[str, str]] = set()
    mention_counts: dict[tuple[str, str], int] = {}
    dropped = {"heading_not_concept": 0, "symbol_not_entity": 0, "low_frequency": 0, "over_cap": 0}

    def _register_entity(
        raw_name: str,
        kind: str = "concept",
        domain: str = "dev",
        summary: str = "",
        extra_aliases: list[str] | None = None,
    ) -> tuple[CandidateEntity, tuple[str, str]]:
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
            if summary and not existing.summary:
                existing.summary = summary
            return existing, key

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
        return entity, key

    for p in passages:
        body = p.body or ""
        if not body.strip():
            continue

        # 1. Explicit definitions (Korean, English, table rows). 정의로 나온 후보는 항상 남긴다.
        for d in extract_explicit_definitions(body):
            term = d["term"]
            aliases = d.get("aliases", [])
            definition = d["definition"]
            quote = d["quote"]
            if not is_definable_term(term):
                dropped["heading_not_concept"] += 1
                continue
            anchor = _anchor(body, quote, d["offset_start"], d["offset_end"])
            if anchor is None:
                continue
            start, end = anchor

            entity, key = _register_entity(
                term,
                kind="concept",
                domain="dev",
                summary=definition,
                extra_aliases=aliases,
            )
            defined_keys.add(key)
            mention_counts[key] = mention_counts.get(key, 0) + 1

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

            predicate = d.get("predicate", "definition")
            if predicate not in ("refers to", "is defined as", "means", "definition"):
                predicate = "definition"

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

        # 2. Heading hierarchy relationships (section_path): 개념 이름으로 쓸 만한 제목끼리만 잇는다.
        if p.section_path and len(p.section_path) >= 2:
            for i in range(1, len(p.section_path)):
                raw_parent = p.section_path[i - 1].strip()
                raw_child = p.section_path[i].strip()
                if not raw_parent or not raw_child or raw_parent == raw_child:
                    continue
                if not (is_concept_heading(raw_parent) and is_concept_heading(raw_child)):
                    dropped["heading_not_concept"] += 1
                    continue
                parent_name = clean_heading(raw_parent)
                child_name = clean_heading(raw_child)
                if norm_name(parent_name) == norm_name(child_name):
                    continue

                # 근거는 제목 줄 자체(번호 포함 원문)에서 정확한 위치를 찾는다. 없으면 관계를 만들지 않는다.
                evidence_quote = ""
                span_start = span_end = -1
                for candidate in (raw_child, p.heading or "", child_name):
                    if candidate and candidate in body:
                        pos = body.find(candidate)
                        evidence_quote = candidate
                        span_start, span_end = pos, pos + len(candidate)
                        break
                if not evidence_quote:
                    continue

                parent_ent, pkey = _register_entity(parent_name, kind="concept")
                child_ent, ckey = _register_entity(child_name, kind="concept")
                mention_counts[pkey] = mention_counts.get(pkey, 0) + 1
                mention_counts[ckey] = mention_counts.get(ckey, 0) + 1
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

        # 3. Code symbols (`symbol`): 식별자 모양 필터를 통과한 것만 엔터티 후보다.
        for m in INLINE_SYMBOL_PATTERN.finditer(body):
            classified = classify_inline_symbol(m.group(1))
            if classified is None:
                dropped["symbol_not_entity"] += 1
                continue
            name, kind, strong = classified
            ent, key = _register_entity(name, kind=kind, domain="dev")
            if strong:
                strong_keys.add(key)
            mention_counts[key] = mention_counts.get(key, 0) + 1
            result.mentions.append(
                CandidateMention(
                    passage_id=p.id,
                    entity_name=ent.canonical_name,
                    role="mentioned",
                    salience=0.5,
                    evidence_quote=m.group(0),
                    span_start=m.start(),
                    span_end=m.end(),
                )
            )

        # 4. Explicit version assertions: 주제는 개념 제목일 때만 정한다(일반 제목 아래의 버전 문장은 주어를 알 수 없다).
        for m in VERSION_PATTERN.finditer(body):
            v_str = m.group(0).strip()
            start_line = max(0, body.rfind("\n", 0, m.start()) + 1)
            end_line = body.find("\n", m.end())
            if end_line == -1:
                end_line = len(body)
            raw_line = body[start_line:end_line]
            sentence = raw_line.strip()
            if len(sentence) < 10:
                continue
            subj = p.heading or (p.section_path[-1] if p.section_path else None)
            if not subj or not is_concept_heading(subj):
                continue
            canonical_subj, _ = split_bilingual_label(clean_heading(subj))
            q_start = start_line + (len(raw_line) - len(raw_line.lstrip()))
            ent, key = _register_entity(canonical_subj)
            mention_counts[key] = mention_counts.get(key, 0) + 1
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

    _apply_frequency_and_cap(result, seen_entities, defined_keys, strong_keys, mention_counts, dropped)
    result.filtered = dropped
    return result


def _apply_frequency_and_cap(
    result: L0ExtractionResult,
    seen_entities: dict[tuple[str, str], CandidateEntity],
    defined_keys: set[tuple[str, str]],
    strong_keys: set[tuple[str, str]],
    mention_counts: dict[tuple[str, str], int],
    dropped: dict[str, int],
) -> None:
    """약한 후보(식별자 모양이 약하고 정의도 아닌)는 2번 이상 나올 때만 남기고, 점수 순으로 상한을 적용한다."""

    def keep_key(key: tuple[str, str]) -> bool:
        if key in defined_keys or key in strong_keys:
            return True
        if seen_entities[key].kind == "concept":
            return True  # 제목에서 온 개념 후보는 is_concept_heading 을 이미 통과했다
        # 식별자 모양이 약한 한 단어 코드(files, agent …)는 아주 자주 나올 때만 개념으로 본다.
        return mention_counts.get(key, 0) >= WEAK_SYMBOL_MIN_MENTIONS

    def score(key: tuple[str, str]) -> tuple[int, int, int]:
        return (1 if key in defined_keys else 0, mention_counts.get(key, 0), 1 if key in strong_keys else 0)

    keys = list(seen_entities)
    kept = [k for k in keys if keep_key(k)]
    dropped["low_frequency"] += len(keys) - len(kept)
    if len(kept) > MAX_L0_ENTITIES:
        kept = sorted(kept, key=score, reverse=True)[:MAX_L0_ENTITIES]
        dropped["over_cap"] += len(keys) - dropped["low_frequency"] - len(kept)
    kept_names = {seen_entities[k].canonical_name for k in kept}
    kept_set = set(kept)

    result.entities = [e for k, e in seen_entities.items() if k in kept_set]
    result.mentions = [m for m in result.mentions if m.entity_name in kept_names]
    result.assertions = [a for a in result.assertions if a.subject_name in kept_names]
    result.relations = [
        r for r in result.relations if r.source_name in kept_names and r.target_name in kept_names
    ]
