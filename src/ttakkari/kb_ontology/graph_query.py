"""지식 도서관(KB v3) 유계 온톨로지 허브 및 1-Hop 서브그래프 쿼리 서비스 (graph_query).

원칙:
1. 유계 그래프 탐색(Bounded Subgraph):
   무제한 전체 그래프 탐색을 금지하고, 중심(center) 노드 기준 1-hop 인접 노드 및 허브(hub) 노드만 조회한다.
   offset, limit, truncated 플래그를 명시하여 프론트엔드 및 API 워커에 정밀한 페이지네이션을 제공한다.
2. 사실 근거 및 원문 추적성(Traceable Provenance):
   모든 노드와 에지는 원문 증거(kb_evidence -> kb_passages -> kb_documents)로 이어진다.
   권한 있는 근거가 없는 노드나 고아 에지는 그래프 결과에 노출하지 않는다.
3. 가시성 격리(Fail-closed):
   개인 세션(caller_visibility='global')에서는 'work_only' 가시성의 노드, 에지, 인용 증거가 일체 누출되지 않는다.
   회사 세션(caller_visibility='work_only')에서만 global 및 work_only 자산이 통합 조회된다.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_access import (
    KbAccessContext,
    document_access_conditions,
    from_caller_visibility,
)
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology.currency import current_revision_ids
from ttakkari.kb_ontology_models import (
    KbDocumentEntity,
    KbEntity,
    KbEntityCanonical,
    KbRelation,
    KbRelationEvidence,
)

log = logging.getLogger(__name__)
MAX_GRAPH_PAGE_SIZE = 100


def _validate_query(caller_visibility: str, offset: int, limit: int) -> None:
    if caller_visibility not in {"global", "work_only"}:
        raise ValueError("caller_visibility must be 'global' or 'work_only'")
    if offset < 0:
        raise ValueError("offset must be non-negative")
    if limit < 1 or limit > MAX_GRAPH_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_GRAPH_PAGE_SIZE}")


async def _current_revision_map(
    db: AsyncSession, document_ids: set[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """문서별 현재 리비전(rev_no 최대). 주장·정본·충돌과 같은 정의다(kb_ontology.currency)."""
    return await current_revision_ids(db, document_ids)

__all__ = [
    "GraphEdge",
    "GraphEvidenceRef",
    "GraphHubsResult",
    "GraphNode",
    "GraphQueryResult",
    "get_ontology_hubs",
    "query_1hop_subgraph",
]


@dataclass
class GraphEvidenceRef:
    """에지를 뒷받침하는 원문 증거 및 문서 인용 메타."""

    evidence_id: str
    passage_id: str
    document_id: str
    relation: str = "supports"
    confidence: float = 1.0
    quote: str | None = None
    section_path: list[str] | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    document_title: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphNode:
    """온톨로지 그래프 노드 (엔터티 정본 정보 포함)."""

    id: str
    canonical_name: str
    norm_name: str
    entity_kind: str
    domain: str
    summary: str
    visibility: str
    mention_count: int
    document_count: int
    canonical_definition: str | None = None
    has_open_conflict: bool = False
    is_center: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphEdge:
    """온톨로지 그래프 에지 (명시적 관계 및 증거)."""

    id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str
    weight: float
    evidence_count: int
    representative_evidence_id: str | None
    status: str
    visibility: str
    evidence: list[GraphEvidenceRef] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GraphQueryResult:
    """1-Hop 서브그래프 쿼리 결과."""

    center: GraphNode | None
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    total_neighbors: int
    offset: int
    limit: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "center": self.center.to_dict() if self.center else None,
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "total_neighbors": self.total_neighbors,
            "offset": self.offset,
            "limit": self.limit,
            "truncated": self.truncated,
        }


@dataclass
class GraphHubsResult:
    """온톨로지 상위 허브 노드 조회 결과."""

    nodes: list[GraphNode]
    total: int
    offset: int
    limit: int
    truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "truncated": self.truncated,
        }


def _resolve_access(
    access: KbAccessContext | None, caller_visibility: str, tenant_id: str
) -> KbAccessContext:
    """모든 접근 판단은 kb_access 의 단일 규칙을 쓴다. access 를 주지 않으면 caller_visibility 를 같은 규칙의 컨텍스트로 바꾼다."""
    if access is not None:
        return access
    return from_caller_visibility(caller_visibility, ontology_tenant_id=tenant_id)


async def _visible_entity_stats(
    db: AsyncSession,
    *,
    ctx: KbAccessContext,
) -> dict[uuid.UUID, tuple[int, int]]:
    """Return authorized current document/mention counts backed by actual evidence."""
    stmt = (
        select(
            KbDocumentEntity.entity_id,
            KbDocumentEntity.document_id,
            KbDocumentEntity.revision_id,
        )
        .join(
            KbDocumentAccess,
            KbDocumentAccess.document_id == KbDocumentEntity.document_id,
        )
        .join(KbEvidence, KbEvidence.id == KbDocumentEntity.evidence_id)
        .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .where(
            *document_access_conditions(ctx, KbDocumentAccess),
            KbDocumentEntity.evidence_id.is_not(None),
            KbRevision.id == KbDocumentEntity.revision_id,
            KbRevision.document_id == KbDocumentEntity.document_id,
        )
    )
    rows = list((await db.execute(stmt)).all())
    current = await _current_revision_map(db, {row.document_id for row in rows})
    documents: dict[uuid.UUID, set[uuid.UUID]] = {}
    mentions: dict[uuid.UUID, int] = {}
    for row in rows:
        if current.get(row.document_id) != row.revision_id:
            continue
        documents.setdefault(row.entity_id, set()).add(row.document_id)
        mentions[row.entity_id] = mentions.get(row.entity_id, 0) + 1
    return {
        entity_id: (len(doc_ids), mentions[entity_id])
        for entity_id, doc_ids in documents.items()
    }


async def get_ontology_hubs(
    db: AsyncSession,
    *,
    tenant_id: str = "personal",
    caller_visibility: str = "global",
    access: KbAccessContext | None = None,
    entity_kind: str | None = None,
    domain: str | None = None,
    offset: int = 0,
    limit: int = 20,
) -> GraphHubsResult:
    """온톨로지 상위 허브 엔터티 노드를 조회한다.

    필터링:
    - active 상태 엔터티
    - caller_visibility 에 부합하는 엔터티
    - 개인 세션(global)의 경우 인가된 문서 언급이 최소 1개 이상 있는 엔터티만 노출
    """
    _validate_query(caller_visibility, offset, limit)
    ctx = _resolve_access(access, caller_visibility, tenant_id)
    tenant_id = ctx.ontology_tenant_id
    visible_stats = await _visible_entity_stats(db, ctx=ctx)
    documents_by_entity = {
        entity_id: count for entity_id, (count, _) in visible_stats.items()
    }
    mention_count_by_entity = {
        entity_id: count for entity_id, (_, count) in visible_stats.items()
    }
    if not documents_by_entity:
        return GraphHubsResult([], 0, offset, limit, False)

    stmt = select(KbEntity).where(
        KbEntity.id.in_(documents_by_entity),
        KbEntity.tenant_id == tenant_id,
        KbEntity.status == "active",
    )
    stmt = stmt.where(KbEntity.visibility.in_(ctx.allowed_visibilities))
    if entity_kind:
        stmt = stmt.where(KbEntity.entity_kind == entity_kind)
    if domain:
        stmt = stmt.where(KbEntity.domain == domain)
    entities = list(await db.scalars(stmt))
    entities.sort(
        key=lambda entity: (
            -(
                documents_by_entity[entity.id]
                + mention_count_by_entity[entity.id]
            ),
            -entity.created_at.timestamp(),
            str(entity.id),
        )
    )
    total = len(entities)
    entities = entities[offset : offset + limit]

    if not entities:
        return GraphHubsResult(
            nodes=[],
            total=total,
            offset=offset,
            limit=limit,
            truncated=False,
        )

    # 정본 요약 카드 일괄 조회
    entity_ids = [e.id for e in entities]
    canonicals = {
        c.entity_id: c
        for c in await db.scalars(
            select(KbEntityCanonical).where(
                KbEntityCanonical.entity_id.in_(entity_ids),
                KbEntityCanonical.visibility.in_(ctx.allowed_visibilities),
            )
        )
    }

    nodes: list[GraphNode] = []
    for e in entities:
        c = canonicals.get(e.id)
        nodes.append(
            GraphNode(
                id=str(e.id),
                canonical_name=e.canonical_name,
                norm_name=e.norm_name,
                entity_kind=e.entity_kind,
                domain=e.domain,
                summary=c.canonical_definition if c else "",
                visibility=e.visibility,
                mention_count=mention_count_by_entity[e.id],
                document_count=documents_by_entity[e.id],
                canonical_definition=c.canonical_definition if c else None,
                has_open_conflict=c.has_open_conflict if c else False,
                is_center=False,
            )
        )

    truncated = (offset + limit) < total
    return GraphHubsResult(
        nodes=nodes,
        total=total,
        offset=offset,
        limit=limit,
        truncated=truncated,
    )


async def query_1hop_subgraph(
    db: AsyncSession,
    center_entity_id: uuid.UUID,
    *,
    tenant_id: str = "personal",
    caller_visibility: str = "global",
    access: KbAccessContext | None = None,
    offset: int = 0,
    limit: int = 50,
) -> GraphQueryResult:
    """중심 노드(center)를 기준으로 1-hop 서브그래프를 조회한다.

    반환:
    - center: 중심 노드 정보
    - nodes: 중심 노드 및 인접 이웃 노드 목록
    - edges: 중심 노드와 인접 노드 간의 유효 관계 에지 (원문 인용 메타 포함)
    - total_neighbors, offset, limit, truncated
    """
    _validate_query(caller_visibility, offset, limit)
    ctx = _resolve_access(access, caller_visibility, tenant_id)
    tenant_id = ctx.ontology_tenant_id
    center_ent = await db.get(KbEntity, center_entity_id)
    visible_stats = await _visible_entity_stats(db, ctx=ctx)
    if (
        center_ent is None
        or center_ent.status != "active"
        or center_ent.tenant_id != tenant_id
        or center_ent.id not in visible_stats
    ):
        return GraphQueryResult(
            center=None,
            nodes=[],
            edges=[],
            total_neighbors=0,
            offset=offset,
            limit=limit,
            truncated=False,
        )

    # 가시성 검사: 허용되지 않은 visibility 의 노드를 중심점으로 요구하면 거부(존재 비노출)
    if center_ent.visibility not in ctx.allowed_visibilities:
        return GraphQueryResult(
            center=None,
            nodes=[],
            edges=[],
            total_neighbors=0,
            offset=offset,
            limit=limit,
            truncated=False,
        )

    # 중심 노드에 연결된 활성 에지 조회 (근거 1개 이상 필수)
    edge_stmt = select(KbRelation).where(
        or_(
            KbRelation.source_entity_id == center_ent.id,
            KbRelation.target_entity_id == center_ent.id,
        ),
        KbRelation.status == "active",
        KbRelation.evidence_count >= 1,
        KbRelation.visibility.in_(ctx.allowed_visibilities),
    )

    all_relations = list(
        await db.scalars(edge_stmt.order_by(KbRelation.weight.desc()))
    )

    # 권한 있는 최신 원문 근거가 없는 관계는 이웃 계산 전부터 제외한다.
    evidence_by_rel: dict[uuid.UUID, list[GraphEvidenceRef]] = {}
    if all_relations:
        ev_stmt = (
            select(
                KbRelationEvidence,
                KbEvidence,
                KbPassage,
                KbRevision,
                KbDocument,
            )
            .join(KbEvidence, KbEvidence.id == KbRelationEvidence.evidence_id)
            .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
            .join(KbRevision, KbRevision.id == KbPassage.revision_id)
            .join(KbDocument, KbDocument.id == KbRevision.document_id)
            .join(
                KbDocumentAccess,
                KbDocumentAccess.document_id == KbDocument.id,
            )
            .where(
                KbRelationEvidence.relation_id.in_([r.id for r in all_relations]),
                KbRelationEvidence.relation == "supports",
                *document_access_conditions(ctx, KbDocumentAccess),
            )
        )
        # 원문 리비전은 evidence→passage→revision 경로로 검증한다.
        ev_stmt = ev_stmt.where(KbRevision.document_id == KbDocument.id)
        ev_rows = list((await db.execute(ev_stmt)).all())
        current_by_document = await _current_revision_map(
            db, {doc.id for _, _, _, _, doc in ev_rows}
        )
        for r_ev, ev, pas, rev, doc in ev_rows:
            if current_by_document.get(doc.id) != rev.id:
                continue
            section_path = getattr(pas, "section_path", None) or []
            if isinstance(section_path, str):
                try:
                    decoded = json.loads(section_path)
                    section_path = decoded if isinstance(decoded, list) else [section_path]
                except json.JSONDecodeError:
                    section_path = [section_path]
            evidence_by_rel.setdefault(r_ev.relation_id, []).append(
                GraphEvidenceRef(
                    evidence_id=str(ev.id),
                    passage_id=str(pas.id),
                    document_id=str(doc.id),
                    relation=r_ev.relation,
                    confidence=r_ev.confidence,
                    quote=pas.body[ev.span_start : ev.span_end],
                    section_path=section_path,
                    start_offset=ev.span_start,
                    end_offset=ev.span_end,
                    document_title=doc.title,
                )
            )
    all_relations = [r for r in all_relations if evidence_by_rel.get(r.id)]

    # 인접 이웃 엔터티 ID 추출
    neighbor_ids_set: set[uuid.UUID] = set()
    for rel in all_relations:
        other_id = (
            rel.target_entity_id
            if rel.source_entity_id == center_ent.id
            else rel.source_entity_id
        )
        neighbor_ids_set.add(other_id)

    # 이웃 엔터티도 tenant·visibility·현재 근거가 있는 경우만 페이지 계산에 포함.
    neighbor_entities: dict[uuid.UUID, KbEntity] = {}
    if neighbor_ids_set:
        n_stmt = select(KbEntity).where(
            KbEntity.id.in_(neighbor_ids_set),
            KbEntity.tenant_id == tenant_id,
            KbEntity.status == "active",
            KbEntity.visibility.in_(ctx.allowed_visibilities),
        )
        for ent in await db.scalars(n_stmt):
            neighbor_entities[ent.id] = ent
    visible_neighbor_ids = sorted(neighbor_entities)
    total_neighbors = len(visible_neighbor_ids)
    selected_neighbor_ids = visible_neighbor_ids[offset : offset + limit]
    neighbor_entities = {
        entity_id: neighbor_entities[entity_id] for entity_id in selected_neighbor_ids
    }
    truncated = (offset + limit) < total_neighbors

    # 중심 노드와 선택된 이웃 노드들에 해당하는 에지만 필터링
    visible_node_ids = {center_ent.id} | set(neighbor_entities.keys())
    selected_relations = [
        r
        for r in all_relations
        if r.source_entity_id in visible_node_ids
        and r.target_entity_id in visible_node_ids
    ]

    # 정본 카드 일괄 로드
    all_visible_ids = list(visible_node_ids)
    canonicals_map = {
        c.entity_id: c
        for c in await db.scalars(
            select(KbEntityCanonical).where(
                KbEntityCanonical.entity_id.in_(all_visible_ids),
                KbEntityCanonical.visibility.in_(ctx.allowed_visibilities),
            )
        )
    }

    # 중심 노드 객체 생성
    center_c = canonicals_map.get(center_ent.id)
    center_node = GraphNode(
        id=str(center_ent.id),
        canonical_name=center_ent.canonical_name,
        norm_name=center_ent.norm_name,
        entity_kind=center_ent.entity_kind,
        domain=center_ent.domain,
        summary=center_c.canonical_definition if center_c else "",
        visibility=center_ent.visibility,
        mention_count=visible_stats[center_ent.id][1],
        document_count=visible_stats[center_ent.id][0],
        canonical_definition=center_c.canonical_definition if center_c else None,
        has_open_conflict=center_c.has_open_conflict if center_c else False,
        is_center=True,
    )

    # 이웃 노드 객체 생성
    nodes = [center_node]
    for n_id in selected_neighbor_ids:
        ent = neighbor_entities.get(n_id)
        if ent is None:
            continue
        c = canonicals_map.get(ent.id)
        nodes.append(
            GraphNode(
                id=str(ent.id),
                canonical_name=ent.canonical_name,
                norm_name=ent.norm_name,
                entity_kind=ent.entity_kind,
                domain=ent.domain,
                summary=c.canonical_definition if c else "",
                visibility=ent.visibility,
                mention_count=visible_stats.get(ent.id, (0, 0))[1],
                document_count=visible_stats.get(ent.id, (0, 0))[0],
                canonical_definition=c.canonical_definition if c else None,
                has_open_conflict=c.has_open_conflict if c else False,
                is_center=False,
            )
        )

    # 에지 객체 생성 (인가된 증거가 있는 에지만 포함)
    edges: list[GraphEdge] = []
    for r in selected_relations:
        ev_list = evidence_by_rel.get(r.id, [])
        # 근거가 비어있으면 노출하지 않는다
        if not ev_list:
            continue
        edges.append(
            GraphEdge(
                id=str(r.id),
                source_entity_id=str(r.source_entity_id),
                target_entity_id=str(r.target_entity_id),
                relation_type=r.relation_type,
                weight=sum(ref.confidence for ref in ev_list) / len(ev_list),
                evidence_count=len(ev_list),
                representative_evidence_id=ev_list[0].evidence_id,
                status=r.status,
                visibility=r.visibility,
                evidence=ev_list,
            )
        )

    return GraphQueryResult(
        center=center_node,
        nodes=nodes,
        edges=edges,
        total_neighbors=total_neighbors,
        offset=offset,
        limit=limit,
        truncated=truncated,
    )
