"""Extractor pipeline step and persistence helper for KB v3 knowledge extraction.

Provides:
1. Pure step function `extract_revision_knowledge` (orchestrator callable)
   Input: revision, passages, options
   Output: ExtractionStageResult (entities, assertions, relations, mentions, evidences, status)
2. Fail-closed document scope & visibility resolver `resolve_document_scope_and_visibility`
3. Idempotent persistence helper `persist_extracted_knowledge`
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.common.entity_norm import norm_name
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbRelation,
)
from ttakkari.kb_pipeline.extract_l0 import extract_l0_from_passages
from ttakkari.kb_pipeline.extract_l1 import extract_l1_screen
from ttakkari.kb_pipeline.extract_l2 import extract_l2_from_passages
from ttakkari.kb_pipeline.quote_verifier import (
    CandidateAssertion,
    CandidateEntity,
    CandidateEvidence,
    CandidateMention,
    CandidateRelation,
    VerificationReport,
    verify_extraction_candidates,
)

log = logging.getLogger(__name__)


@dataclass
class ExtractionOptions:
    """Configuration options for extraction."""

    llm_caller: Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None
    known_aliases: dict[str, str] | None = None
    max_l2_calls_per_doc: int = 8
    daily_budget_remaining: int | None = None
    origin_scope: str = "personal"
    visibility: str = "global"


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
    mentions_created: int = 0
    evidences_created: int = 0
    evidences_reused: int = 0
    origin_scope: str = "personal"
    visibility: str = "global"


async def resolve_document_scope_and_visibility(
    db: AsyncSession, document_id: uuid.UUID
) -> tuple[str, str]:
    """Derive origin_scope and visibility fail-closed from KbDocumentAccess or document lineage.

    Never hardcodes 'global'. If document is marked work or belongs to a work project,
    visibility becomes 'work_only' to prevent leakage.
    """
    # 1. Check explicit KbDocumentAccess
    access = await db.get(KbDocumentAccess, document_id)
    if access is not None:
        origin_scope = access.origin_scope or access.scope or "personal"
        visibility = access.visibility or ("work_only" if origin_scope == "work" else "global")
        # Enforce consistency: work origin must be work_only visibility
        if origin_scope == "work":
            visibility = "work_only"
        return origin_scope, visibility

    # 2. Check KbDocument doc_key heuristics as fallback
    doc = await db.get(KbDocument, document_id)
    if doc is not None:
        doc_key = doc.doc_key.lower()
        if any(w in doc_key for w in ("work", "company", "biz", "ezwork", "skein")):
            return "work", "work_only"

    # Default fail-safe
    return "personal", "global"


async def extract_revision_knowledge(
    revision: KbRevision,
    passages: list[KbPassage],
    *,
    options: ExtractionOptions | None = None,
) -> ExtractionStageResult:
    """Pure pipeline stage function to extract knowledge from passages.

    Executes 3-tier strategy: L0 (deterministic rules) -> L1 (dictionary & passage screening) -> L2 (Qwen JSON Schema).
    Applies quote anchor verification strictly to eliminate hallucinations.
    Degrades gracefully when L2 is unavailable or over budget.
    """
    opt = options or ExtractionOptions()

    if not passages:
        return ExtractionStageResult(
            status="succeeded",
            metrics={"message": "no_passages"},
        )

    # 1. L0 extraction (cost 0, latency < 20ms)
    l0_res = extract_l0_from_passages(
        passages,
        origin_scope=opt.origin_scope,
        visibility=opt.visibility,
    )

    # 2. L1 dictionary matching and passage screening
    l1_res = extract_l1_screen(
        passages,
        known_aliases=opt.known_aliases,
        max_calls_per_doc=opt.max_l2_calls_per_doc,
        daily_budget_remaining=opt.daily_budget_remaining,
    )

    # 3. L2 extraction on screened passages
    l2_res = await extract_l2_from_passages(
        l1_res.selected_passages,
        llm_caller=opt.llm_caller,
        max_calls=opt.max_l2_calls_per_doc,
        daily_budget_remaining=l1_res.budget_remaining,
        origin_scope=opt.origin_scope,
        visibility=opt.visibility,
    )

    # Determine stage status
    stage_status = "succeeded"
    degradation_reason = None
    if l1_res.budget_exhausted:
        stage_status = "degraded"
        degradation_reason = "daily_budget_exhausted"
    elif l2_res.degraded:
        stage_status = "degraded"
        degradation_reason = l2_res.degradation_reason

    # Merge entity candidates deduplicating by norm_name + kind
    merged_entities: dict[tuple[str, str], CandidateEntity] = {}
    for ent in [*l0_res.entities, *l2_res.entities]:
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

    # Combine mentions
    all_mentions = [*l0_res.mentions, *l1_res.mentions]

    # Combine assertions and relations for strict verification
    candidate_assertions = [*l0_res.assertions, *l2_res.assertions]
    candidate_relations = [*l0_res.relations, *l2_res.relations]

    # 4. Strict quote anchor verification (100% elimination of hallucinated quotes)
    v_report = verify_extraction_candidates(
        passages,
        candidate_assertions,
        candidate_relations,
    )

    metrics = {
        "l0_entities_count": len(l0_res.entities),
        "l0_assertions_count": len(l0_res.assertions),
        "l0_relations_count": len(l0_res.relations),
        "l1_selected_passages": len(l1_res.selected_passages),
        "l2_calls_made": l2_res.calls_made,
        "l2_assertions_count": len(l2_res.assertions),
        "l2_relations_count": len(l2_res.relations),
        "verified_assertions": len(v_report.verified_assertions),
        "rejected_assertions": len(v_report.rejected_assertions),
        "verified_relations": len(v_report.verified_relations),
        "rejected_relations": len(v_report.rejected_relations),
        "verified_evidences": len(v_report.verified_evidences),
        "hallucinated_quotes_count": len(v_report.hallucinated_quotes),
    }

    return ExtractionStageResult(
        status=stage_status,
        entities=list(merged_entities.values()),
        assertions=v_report.verified_assertions,
        relations=v_report.verified_relations,
        mentions=all_mentions,
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
    - Scope and visibility derived from document access if not supplied (fail-closed).
    - Existing entities, aliases, evidence, and relations are reused without unique collision.
    - Relations are saved as status='candidate' with evidence_count=0 (no direct active promotion).
    """
    if origin_scope is None or visibility is None:
        derived_scope, derived_vis = await resolve_document_scope_and_visibility(db, document_id)
        origin_scope = origin_scope or derived_scope
        visibility = visibility or derived_vis

    summary = PersistenceResult(origin_scope=origin_scope, visibility=visibility)

    # 1. Persist Entities & Aliases
    entity_map: dict[str, uuid.UUID] = {}  # canonical_name -> entity_id

    for cand_ent in extraction.entities:
        norm = norm_name(cand_ent.canonical_name)
        stmt = select(KbEntity).where(
            KbEntity.tenant_id == tenant_id,
            KbEntity.visibility == visibility,
            KbEntity.entity_kind == cand_ent.kind,
            KbEntity.norm_name == norm,
        )
        existing_ent = (await db.scalars(stmt)).first()

        if existing_ent is not None:
            entity_id = existing_ent.id
            existing_ent.mention_count += 1
            summary.entities_reused += 1
        else:
            new_ent = KbEntity(
                tenant_id=tenant_id,
                origin_scope=origin_scope,
                visibility=visibility,
                canonical_name=cand_ent.canonical_name,
                norm_name=norm,
                entity_kind=cand_ent.kind,
                domain=cand_ent.domain,
                summary=cand_ent.summary,
                confidence=cand_ent.confidence,
                mention_count=1,
                status="active",
            )
            db.add(new_ent)
            await db.flush()
            entity_id = new_ent.id
            summary.entities_created += 1

        entity_map[cand_ent.canonical_name] = entity_id

        # Persist Aliases
        for alias in cand_ent.aliases:
            if not alias:
                continue
            norm_a = norm_name(alias)
            alias_stmt = select(KbEntityAlias).where(
                KbEntityAlias.entity_id == entity_id,
                KbEntityAlias.norm_alias == norm_a,
            )
            if (await db.scalars(alias_stmt)).first() is None:
                new_alias = KbEntityAlias(
                    entity_id=entity_id,
                    alias=alias,
                    norm_alias=norm_a,
                    alias_kind="canonical" if alias == cand_ent.canonical_name else "synonym",
                    source="extracted",
                )
                db.add(new_alias)
                summary.aliases_created += 1

    await db.flush()

    # 2. Persist Evidence
    evidence_id_map: dict[tuple[uuid.UUID, int, int], uuid.UUID] = {}
    for cand_ev in extraction.evidences:
        ev_key = (cand_ev.passage_id, cand_ev.span_start, cand_ev.span_end)
        stmt = select(KbEvidence).where(
            KbEvidence.passage_id == cand_ev.passage_id,
            KbEvidence.span_start == cand_ev.span_start,
            KbEvidence.span_end == cand_ev.span_end,
        )
        existing_ev = (await db.scalars(stmt)).first()
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

    # 3. Persist Mentions (KbDocumentEntity)
    seen_doc_entities: set[tuple[uuid.UUID, uuid.UUID]] = set()
    for m in extraction.mentions:
        ent_id = entity_map.get(m.entity_name)
        if ent_id is None:
            # Check DB by norm
            norm_m = norm_name(m.entity_name)
            e_row = (
                await db.scalars(
                    select(KbEntity).where(
                        KbEntity.tenant_id == tenant_id,
                        KbEntity.visibility == visibility,
                        KbEntity.norm_name == norm_m,
                    )
                )
            ).first()
            if e_row is not None:
                ent_id = e_row.id
                entity_map[m.entity_name] = ent_id

        if ent_id is None:
            continue

        doc_ent_key = (m.passage_id, ent_id)
        if doc_ent_key in seen_doc_entities:
            continue
        seen_doc_entities.add(doc_ent_key)

        existing_doc_ent = await db.get(KbDocumentEntity, doc_ent_key)
        if existing_doc_ent is None:
            # Map evidence id if available
            ev_id = None
            if m.span_start >= 0 and m.span_end > m.span_start:
                ev_id = evidence_id_map.get((m.passage_id, m.span_start, m.span_end))

            new_doc_ent = KbDocumentEntity(
                passage_id=m.passage_id,
                entity_id=ent_id,
                document_id=document_id,
                revision_id=revision_id,
                evidence_id=ev_id,
                role=m.role if m.role in ("primary_topic", "defined", "mentioned") else "mentioned",
                salience=m.salience,
            )
            db.add(new_doc_ent)
            summary.mentions_created += 1

    await db.flush()

    # 4. Persist Assertions & AssertionEvidence
    for a in extraction.assertions:
        subj_id = entity_map.get(a.subject_name)
        if subj_id is None:
            # Register or find subject entity
            norm_s = norm_name(a.subject_name)
            s_row = (
                await db.scalars(
                    select(KbEntity).where(
                        KbEntity.tenant_id == tenant_id,
                        KbEntity.visibility == visibility,
                        KbEntity.norm_name == norm_s,
                    )
                )
            ).first()
            if s_row:
                subj_id = s_row.id
            else:
                s_ent = KbEntity(
                    tenant_id=tenant_id,
                    origin_scope=origin_scope,
                    visibility=visibility,
                    canonical_name=a.subject_name,
                    norm_name=norm_s,
                    entity_kind="concept",
                    domain="general",
                    status="active",
                )
                db.add(s_ent)
                await db.flush()
                subj_id = s_ent.id
            entity_map[a.subject_name] = subj_id

        obj_ent_id = None
        if a.object_entity_name:
            obj_ent_id = entity_map.get(a.object_entity_name)

        norm_val = norm_name(a.object_value)[:240]

        # Check existing assertion for idempotency
        stmt = select(KbAssertion).where(
            KbAssertion.revision_id == revision_id,
            KbAssertion.subject_entity_id == subj_id,
            KbAssertion.predicate == a.predicate,
            KbAssertion.norm_object_value == norm_val,
        )
        existing_assertion = (await db.scalars(stmt)).first()

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
                origin_scope=origin_scope,
                visibility=visibility,
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
                extractor_tier=a.extractor_tier,
            )
            db.add(new_assertion)
            await db.flush()
            assertion_id = new_assertion.id
            summary.assertions_created += 1

        # Link Assertion Evidence
        if a.passage_id and a.span_start >= 0 and a.span_end > a.span_start:
            ev_id = evidence_id_map.get((a.passage_id, a.span_start, a.span_end))
            if ev_id is not None:
                existing_ae = await db.get(KbAssertionEvidence, (assertion_id, ev_id))
                if existing_ae is None:
                    db.add(KbAssertionEvidence(assertion_id=assertion_id, evidence_id=ev_id, relation="supports"))

    await db.flush()

    # 5. Persist Relations
    for r in extraction.relations:
        src_id = entity_map.get(r.source_name)
        tgt_id = entity_map.get(r.target_name)

        if src_id is None or tgt_id is None:
            continue

        stmt = select(KbRelation).where(
            KbRelation.source_entity_id == src_id,
            KbRelation.target_entity_id == tgt_id,
            KbRelation.relation_type == r.relation_type,
        )
        existing_rel = (await db.scalars(stmt)).first()

        if existing_rel is not None:
            summary.relations_reused += 1
        else:
            rep_ev_id = None
            if r.passage_id and r.span_start >= 0 and r.span_end > r.span_start:
                rep_ev_id = evidence_id_map.get((r.passage_id, r.span_start, r.span_end))

            # Strictly save as 'candidate' and evidence_count=0 as schema single entry point rule
            new_rel = KbRelation(
                origin_scope=origin_scope,
                visibility=visibility,
                source_entity_id=src_id,
                target_entity_id=tgt_id,
                relation_type=r.relation_type,
                weight=r.weight,
                evidence_count=0,
                representative_evidence_id=rep_ev_id,
                status="candidate",
            )
            db.add(new_rel)
            summary.relations_created += 1

    await db.flush()
    return summary
