"""KB v3 사용자 API 의 온톨로지 뷰: 그래프(허브·1-hop 이웃 페이지네이션), 엔터티 상세, 별칭, 충돌(KB3-402).

접근 판단은 전부 호출자가 넘긴 KbAccessContext(kb_access 단일 규칙)로 한다. 관계는 기본이 active 이고,
include_candidates 요청 옵션을 줄 때만 근거가 있는 candidate 를 '후보' 표시와 낮은 가중치로 함께 돌려준다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import kb_library as lib
from ttakkari.common.entity_norm import norm_name
from ttakkari.kb_access import (
    KbAccessContext,
    assertion_access_filter,
    document_access_filter,
    entity_access_filter,
    entity_alias_access_filter,
    passage_access_filter,
    relation_access_filter,
)
from ttakkari.kb_domains import entity_domains_for_topic
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology import (
    get_ontology_hubs,
    list_conflicts_with_total,
    query_1hop_subgraph,
    resolve_conflict,
)
from ttakkari.kb_ontology.graph_query import CANDIDATE_WEIGHT_FACTOR
from ttakkari.kb_ontology.resolver import recount_entity
from ttakkari.kb_ontology.retrieval import fetch_canonical_cards
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_v3_views import _published_at, assertion_views, evidence_refs

MAX_GRAPH_LIMIT = 100
MAX_ALIAS_CHARS = 200
RELINK_LIMIT = 200
AMBIGUOUS_RESOLUTIONS = frozenset({"merged", "kept_separate"})
ASSERTION_RESOLUTIONS = frozenset({"resolved_a", "resolved_b", "resolved_both_valid"})


class EntityNotFound(LookupError):
    pass


class ConflictNotFound(LookupError):
    pass


class AliasInvalid(ValueError):
    pass


class AliasTaken(ValueError):
    """다른 개념이 이미 그 표기를 쓰고 있다. 같은 표기가 두 개념을 가리키면 검색이 모호해지므로 자동으로 옮기지 않는다."""


class ResolutionNotApplicable(ValueError):
    pass


# --------------------------------------------------------------------------------------------
# 그래프
# --------------------------------------------------------------------------------------------
async def _assertion_counts(db: AsyncSession, access: KbAccessContext, entity_ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not entity_ids:
        return {}
    return dict((await db.execute(
        select(KbAssertion.subject_entity_id, func.count()).where(
            KbAssertion.subject_entity_id.in_(entity_ids), KbAssertion.status.in_(("active", "disputed")),
            assertion_access_filter(access, KbAssertion),
        ).group_by(KbAssertion.subject_entity_id)
    )).all())


def _node(n: dict[str, Any], assertion_count: int) -> dict[str, Any]:
    return {
        "id": n["id"],
        "name": n["canonical_name"],
        "entity_kind": n["entity_kind"],
        "domain": n["domain"],
        "document_count": n["document_count"],
        "mention_count": n["mention_count"],
        "assertion_count": assertion_count,
        "has_open_conflict": bool(n.get("has_open_conflict")),
        "summary": n.get("canonical_definition") or n.get("summary") or "",
        "is_center": bool(n.get("is_center")),
    }


def _edge(e: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": e["id"],
        "source": e["source_entity_id"],
        "target": e["target_entity_id"],
        "predicate": e["relation_type"],
        "relation_type": e["relation_type"],
        "weight": e["weight"],
        "status": e["status"],
        "candidate": bool(e.get("candidate")),
        "evidence_count": e["evidence_count"],
        "evidence": e.get("evidence") or [],
    }


async def _edges_among(
    db: AsyncSession, access: KbAccessContext, node_ids: list[uuid.UUID], include_candidates: bool,
) -> list[dict[str, Any]]:
    """주어진 노드들 사이의 관계(양 끝이 모두 노드 집합 안). 현재 리비전의 접근 가능한 근거가 있는 관계만이다."""
    if len(node_ids) < 2:
        return []
    current_evidence = (
        select(KbRelationEvidence.relation_id)
        .join(KbEvidence, KbEvidence.id == KbRelationEvidence.evidence_id)
        .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .where(KbRelationEvidence.relation_id == KbRelation.id, lib._current_revision_filter())
        .correlate(KbRelation)
        .exists()
    )
    rels = list(await db.scalars(
        select(KbRelation).where(
            KbRelation.source_entity_id.in_(node_ids), KbRelation.target_entity_id.in_(node_ids),
            relation_access_filter(access, KbRelation, include_candidates=include_candidates), current_evidence,
        ).order_by((KbRelation.status != "active"), KbRelation.weight.desc(), KbRelation.id)
    ))
    refs = await evidence_refs(db, [r.representative_evidence_id for r in rels if r.representative_evidence_id], access)
    out = []
    for r in rels:
        candidate = r.status != "active"
        ref = refs.get(r.representative_evidence_id)
        out.append({
            "id": str(r.id), "source_entity_id": str(r.source_entity_id), "target_entity_id": str(r.target_entity_id),
            "relation_type": r.relation_type, "status": r.status, "candidate": candidate,
            "weight": r.weight * CANDIDATE_WEIGHT_FACTOR if candidate else r.weight,
            "evidence_count": r.evidence_count, "evidence": [ref] if ref else [],
        })
    return out


async def search_entities(
    db: AsyncSession, access: KbAccessContext, q: str, *, domain: str | None, limit: int,
) -> list[KbEntity]:
    """이름·별칭 부분 일치로 접근 가능한 엔터티를 찾는다(그래프 검색 입력용). 문서 수가 많은 개념이 먼저다."""
    pattern = q.strip()
    alias_match = (
        select(KbEntityAlias.id).where(
            KbEntityAlias.entity_id == KbEntity.id, KbEntityAlias.alias.icontains(pattern, autoescape=True),
            entity_alias_access_filter(access, KbEntity, KbEntityAlias),
        ).correlate(KbEntity).exists()
    )
    stmt = select(KbEntity).where(
        KbEntity.status == "active", entity_access_filter(access, KbEntity),
        or_(KbEntity.canonical_name.icontains(pattern, autoescape=True), alias_match),
    )
    if domain:
        stmt = stmt.where(KbEntity.domain.in_(entity_domains_for_topic(domain)))
    return list(await db.scalars(stmt.order_by(KbEntity.document_count.desc(), KbEntity.canonical_name, KbEntity.id).limit(limit)))


async def graph_v3(
    db: AsyncSession, access: KbAccessContext, *, center_entity_id: uuid.UUID | None, q: str | None, domain: str | None,
    hops: int, limit: int, offset: int, include_candidates: bool,
) -> dict[str, Any]:
    limit = max(1, min(limit, MAX_GRAPH_LIMIT))
    hubs_total = (await get_ontology_hubs(db, access=access, limit=1)).total
    base = {"total_entities": hubs_total, "hops": 1, "hops_requested": hops, "limit": limit, "offset": offset,
            "include_candidates": include_candidates}

    if center_entity_id is not None:
        result = await query_1hop_subgraph(
            db, center_entity_id, access=access, offset=offset, limit=limit, include_candidates=include_candidates
        )
        if result.center is None:
            raise EntityNotFound(str(center_entity_id))
        data = result.to_dict()
        counts = await _assertion_counts(db, access, [uuid.UUID(n["id"]) for n in data["nodes"]])
        return {
            **base,
            "nodes": [_node(n, counts.get(uuid.UUID(n["id"]), 0)) for n in data["nodes"]],
            "edges": [_edge(e) for e in data["edges"]],
            "center_entity_id": str(center_entity_id),
            "total_neighbors": data["total_neighbors"],
            "has_more": data["truncated"],
            "mode": "neighbors",
        }

    if q and q.strip():
        entities = await search_entities(db, access, q, domain=domain, limit=limit)
        ids = [e.id for e in entities]
        hub_nodes = {n.id: n for n in (await get_ontology_hubs(db, access=access, limit=100)).nodes}
        counts = await _assertion_counts(db, access, ids)
        nodes = []
        for e in entities:
            hub = hub_nodes.get(str(e.id))
            n = hub.to_dict() if hub else {
                "id": str(e.id), "canonical_name": e.canonical_name, "entity_kind": e.entity_kind, "domain": e.domain,
                "document_count": e.document_count, "mention_count": e.mention_count, "summary": e.summary,
            }
            nodes.append(_node(n, counts.get(e.id, 0)))
        edges = await _edges_among(db, access, ids, include_candidates)
        return {**base, "nodes": nodes, "edges": [_edge(e) for e in edges],
                "center_entity_id": str(ids[0]) if ids else None, "total_neighbors": 0, "has_more": False, "mode": "search"}

    hubs = await get_ontology_hubs(
        db, access=access, domain=entity_domains_for_topic(domain) if domain else None, offset=offset, limit=limit
    )
    ids = [uuid.UUID(n.id) for n in hubs.nodes]
    counts = await _assertion_counts(db, access, ids)
    edges = await _edges_among(db, access, ids, include_candidates)
    return {**base, "nodes": [_node(n.to_dict(), counts.get(uuid.UUID(n.id), 0)) for n in hubs.nodes],
            "edges": [_edge(e) for e in edges], "center_entity_id": None, "total_neighbors": 0,
            "has_more": hubs.truncated, "mode": "hubs"}


# --------------------------------------------------------------------------------------------
# 충돌
# --------------------------------------------------------------------------------------------
async def conflict_views(db: AsyncSession, access: KbAccessContext, conflicts: list[KbConflict]) -> list[dict[str, Any]]:
    if not conflicts:
        return []
    entity_ids = {c.entity_id for c in conflicts} | {c.candidate_entity_id for c in conflicts if c.candidate_entity_id}
    entities = {e.id: e for e in await db.scalars(select(KbEntity).where(KbEntity.id.in_(entity_ids)))}
    assertion_ids = {a for c in conflicts for a in (c.assertion_a_id, c.assertion_b_id) if a}
    assertions = {a.id: a for a in await db.scalars(select(KbAssertion).where(KbAssertion.id.in_(assertion_ids)))} if assertion_ids else {}
    views = {v["id"]: v for v in await assertion_views(db, list(assertions.values()), access)}
    out = []
    for c in conflicts:
        ent = entities.get(c.entity_id)
        view: dict[str, Any] = {
            "id": str(c.id), "kind": c.conflict_kind, "status": c.status, "entity_id": str(c.entity_id),
            "entity_name": ent.canonical_name if ent else None, "predicate": c.predicate,
            "created_at": c.created_at.isoformat() if c.created_at else None,
            "note": c.resolution_note, "resolved_by": c.resolved_by,
            "resolved_at": c.resolved_at.isoformat() if c.resolved_at else None,
        }
        if c.conflict_kind == "ambiguous_entity":
            for key, ent_id in (("entity_a", c.entity_id), ("entity_b", c.candidate_entity_id)):
                e = entities.get(ent_id) if ent_id else None
                view[key] = None if e is None else {
                    "id": str(e.id), "name": e.canonical_name, "entity_kind": e.entity_kind,
                    "document_count": e.document_count, "domain": e.domain,
                }
        else:
            for key, a_id in (("claim_a", c.assertion_a_id), ("claim_b", c.assertion_b_id)):
                a = assertions.get(a_id) if a_id else None
                if a is None:
                    view[key] = None
                    continue
                av = views.get(str(a.id), {})
                first = (av.get("evidence") or [{}])[0]
                source_id = await db.scalar(select(KbRevision.source_id).where(KbRevision.id == a.revision_id))
                view[key] = {
                    "assertion_id": str(a.id), "text": av.get("text"), "quote": first.get("quote"),
                    "document_id": str(a.document_id), "document_title": first.get("document_title"),
                    "revision_id": str(a.revision_id), "rev_no": first.get("rev_no"),
                    "published_at": await _published_at(db, a.document_id, a.revision_id, source_id) if source_id else None,
                    "passage_id": first.get("passage_id"), "status": a.status, "polarity": a.polarity,
                }
        out.append(view)
    return out


async def conflicts_v3(
    db: AsyncSession, access: KbAccessContext, *, status: str | None, kind: str | None, limit: int, offset: int,
    entity_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    items, total = await list_conflicts_with_total(
        db, access=access, status=status, kind=kind, entity_id=entity_id, offset=offset, limit=min(limit, 100)
    )
    return {"items": await conflict_views(db, access, items), "total": total, "limit": limit, "offset": offset}


async def resolve_conflict_v3(
    db: AsyncSession, access: KbAccessContext, conflict_id: uuid.UUID, *, resolution: str, note: str | None,
) -> dict[str, Any]:
    existing = await db.get(KbConflict, conflict_id)
    if existing is None:
        raise ConflictNotFound(str(conflict_id))
    # 해결 방식은 충돌 종류에 맞아야 한다: 모호 엔터티는 병합/분리 유지, 상충 주장은 A/B 채택/둘 다 유효.
    allowed = AMBIGUOUS_RESOLUTIONS if existing.conflict_kind == "ambiguous_entity" else ASSERTION_RESOLUTIONS
    if resolution not in allowed:
        raise ResolutionNotApplicable(f"{existing.conflict_kind} accepts only {sorted(allowed)}")
    try:
        conflict = await resolve_conflict(db, conflict_id, resolution, resolution_note=note, resolved_by="user", access=access)
    except ValueError as exc:
        if "not found" in str(exc):
            raise ConflictNotFound(str(conflict_id)) from exc  # 접근 규칙을 통과하지 못하면 존재도 알리지 않는다
        raise
    card = (await fetch_canonical_cards(db, [conflict.entity_id], access) or [None])[0]
    view = (await conflict_views(db, access, [conflict]))[0]
    return {
        "status": conflict.status,
        "conflict": view,
        "canonical": None if card is None else {
            "summary": card.get("canonical_definition") or "",
            "key_facts": [f"{f.get('predicate', '')}: {f.get('value', '')}" for f in card.get("key_facts") or [] if isinstance(f, dict)],
            "has_open_conflict": card.get("has_open_conflict", False),
            "version_no": card.get("version_no"),
        },
    }


# --------------------------------------------------------------------------------------------
# 엔터티 상세
# --------------------------------------------------------------------------------------------
async def _accessible_entity(db: AsyncSession, access: KbAccessContext, entity_id: uuid.UUID) -> KbEntity:
    entity = await db.scalar(select(KbEntity).where(KbEntity.id == entity_id, entity_access_filter(access, KbEntity)))
    if entity is None:
        raise EntityNotFound(str(entity_id))
    return entity


async def entity_detail_v3(
    db: AsyncSession, access: KbAccessContext, entity_id: uuid.UUID, *, include_candidates: bool = False,
) -> dict[str, Any]:
    entity = await _accessible_entity(db, access, entity_id)
    aliases = [
        {"alias": a.alias, "lang": a.lang, "kind": a.alias_kind, "source": a.source}
        for a in await db.scalars(
            select(KbEntityAlias).where(
                KbEntityAlias.entity_id == entity.id, entity_alias_access_filter(access, KbEntity, KbEntityAlias)
            ).order_by(KbEntityAlias.source, KbEntityAlias.alias)
        )
    ]

    rels = list(await db.scalars(
        select(KbRelation).where(
            or_(KbRelation.source_entity_id == entity.id, KbRelation.target_entity_id == entity.id),
            relation_access_filter(access, KbRelation, include_candidates=include_candidates),
        ).order_by((KbRelation.status != "active"), KbRelation.weight.desc(), KbRelation.id)
    ))
    other_ids = {r.target_entity_id if r.source_entity_id == entity.id else r.source_entity_id for r in rels}
    others = {
        e.id: e for e in await db.scalars(
            select(KbEntity).where(KbEntity.id.in_(other_ids), entity_access_filter(access, KbEntity), KbEntity.status == "active")
        )
    } if other_ids else {}
    relations = []
    for r in rels:
        outgoing = r.source_entity_id == entity.id
        other = others.get(r.target_entity_id if outgoing else r.source_entity_id)
        if other is None:
            continue
        relations.append({
            "id": str(r.id), "predicate": r.relation_type, "direction": "out" if outgoing else "in",
            "entity_id": str(other.id), "name": other.canonical_name, "status": r.status, "candidate": r.status != "active",
            "evidence_count": r.evidence_count,
        })

    card = (await fetch_canonical_cards(db, [entity.id], access) or [None])[0]
    canonical = None
    if card is not None:
        canonical = {
            "summary": card.get("canonical_definition") or "",
            "key_facts": [f"{f.get('predicate', '')}: {f.get('value', '')}" for f in card.get("key_facts") or [] if isinstance(f, dict)],
            "key_facts_detail": card.get("key_facts") or [],
            "updated_at": None, "version_no": card.get("version_no"),
            "source_document_ids": card.get("source_document_ids") or [],
        }

    assertions = list(await db.scalars(
        select(KbAssertion).where(
            KbAssertion.subject_entity_id == entity.id, KbAssertion.status.in_(("active", "disputed")),
            assertion_access_filter(access, KbAssertion),
        ).order_by(KbAssertion.is_canonical.desc(), KbAssertion.created_at, KbAssertion.id)
    ))

    docs = (await db.execute(
        select(KbDocument, KbRevision.rev_no).join(KbRevision, KbRevision.document_id == KbDocument.id)
        .join(KbDocumentEntity, KbDocumentEntity.revision_id == KbRevision.id)
        .where(KbDocumentEntity.entity_id == entity.id, lib._current_revision_filter(), document_access_filter(access, KbDocument))
        .group_by(KbDocument.id, KbRevision.rev_no).order_by(KbDocument.updated_at.desc())
    )).all()

    conflicts, _ = await list_conflicts_with_total(db, access=access, status="open", entity_id=entity.id, offset=0, limit=50)
    return {
        "id": str(entity.id), "name": entity.canonical_name, "entity_kind": entity.entity_kind, "domain": entity.domain,
        "summary": entity.summary, "document_count": entity.document_count, "mention_count": entity.mention_count,
        "confidence": entity.confidence, "aliases": aliases, "canonical": canonical,
        "assertions": await assertion_views(db, assertions, access), "relations": relations,
        "documents": [{"id": str(d.id), "title": d.title, "rev_no": rev_no} for d, rev_no in docs],
        "conflicts": await conflict_views(db, access, conflicts),
        "include_candidates": include_candidates,
    }


# --------------------------------------------------------------------------------------------
# 별칭 추가와 재연결
# --------------------------------------------------------------------------------------------
async def add_alias(
    db: AsyncSession, access: KbAccessContext, entity_id: uuid.UUID, *, alias: str, lang: str | None,
) -> dict[str, Any]:
    text = alias.strip()
    if not text or len(text) > MAX_ALIAS_CHARS or "\x00" in text:
        raise AliasInvalid("alias_invalid")
    normalized = norm_name(text)
    if len(normalized) < 2:
        raise AliasInvalid("alias_too_short")
    entity = await _accessible_entity(db, access, entity_id)

    existing = await db.scalar(select(KbEntityAlias).where(KbEntityAlias.entity_id == entity.id, KbEntityAlias.norm_alias == normalized))
    if existing is not None:
        return {"alias": {"alias": existing.alias, "lang": existing.lang, "source": existing.source}, "relinked_count": 0, "created": False}
    taken = await db.scalar(
        select(KbEntity.canonical_name).where(
            KbEntity.id != entity.id, KbEntity.status == "active", entity_access_filter(access, KbEntity),
            or_(
                KbEntity.norm_name == normalized,
                select(KbEntityAlias.id).where(KbEntityAlias.entity_id == KbEntity.id, KbEntityAlias.norm_alias == normalized)
                .correlate(KbEntity).exists(),
            ),
        ).limit(1)
    )
    if taken is not None:
        raise AliasTaken(taken)

    row = KbEntityAlias(entity_id=entity.id, alias=text, norm_alias=normalized, lang=(lang or None), alias_kind="synonym", source="user_verified")
    db.add(row)
    await db.flush()

    relinked = await _relink_passages(db, access, entity, text)
    return {"alias": {"alias": text, "lang": row.lang, "source": "user_verified"}, "relinked_count": relinked, "created": True}


async def _relink_passages(db: AsyncSession, access: KbAccessContext, entity: KbEntity, alias: str) -> int:
    """별칭이 정확히 나오는 현재 패시지를 엔터티에 잇는다. 엔터티와 같은 가시성의 문서만 쓴다(권한 경계를 넘어 잇지 않는다)."""
    scoped = dataclasses.replace(access, allowed_visibilities=(entity.visibility,))
    linked = select(KbDocumentEntity.passage_id).where(
        KbDocumentEntity.passage_id == KbPassage.id, KbDocumentEntity.entity_id == entity.id
    ).correlate(KbPassage).exists()
    rows = (await db.execute(
        select(KbPassage, KbRevision.document_id)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .where(
            lib._current_revision_filter(), passage_access_filter(scoped, KbPassage),
            func.strpos(func.lower(KbPassage.body), alias.lower()) > 0, ~linked,
        ).order_by(KbPassage.created_at, KbPassage.id).limit(RELINK_LIMIT)
    )).all()
    count = 0
    needle = alias.lower()
    for passage, document_id in rows:
        start = passage.body.lower().find(needle)
        end = start + len(alias)
        if start < 0 or passage.body[start:end].lower() != needle:
            continue  # lower() 가 길이를 바꾸는 문자열은 정확한 구간을 보장할 수 없으니 잇지 않는다
        quote = passage.body[start:end]
        ev = await db.scalar(select(KbEvidence).where(
            KbEvidence.passage_id == passage.id, KbEvidence.span_start == start, KbEvidence.span_end == end))
        if ev is None:
            ev = KbEvidence(passage_id=passage.id, span_start=start, span_end=end,
                            sha256=hashlib.sha256(quote.encode("utf-8")).hexdigest(), note="user-verified alias relink")
            db.add(ev)
            await db.flush()
        db.add(KbDocumentEntity(
            passage_id=passage.id, entity_id=entity.id, document_id=document_id, revision_id=passage.revision_id,
            evidence_id=ev.id, role="mentioned", salience=0.4,
        ))
        count += 1
    if count:
        await db.flush()
        await recount_entity(db, entity)
    return count
