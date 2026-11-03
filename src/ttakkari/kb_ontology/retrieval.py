"""KB v3 온톨로지 기반 검색 및 매칭 헬퍼 (src/ttakkari/kb_ontology/retrieval.py).

5대 후보 생성 경로 중:
- 경로 3: 엔터티·별칭 직접 매칭 및 정본/주장 역색인 (entity_direct_rank)
- 경로 4: 1-hop 온톨로지 그래프 확장 (graph_1hop_rank)
을 지원한다.

규칙:
1. 미구현 정본 데이터를 가짜로 만들지 않는다.
2. 무근거 후보를 사실로 승격하거나 similarity 로 자동 alias 를 학습하지 않는다.
3. Strict provenance: 허용된 문서 원천이 확인된 객체만 반환한다.
4. canonical/graph_query 는 별도 worker ee877e88 이 구현 중이므로 검색 전용 안전 조회만 수행한다.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.common.entity_norm import norm_name
from ttakkari.common.lexical import strip_korean_suffix
from ttakkari.kb_access import (
    KbAccessContext,
    assertion_access_filter,
    entity_access_filter,
    entity_alias_access_filter,
    passage_access_filter,
    relation_access_filter,
)
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbEntityCanonical,
    KbRelation,
)

# 후보(candidate) 관계는 확정(active) 관계보다 낮은 가중치로만 반영한다.
CANDIDATE_WEIGHT_FACTOR = 0.3


def _extract_candidate_names(query: str, query_terms: list[str]) -> list[str]:
    """질의 문자열과 토큰들로부터 정규화된 후보 엔터티 명칭들을 추출한다."""
    candidates: set[str] = set()

    # 1. 쿼리 전체 및 어간 정규화
    clean_query = query.strip()
    if clean_query:
        candidates.add(norm_name(clean_query))
        candidates.add(norm_name(strip_korean_suffix(clean_query)))

    # 2. 토큰별 정규화 및 접미사 제거
    for t in query_terms:
        if len(t) >= 2:
            candidates.add(norm_name(t))
            stripped = strip_korean_suffix(t)
            if len(stripped) >= 2:
                candidates.add(norm_name(stripped))

    # 3. 인접 2-gram / 3-gram 조합
    words = [w for w in query_terms if len(w) >= 2]
    for i in range(len(words) - 1):
        candidates.add(norm_name(f"{words[i]} {words[i+1]}"))
        candidates.add(norm_name(f"{words[i]}{words[i+1]}"))
        if i < len(words) - 2:
            candidates.add(norm_name(f"{words[i]} {words[i+1]} {words[i+2]}"))

    return [c for c in candidates if c]


async def find_matched_entities(
    db: AsyncSession,
    query: str,
    query_terms: list[str],
    ctx: KbAccessContext,
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """질의 토큰 및 n-gram과 매칭되는 정본 엔터티를 검색한다.

    kb_entities.norm_name 및 kb_entity_aliases.norm_alias 와 대조하며,
    caller의 access context 에 따라 엄격하게 필터링한다.
    """
    candidate_norms = _extract_candidate_names(query, query_terms)
    if not candidate_norms:
        return []

    # 1. 엔티티 norm_name 직접 매칭
    direct_stmt = (
        select(KbEntity)
        .where(
            KbEntity.norm_name.in_(candidate_norms),
            entity_access_filter(ctx, KbEntity),
        )
        .limit(limit)
    )
    direct_entities = list((await db.execute(direct_stmt)).scalars().all())

    matched_dict: dict[uuid.UUID, dict[str, Any]] = {}
    for ent in direct_entities:
        matched_dict[ent.id] = {
            "entity_id": ent.id,
            "canonical_name": ent.canonical_name,
            "norm_name": ent.norm_name,
            "entity_kind": ent.entity_kind,
            "domain": ent.domain,
            "confidence": ent.confidence,
            "matched_by": "norm_name",
            "matched_alias": None,
            "alias_kind": None,
        }

    # 2. 별칭(kb_entity_aliases) 매칭
    if len(matched_dict) < limit:
        remaining = limit - len(matched_dict)
        alias_stmt = (
            select(KbEntity, KbEntityAlias)
            .join(KbEntityAlias, KbEntityAlias.entity_id == KbEntity.id)
            .where(
                KbEntityAlias.norm_alias.in_(candidate_norms),
                entity_access_filter(ctx, KbEntity),
                entity_alias_access_filter(ctx, KbEntity, KbEntityAlias),
            )
            .limit(remaining * 2)
        )
        alias_rows = (await db.execute(alias_stmt)).all()
        for ent, alias in alias_rows:
            if ent.id not in matched_dict:
                matched_dict[ent.id] = {
                    "entity_id": ent.id,
                    "canonical_name": ent.canonical_name,
                    "norm_name": ent.norm_name,
                    "entity_kind": ent.entity_kind,
                    "domain": ent.domain,
                    "confidence": ent.confidence,
                    "matched_by": "alias",
                    "matched_alias": alias.alias,
                    "alias_kind": alias.alias_kind,
                }
            if len(matched_dict) >= limit:
                break

    return list(matched_dict.values())


async def fetch_entity_passages(
    db: AsyncSession,
    entity_ids: list[uuid.UUID],
    ctx: KbAccessContext,
    *,
    limit: int = 30,
) -> list[tuple[uuid.UUID, float]]:
    """매칭된 엔터티와 연결된 passage ID 와 salience 목록을 가져온다 (경로 3)."""
    if not entity_ids:
        return []

    stmt = (
        select(KbDocumentEntity.passage_id, KbDocumentEntity.salience)
        .join(KbPassage, KbPassage.id == KbDocumentEntity.passage_id)
        .where(
            KbDocumentEntity.entity_id.in_(entity_ids),
            passage_access_filter(ctx, KbPassage),
        )
        .order_by(KbDocumentEntity.salience.desc(), KbDocumentEntity.passage_id)
        .limit(limit)
    )
    rows = (await db.execute(stmt)).all()
    return [(row[0], float(row[1])) for row in rows]


async def expand_1hop_relations(
    db: AsyncSession,
    entity_ids: list[uuid.UUID],
    ctx: KbAccessContext,
    *,
    max_per_entity: int = 5,
    include_candidates: bool = False,
) -> list[dict[str, Any]]:
    """매칭된 엔터티들의 1-hop 활성 관계를 확장 조회한다 (경로 4).

    근거가 1개 이상인 active 관계만 조회하며(기본),
    relation_access_filter 를 통해 원본 증거 문서 접근 권한을 확인한다.
    include_candidates=True 면 근거가 있는 candidate 도 '후보'(status='candidate', candidate=True)로 낮은 가중치로 포함한다.
    """
    if not entity_ids:
        return []

    stmt = (
        select(KbRelation)
        .where(
            or_(
                KbRelation.source_entity_id.in_(entity_ids),
                KbRelation.target_entity_id.in_(entity_ids),
            ),
            relation_access_filter(ctx, KbRelation, include_candidates=include_candidates),
        )
        # active 가 먼저, 같은 상태 안에서는 가중치 순(후보가 활성 관계의 자리를 밀어내지 않는다)
        .order_by((KbRelation.status != "active"), KbRelation.weight.desc(), KbRelation.id)
        .limit(len(entity_ids) * max_per_entity)
    )
    relations = list((await db.execute(stmt)).scalars().all())

    # 관계 엔터티들의 ID 수집
    neighbor_ids = set()
    for rel in relations:
        neighbor_ids.add(rel.source_entity_id)
        neighbor_ids.add(rel.target_entity_id)

    # 이웃 엔터티 정보 로드 (접근 허용 필터 적용)
    entities_map: dict[uuid.UUID, KbEntity] = {}
    if neighbor_ids:
        ent_stmt = select(KbEntity).where(
            KbEntity.id.in_(neighbor_ids),
            entity_access_filter(ctx, KbEntity),
        )
        loaded = (await db.execute(ent_stmt)).scalars().all()
        entities_map = {e.id: e for e in loaded}

    result = []
    for rel in relations:
        src = entities_map.get(rel.source_entity_id)
        tgt = entities_map.get(rel.target_entity_id)
        # 양쪽 엔터티 모두 접근 권한이 있을 때만 포함
        if src is not None and tgt is not None:
            is_candidate = rel.status != "active"
            result.append({
                "relation_id": str(rel.id),
                "relation_type": rel.relation_type,
                "status": rel.status,
                "candidate": is_candidate,
                "weight": rel.weight * CANDIDATE_WEIGHT_FACTOR if is_candidate else rel.weight,
                "evidence_count": rel.evidence_count,
                "representative_evidence_id": (
                    str(rel.representative_evidence_id) if rel.representative_evidence_id else None
                ),
                "source_entity": {
                    "id": str(src.id),
                    "canonical_name": src.canonical_name,
                    "kind": src.entity_kind,
                },
                "target_entity": {
                    "id": str(tgt.id),
                    "canonical_name": tgt.canonical_name,
                    "kind": tgt.entity_kind,
                },
            })
    return result


async def fetch_1hop_passages(
    db: AsyncSession,
    expanded_relations: list[dict[str, Any]],
    ctx: KbAccessContext,
    *,
    limit: int = 15,
) -> list[uuid.UUID]:
    """1-hop 확장된 관계들의 대표 근거 또는 이웃 패시지를 수집한다 (경로 4 패시지 후보)."""
    passage_ids: list[uuid.UUID] = []
    rep_evidence_ids = [
        uuid.UUID(r["representative_evidence_id"])
        for r in expanded_relations
        if r.get("representative_evidence_id")
    ]

    if rep_evidence_ids:
        ev_stmt = (
            select(KbEvidence.passage_id)
            .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
            .where(
                KbEvidence.id.in_(rep_evidence_ids),
                passage_access_filter(ctx, KbPassage),
            )
            .limit(limit)
        )
        passage_ids.extend((await db.execute(ev_stmt)).scalars().all())

    return passage_ids[:limit]


async def fetch_canonical_cards(
    db: AsyncSession,
    entity_ids: list[uuid.UUID],
    ctx: KbAccessContext,
    relations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """실제로 DB에 존재하는 정본(KbEntityCanonical)만 조회하여 정본 카드를 구성한다.

    가짜 정본 데이터는 결코 합성하지 않는다.
    """
    if not entity_ids:
        return []

    # JSON provenance fields are display data, not authorization evidence. Rebuild
    # the allowed IDs from document-access rows and passage lineage before returning.
    source_doc_rows = await db.execute(
        select(KbDocumentEntity.document_id)
        .join(KbDocument, KbDocument.id == KbDocumentEntity.document_id)
        .join(KbDocumentAccess, KbDocumentAccess.document_id == KbDocument.id)
        .where(
            KbDocumentEntity.entity_id.in_(entity_ids),
            KbDocumentAccess.tenant_id == ctx.tenant_id,
            KbDocumentAccess.visibility.in_(ctx.allowed_visibilities),
            KbDocumentAccess.hidden.is_(False) if not ctx.include_hidden else True,
            KbDocumentAccess.origin_scope == ctx.origin_scope_filter
            if ctx.origin_scope_filter else True,
        )
    )
    allowed_document_ids = {str(row[0]) for row in source_doc_rows}
    stmt = (
        select(KbEntityCanonical, KbEntity)
        .join(KbEntity, KbEntity.id == KbEntityCanonical.entity_id)
        .where(
            KbEntityCanonical.entity_id.in_(entity_ids),
            KbEntityCanonical.visibility.in_(ctx.allowed_visibilities),
            entity_access_filter(ctx, KbEntity),
        )
    )
    rows = (await db.execute(stmt)).all()
    conflict_a = aliased(KbAssertion)
    conflict_b = aliased(KbAssertion)
    visible_conflict_entities = set(
        await db.scalars(
            select(KbConflict.entity_id)
            .join(conflict_a, conflict_a.id == KbConflict.assertion_a_id)
            .join(conflict_b, conflict_b.id == KbConflict.assertion_b_id)
            .where(
                KbConflict.entity_id.in_(entity_ids),
                KbConflict.status == "open",
                KbConflict.visibility.in_(ctx.allowed_visibilities),
                assertion_access_filter(ctx, conflict_a),
                assertion_access_filter(ctx, conflict_b),
            )
        )
    )
    declared_evidence_ids = {
        uuid.UUID(str(item))
        for canonical, _ in rows
        for item in (canonical.supporting_evidence_ids or [])
        if _is_uuid(item)
    }
    allowed_evidence_ids: set[str] = set()
    if declared_evidence_ids:
        evidence_rows = await db.execute(
            select(KbEvidence.id)
            .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
            .where(
                KbEvidence.id.in_(declared_evidence_ids),
                passage_access_filter(ctx, KbPassage),
            )
        )
        allowed_evidence_ids = {str(row[0]) for row in evidence_rows}

    rel_by_entity: dict[str, list[dict[str, Any]]] = {}
    if relations:
        for r in relations:
            src_id = r["source_entity"]["id"]
            tgt_id = r["target_entity"]["id"]
            rel_by_entity.setdefault(src_id, []).append(r)
            rel_by_entity.setdefault(tgt_id, []).append(r)

    cards = []
    for canonical, ent in rows:
        provenance = _restrict_canonical_provenance(
            {
                "source_document_ids": canonical.source_document_ids or [],
                "supporting_evidence_ids": canonical.supporting_evidence_ids or [],
            },
            allowed_document_ids=allowed_document_ids,
            allowed_evidence_ids=allowed_evidence_ids,
        )
        if not provenance["source_document_ids"] and not provenance["supporting_evidence_ids"]:
            continue
        ent_id_str = str(ent.id)
        cards.append({
            "entity_id": ent_id_str,
            "canonical_name": ent.canonical_name,
            "norm_name": ent.norm_name,
            "entity_kind": ent.entity_kind,
            "domain": ent.domain,
            "canonical_definition": canonical.canonical_definition,
            "key_facts": canonical.key_facts or [],
            "has_open_conflict": ent.id in visible_conflict_entities,
            "version_no": canonical.version_no,
            "supporting_evidence_ids": provenance["supporting_evidence_ids"],
            "source_document_ids": provenance["source_document_ids"],
            "relations": rel_by_entity.get(ent_id_str, []),
        })
    return cards


def _restrict_canonical_provenance(
    card: dict[str, Any],
    *,
    allowed_document_ids: set[str],
    allowed_evidence_ids: set[str],
) -> dict[str, Any]:
    """Return only canonical provenance identifiers authorized by source lineage."""
    result = dict(card)
    result["source_document_ids"] = [
        str(item) for item in card.get("source_document_ids", [])
        if str(item) in allowed_document_ids
    ]
    result["supporting_evidence_ids"] = [
        str(item) for item in card.get("supporting_evidence_ids", [])
        if str(item) in allowed_evidence_ids
    ]
    return result


def _is_uuid(value: Any) -> bool:
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return False
    return True
