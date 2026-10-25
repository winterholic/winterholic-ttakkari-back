"""Extractor pipeline step and persistence helper for KB v3 knowledge extraction.

Provides:
1. Pure step function `extract_revision_knowledge` (L0 rules -> L1 seed dictionary -> L2 adapter, strict quote verification)
2. Idempotent persistence helper `persist_extracted_knowledge` (entities, aliases, evidence, mentions, assertions, relation candidates)

Scope/visibility are never defaulted to 'global': they come from the one shared policy (ttakkari.kb_visibility),
and every derived row (assertion, relation) takes derive_derived_policy over its sources.
Relations are stored as status='candidate' together with real kb_relation_evidence rows. Only relations from L2 or an
explicit sentence pattern (basis in PROMOTABLE_BASES) that carry a quote-verified evidence row are promoted to active,
and only through the schema's single entry point (kb_pipeline.relation_promotion). Heading-derived structure stays candidate.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.common.entity_norm import norm_name
from ttakkari.kb_models import KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbDocumentEntity,
    KbEntity,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_pipeline.extract_l0 import extract_l0_from_passages
from ttakkari.kb_pipeline.extract_l1 import extract_l1_screen
from ttakkari.kb_pipeline.extract_l2 import extract_l2_from_passages
from ttakkari.kb_pipeline.extract_relations import build_label_index, extract_explicit_relations
from ttakkari.kb_pipeline.quote_verifier import (
    CandidateAssertion,
    CandidateEntity,
    CandidateEvidence,
    CandidateMention,
    CandidateRelation,
    VerificationReport,
    verify_extraction_candidates,
)
from ttakkari.kb_pipeline.relation_promotion import normalize_relation_type, promote_with_evidence
from ttakkari.kb_visibility import (
    FAIL_CLOSED,
    derive_derived_policy,
    resolve_document_policy,
)

log = logging.getLogger(__name__)


@dataclass
class ExtractionOptions:
    """Configuration options for extraction. 범위 기본값은 fail-closed 이며 호출자가 문서 접근에서 유도해 넘긴다."""

    llm_caller: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    known_aliases: dict[str, str] | None = None
    known_entities: dict[str, dict[str, Any]] | None = None  # canonical_name -> {kind, domain, summary, aliases}
    max_l2_calls_per_doc: int = 8
    daily_budget_remaining: int | None = None
    origin_scope: str = FAIL_CLOSED[0]
    visibility: str = FAIL_CLOSED[1]


@dataclass
class ExtractionStageResult:
    """Pure output of the extraction stage."""

    status: str  # succeeded | degraded | held | failed
    entities: list[CandidateEntity] = field(default_factory=list)
    assertions: list[CandidateAssertion] = field(default_factory=list)
    relations: list[CandidateRelation] = field(default_factory=list)
    mentions: list[CandidateMention] = field(default_factory=list)
    evidences: list[CandidateEvidence] = field(default_factory=list)
    verification_report: VerificationReport = field(default_factory=VerificationReport)
    metrics: dict[str, Any] = field(default_factory=dict)
    degradation_reason: str | None = None


@dataclass
class PersistenceResult:
    """Summary of DB persistence operation."""

    entities_created: int = 0
    entities_reused: int = 0
    aliases_created: int = 0
    assertions_created: int = 0
    assertions_reused: int = 0
    relations_created: int = 0
    relations_reused: int = 0
    relations_scope_blocked: int = 0
    relation_evidence_created: int = 0
    relations_promoted: int = 0
    mentions_created: int = 0
    evidences_created: int = 0
    evidences_reused: int = 0
    origin_scope: str = FAIL_CLOSED[0]
    visibility: str = FAIL_CLOSED[1]

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


async def resolve_document_scope_and_visibility(
    db: AsyncSession, document_id: uuid.UUID
) -> tuple[str, str]:
    """문서의 (origin_scope, visibility). 접근 레코드가 없거나 미상이면 fail-closed ('work', 'work_only')."""
    return await resolve_document_policy(db, document_id)


async def extract_revision_knowledge(
    revision: KbRevision,
    passages: list[KbPassage],
    *,
    options: ExtractionOptions | None = None,
) -> ExtractionStageResult:
    """Pure pipeline stage function to extract knowledge from passages.

    L0(결정 규칙) -> L1(시드 사전 언급 + L2 대상 선별) -> L2(어댑터가 주입한 모델 호출). 모든 인용은 정확 오프셋 검증을 통과해야 한다.
    L2 가 꺼져 있거나 한도를 넘었거나 실패하면 L0/L1 결과를 보존한 채 degraded 로 끝난다.
    """
    opt = options or ExtractionOptions()

    if not passages:
        return ExtractionStageResult(status="succeeded", metrics={"message": "no_passages"})

    l0_res = extract_l0_from_passages(
        passages, origin_scope=opt.origin_scope, visibility=opt.visibility
    )

    l1_res = extract_l1_screen(
        passages,
        known_aliases=opt.known_aliases,
        max_calls_per_doc=opt.max_l2_calls_per_doc,
        daily_budget_remaining=opt.daily_budget_remaining,
    )

    # L1 이 사전으로 맞춘 언급의 엔터티는 시드 메타(kind/domain/별칭)와 함께 후보로 올린다.
    seed_entities: list[CandidateEntity] = []
    seen_seed: set[str] = set()
    for m in l1_res.mentions:
        meta = (opt.known_entities or {}).get(m.entity_name)
        if meta is None or m.entity_name in seen_seed:
            continue
        seen_seed.add(m.entity_name)
        seed_entities.append(
            CandidateEntity(
                name=m.entity_name,
                canonical_name=m.entity_name,
                aliases=list(meta.get("aliases", [])),
                kind=meta.get("kind", "concept"),
                domain=meta.get("domain", "general"),
                summary=meta.get("summary", ""),
                confidence=0.9,
                origin_scope=opt.origin_scope,
                visibility=opt.visibility,
                alias_source="seed",
            )
        )

    l2_res = await extract_l2_from_passages(
        l1_res.selected_passages,
        llm_caller=opt.llm_caller,
        max_calls=opt.max_l2_calls_per_doc,
        daily_budget_remaining=l1_res.budget_remaining,
        origin_scope=opt.origin_scope,
        visibility=opt.visibility,
    )

    stage_status = "succeeded"
    degradation_reason = None
    if l1_res.budget_exhausted:
        stage_status = "degraded"
        degradation_reason = "daily_budget_exhausted"
    elif l2_res.degraded:
        stage_status = "degraded"
        degradation_reason = l2_res.degradation_reason

    merged_entities: dict[tuple[str, str], CandidateEntity] = {}
    for ent in [*l0_res.entities, *seed_entities, *l2_res.entities]:
        key = (norm_name(ent.canonical_name), ent.kind)
        if key in merged_entities:
            existing = merged_entities[key]
            for a in ent.aliases:
                if a not in existing.aliases:
                    existing.aliases.append(a)
            if ent.summary and not existing.summary:
                existing.summary = ent.summary
        else:
            merged_entities[key] = ent

    # 명시적 서술 패턴 관계: 끝점은 시드 사전과 이번 문서 후보의 표기만 인정한다(새 개념을 만들지 않는다).
    label_index = build_label_index(
        opt.known_entities or {},
        {ent.canonical_name: list(ent.aliases) for ent in merged_entities.values()},
    )
    explicit = extract_explicit_relations(
        passages, label_index, known_candidates={ent.canonical_name for ent in merged_entities.values()}
    )
    for name in sorted(explicit.seed_endpoints):
        meta = (opt.known_entities or {}).get(name)
        if meta is None:
            continue
        merged_entities[(norm_name(name), meta.get("kind", "concept"))] = CandidateEntity(
            name=name,
            canonical_name=name,
            aliases=list(meta.get("aliases", [])),
            kind=meta.get("kind", "concept"),
            domain=meta.get("domain", "general"),
            summary=meta.get("summary", ""),
            confidence=0.9,
            origin_scope=opt.origin_scope,
            visibility=opt.visibility,
            alias_source="seed",
        )

    candidate_assertions = [*l0_res.assertions, *l2_res.assertions]
    candidate_relations = [*l0_res.relations, *explicit.relations, *l2_res.relations]
    candidate_mentions = [*l0_res.mentions, *l1_res.mentions, *explicit.mentions]

    v_report = verify_extraction_candidates(
        passages, candidate_assertions, candidate_relations, candidate_mentions
    )

    metrics = {
        "l0_entities_count": len(l0_res.entities),
        "l0_assertions_count": len(l0_res.assertions),
        "l0_relations_count": len(l0_res.relations),
        "l0_filtered": dict(l0_res.filtered),
        "l1_selected_passages": len(l1_res.selected_passages),
        "l1_mentions_count": len(l1_res.mentions),
        "l1_seed_entities": len(seed_entities),
        "l2_calls_made": l2_res.calls_made,
        "l2_assertions_count": len(l2_res.assertions),
        "l2_relations_count": len(l2_res.relations),
        "explicit_relations_count": len(explicit.relations),
        "verified_assertions": len(v_report.verified_assertions),
        "rejected_assertions": len(v_report.rejected_assertions),
        "verified_relations": len(v_report.verified_relations),
        "rejected_relations": len(v_report.rejected_relations),
        "verified_mentions": len(v_report.verified_mentions),
        "rejected_mentions": len(v_report.rejected_mentions),
        "verified_evidences": len(v_report.verified_evidences),
        "hallucinated_quotes_count": len(v_report.hallucinated_quotes),
    }

    return ExtractionStageResult(
        status=stage_status,
        entities=list(merged_entities.values()),
        assertions=v_report.verified_assertions,
        relations=v_report.verified_relations,
        mentions=v_report.verified_mentions,
        evidences=v_report.verified_evidences,
        verification_report=v_report,
        metrics=metrics,
        degradation_reason=degradation_reason,
    )


async def persist_extracted_knowledge(
    db: AsyncSession,
    extraction: ExtractionStageResult,
    *,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    origin_scope: str | None = None,
    visibility: str | None = None,
    tenant_id: str = "personal",
) -> PersistenceResult:
    """Idempotently persist verified extraction artifacts to kb_ontology tables.

    Rules:
    - 범위·가시성은 문서 접근에서 유도한다(생략 시, 접근 정보가 없으면 work_only). 호출자가 넘긴 값도 문서 정책보다 느슨할 수 없다.
    - 주장·관계의 visibility 는 문서 + 주어/목적어 엔터티에서 derive_derived_policy 로 정한다.
    - 같은 리비전을 다시 저장해도 행이 늘지 않는다(엔터티·별칭·근거·언급·주장·관계·관계 근거 모두 재사용).
    - 관계는 status='candidate' 로만 저장하고 검증된 근거를 kb_relation_evidence 행으로 함께 저장한다. active 승격은 하지 않는다.
    """
    doc_policy = await resolve_document_policy(db, document_id)
    if origin_scope is not None and visibility is not None:
        doc_policy = derive_derived_policy([doc_policy, (origin_scope, visibility)])
    origin_scope, visibility = doc_policy

    summary = PersistenceResult(origin_scope=origin_scope, visibility=visibility)

    entity_map: dict[str, uuid.UUID] = {}
    entity_policy: dict[uuid.UUID, tuple[str, str]] = {}
    touched_entities: dict[uuid.UUID, KbEntity] = {}

    def _resolved_entity_id(name: str) -> uuid.UUID | None:
        """Return only identities resolved from this extraction's verified mentions."""
        return entity_map.get(name)

    # 1. Evidence anchors are persisted first so identity resolution can only learn aliases from
    #    verified passage spans. Never trust CandidateEntity.alias_source as a provenance claim.
    evidence_id_map: dict[tuple[uuid.UUID, int, int], uuid.UUID] = {}
    for cand_ev in extraction.evidences:
        ev_key = (cand_ev.passage_id, cand_ev.span_start, cand_ev.span_end)
        existing_ev = (
            await db.scalars(
                select(KbEvidence).where(
                    KbEvidence.passage_id == cand_ev.passage_id,
                    KbEvidence.span_start == cand_ev.span_start,
                    KbEvidence.span_end == cand_ev.span_end,
                )
            )
        ).first()
        if existing_ev is not None:
            evidence_id_map[ev_key] = existing_ev.id
            summary.evidences_reused += 1
        else:
            new_ev = KbEvidence(
                passage_id=cand_ev.passage_id,
                span_start=cand_ev.span_start,
                span_end=cand_ev.span_end,
                sha256=cand_ev.sha256,
                note=cand_ev.note,
            )
            db.add(new_ev)
            await db.flush()
            evidence_id_map[ev_key] = new_ev.id
            summary.evidences_created += 1

    # Claims and relations can anchor entity mentions when the entity's exact label occurs inside
    # their independently verified quote. This gives endpoints their own exact evidence span.
    anchored_mentions = list(extraction.mentions)
    candidate_by_norm: dict[str, CandidateEntity] = {}
    for candidate in extraction.entities:
        candidate_by_norm[norm_name(candidate.canonical_name)] = candidate
        candidate_by_norm[norm_name(candidate.name)] = candidate
        for alias in candidate.aliases:
            candidate_by_norm.setdefault(norm_name(alias), candidate)

    async def _add_claim_anchor(name: str | None, evidence: CandidateEvidence | None, role: str) -> None:
        if not name or evidence is None:
            return
        passage = await db.get(KbPassage, evidence.passage_id)
        if passage is None or passage.body[evidence.span_start : evidence.span_end] != evidence.quote:
            return
        # Exact matching avoids inventing offsets through Unicode normalization or fuzzy spelling.
        pos = evidence.quote.find(name)
        if pos < 0:
            return
        start = evidence.span_start + pos
        end = start + len(name)
        ev_key = (evidence.passage_id, start, end)
        ev_id = evidence_id_map.get(ev_key)
        if ev_id is None:
            row = (
                await db.scalars(select(KbEvidence).where(
                    KbEvidence.passage_id == evidence.passage_id,
                    KbEvidence.span_start == start,
                    KbEvidence.span_end == end,
                ))
            ).first()
            if row is None:
                row = KbEvidence(
                    passage_id=evidence.passage_id,
                    span_start=start,
                    span_end=end,
                    sha256=hashlib.sha256(name.encode("utf-8")).hexdigest(),
                    note="entity identity span within verified claim evidence",
                )
                db.add(row)
                await db.flush()
                summary.evidences_created += 1
            ev_id = row.id
            evidence_id_map[ev_key] = ev_id
        anchored_mentions.append(CandidateMention(
            passage_id=evidence.passage_id,
            entity_name=name,
            role=role,
            evidence_quote=name,
            span_start=start,
            span_end=end,
            evidence=CandidateEvidence(
                passage_id=evidence.passage_id,
                span_start=start,
                span_end=end,
                quote=name,
                sha256=hashlib.sha256(name.encode("utf-8")).hexdigest(),
            ),
        ))

    for assertion in extraction.assertions:
        await _add_claim_anchor(assertion.subject_name, assertion.evidence, "mentioned")
        await _add_claim_anchor(assertion.object_entity_name, assertion.evidence, "mentioned")
    for relation in extraction.relations:
        await _add_claim_anchor(relation.source_name, relation.evidence, "mentioned")
        await _add_claim_anchor(relation.target_name, relation.evidence, "mentioned")

    # 2. Entities & aliases: resolve only evidence-verified mentions through the ontology resolver.
    #    Candidate entities without a mention anchor are discarded, preventing mention_count=0
    #    nodes and keeping document-derived identity out of the resolver's bypass path.
    from ttakkari.kb_ontology.resolver import resolve_entity

    for mention in anchored_mentions:
        verified = mention.evidence
        if verified is None:
            continue
        passage = await db.get(KbPassage, mention.passage_id)
        if passage is None or passage.body[mention.span_start : mention.span_end] != mention.evidence_quote:
            continue
        evidence_id = evidence_id_map.get((mention.passage_id, mention.span_start, mention.span_end))
        if evidence_id is None:
            continue
        candidate = candidate_by_norm.get(norm_name(mention.entity_name))
        grounded_aliases = candidate.aliases if candidate is not None else []
        resolution_kind = candidate.kind if candidate is not None else "concept"
        resolution_domain = candidate.domain if candidate is not None else "general"
        resolution = await resolve_entity(
            db,
            mention.entity_name,
            aliases=grounded_aliases,
            entity_kind=resolution_kind,
            domain=resolution_domain,
            summary=candidate.summary if candidate is not None else "",
            passage_text=passage.body,
            tenant_id=tenant_id,
            document_id=document_id,
            revision_id=revision_id,
            passage_id=mention.passage_id,
            evidence_id=evidence_id,
            role=mention.role,
            salience=mention.salience,
        )
        entity_id = resolution.entity.id
        entity_map[mention.entity_name] = entity_id
        if candidate is not None:
            entity_map[candidate.name] = entity_id
            entity_map[candidate.canonical_name] = entity_id
        entity_policy[entity_id] = (resolution.entity.origin_scope, resolution.entity.visibility)
        touched_entities[entity_id] = resolution.entity
        if resolution.created:
            summary.entities_created += 1
        else:
            summary.entities_reused += 1
        summary.aliases_created += len(resolution.learned_aliases)

    await db.flush()

    # 3. Mentions: resolve_entity created the anchored link and recounted it. Keep this idempotent
    #    fallback for verified links, without attaching an unverified candidate mention.
    seen_doc_entities: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for m in extraction.mentions:
        if m.evidence is None:
            continue
        ent_id = entity_map.get(m.entity_name)
        if ent_id is None:
            continue

        doc_ent_key = (m.passage_id, ent_id)
        if doc_ent_key in seen_doc_entities:
            continue
        seen_doc_entities.add(doc_ent_key)

        if await db.get(KbDocumentEntity, doc_ent_key) is None:
            ev_id = evidence_id_map.get((m.passage_id, m.span_start, m.span_end))
            db.add(
                KbDocumentEntity(
                    passage_id=m.passage_id,
                    entity_id=ent_id,
                    document_id=document_id,
                    revision_id=revision_id,
                    evidence_id=ev_id,
                    role=m.role if m.role in ("primary_topic", "defined", "mentioned") else "mentioned",
                    salience=m.salience,
                )
            )
            summary.mentions_created += 1

    await db.flush()

    # 4. Assertions & AssertionEvidence
    for a in extraction.assertions:
        subj_id = _resolved_entity_id(a.subject_name)
        if subj_id is None:
            continue
        obj_ent_id = entity_map.get(a.object_entity_name) if a.object_entity_name else None
        norm_val = norm_name(a.object_value)[:240]

        a_scope, a_vis = derive_derived_policy(
            [
                (origin_scope, visibility),
                entity_policy[subj_id],
                *([entity_policy[obj_ent_id]] if obj_ent_id is not None and obj_ent_id in entity_policy else []),
            ]
        )

        existing_assertion = (
            await db.scalars(
                select(KbAssertion).where(
                    KbAssertion.revision_id == revision_id,
                    KbAssertion.subject_entity_id == subj_id,
                    KbAssertion.predicate == a.predicate[:64],
                    KbAssertion.norm_object_value == norm_val,
                )
            )
        ).first()

        if existing_assertion is not None:
            assertion_id = existing_assertion.id
            summary.assertions_reused += 1
        else:
            cond = dict(a.applies_condition)
            if a.applies_version:
                cond["applies_version"] = a.applies_version
            new_assertion = KbAssertion(
                document_id=document_id,
                revision_id=revision_id,
                origin_scope=a_scope,
                visibility=a_vis,
                subject_entity_id=subj_id,
                predicate=a.predicate[:64],
                object_entity_id=obj_ent_id,
                object_value=a.object_value,
                norm_object_value=norm_val,
                modality=a.modality if a.modality in ("fact", "recommendation", "requirement", "deprecated") else "fact",
                polarity=a.polarity if a.polarity in ("positive", "negative") else "positive",
                applies_condition=cond,
                trust_score=a.trust_score,
                status="active",
                extractor_tier=a.extractor_tier if a.extractor_tier in ("L0", "L1", "L2", "human") else "L0",
            )
            db.add(new_assertion)
            await db.flush()
            assertion_id = new_assertion.id
            summary.assertions_created += 1

        if a.passage_id and a.span_start >= 0 and a.span_end > a.span_start:
            ev_id = evidence_id_map.get((a.passage_id, a.span_start, a.span_end))
            if ev_id is not None and await db.get(KbAssertionEvidence, (assertion_id, ev_id)) is None:
                db.add(KbAssertionEvidence(assertion_id=assertion_id, evidence_id=ev_id, relation="supports"))

    await db.flush()

    # 5. Relation candidates + their evidence rows
    for r in extraction.relations:
        src_id = _resolved_entity_id(r.source_name)
        tgt_id = _resolved_entity_id(r.target_name)
        if src_id is None or tgt_id is None or src_id == tgt_id:
            continue

        ev_id = None
        if r.passage_id and r.span_start >= 0 and r.span_end > r.span_start:
            ev_id = evidence_id_map.get((r.passage_id, r.span_start, r.span_end))
        if ev_id is None:
            # 검증된 근거가 없는 관계는 저장하지 않는다(근거 없는 관계를 만들지 않는다).
            continue

        r_scope, r_vis = derive_derived_policy(
            [(origin_scope, visibility), entity_policy[src_id], entity_policy[tgt_id]]
        )
        relation_type = normalize_relation_type(r.relation_type)

        rel = (
            await db.scalars(
                select(KbRelation).where(
                    KbRelation.source_entity_id == src_id,
                    KbRelation.target_entity_id == tgt_id,
                    KbRelation.relation_type == relation_type,
                )
            )
        ).first()
        if rel is not None and rel.visibility != r_vis:
            # uq_kb_relation_edge spans visibility. Never attach work-only evidence to an existing
            # global edge (or vice versa); the schema cannot represent two scoped copies yet.
            summary.relations_scope_blocked += 1
            continue
        if rel is None:
            rel = KbRelation(
                origin_scope=r_scope,
                visibility=r_vis,
                source_entity_id=src_id,
                target_entity_id=tgt_id,
                relation_type=relation_type,
                weight=r.weight,
                evidence_count=0,
                representative_evidence_id=ev_id,
                status="candidate",
            )
            db.add(rel)
            await db.flush()
            summary.relations_created += 1
        else:
            summary.relations_reused += 1

        had_evidence = await db.get(KbRelationEvidence, (rel.id, ev_id)) is not None
        if not had_evidence:
            db.add(KbRelationEvidence(relation_id=rel.id, evidence_id=ev_id, confidence=r.weight, relation="supports"))
            await db.flush()
            summary.relation_evidence_created += 1
            # DB 트리거(0072)와 같은 값: 실제 근거 행 수. 트리거가 없는 스키마에서도 일관되게 유지한다.
            rel.evidence_count = int(
                await db.scalar(
                    select(func.count()).select_from(KbRelationEvidence).where(KbRelationEvidence.relation_id == rel.id)
                )
                or 0
            )
        # 검증된 근거 행이 있는 L2/명시적 서술 관계만 단일 진입점으로 승격한다. 구조 유추 관계는 candidate 로 남는다.
        if rel.status == "candidate" and await promote_with_evidence(
            db, rel, ev_id, basis=r.basis, confidence=r.weight
        ):
            summary.relations_promoted += 1

    await db.flush()

    # 6. 카운트는 증분 덧셈이 아니라 kb_document_entities 에서 다시 계산한다(재실행해도 불어나지 않는다).
    from ttakkari.kb_ontology.resolver import recount_entity

    for entity_row in touched_entities.values():
        await recount_entity(db, entity_row)

    await db.flush()
    return summary
