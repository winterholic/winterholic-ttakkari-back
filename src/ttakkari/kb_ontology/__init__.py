"""지식 도서관 v3 온톨로지(kb_ontology) 패키지.

- seed_aliases: 한·영 시드 별칭 사전(Bilingual Seed Aliases)과 멱등 시딩
- compat: 동음이의어·도메인·kind 호환 규칙
- resolver: 결정적 엔터티 해소기. 유사도는 병합 후보만 만들고 자동 연결하지 않는다.
- canonicals: 다중 문서 통합 정본 요약(kb_entity_canonicals) 합성 및 최신성(supersedes_id) 관리
- conflicts: 동일 주어·술어 지식 상충 탐지 및 해결 서비스(resolve_conflict)
- graph_query: 유계 온톨로지 허브 노드 및 1-hop 서브그래프 쿼리 서비스
"""

from __future__ import annotations

from ttakkari.kb_ontology.canonicals import (
    get_active_assertions_for_entity,
    rebuild_all_canonicals,
    rebuild_canonical,
    synthesize_canonical_for_entity,
    update_canonicals_for_revision,
    update_superseded_assertions,
)
from ttakkari.kb_ontology.compat import (
    KNOWN_HOMONYMS,
    is_domain_compatible,
    is_kind_compatible,
)
from ttakkari.kb_ontology.conflicts import (
    detect_conflicts_for_entity,
    detect_conflicts_for_revision,
    list_conflicts,
    list_conflicts_with_total,
    resolve_conflict,
)
from ttakkari.kb_ontology.graph_query import (
    GraphEdge,
    GraphEvidenceRef,
    GraphHubsResult,
    GraphNode,
    GraphQueryResult,
    get_ontology_hubs,
    query_1hop_subgraph,
)
from ttakkari.kb_ontology.resolver import (
    EmbedFn,
    EntityResolutionStep,
    ResolutionResult,
    derive_scope,
    kb_embed_fn,
    recount_entity,
    resolve_entity,
)
from ttakkari.kb_ontology.seed_aliases import (
    SeedEntity,
    bootstrap_seed_aliases,
    ensure_seed_entity,
    find_seed_by_norm,
    find_seeds_by_norm,
    get_seed_entities,
    is_seed_alias,
    validate_seed_inventory,
)

__all__ = [
    "KNOWN_HOMONYMS",
    "EmbedFn",
    "EntityResolutionStep",
    "GraphEdge",
    "GraphEvidenceRef",
    "GraphHubsResult",
    "GraphNode",
    "GraphQueryResult",
    "ResolutionResult",
    "SeedEntity",
    "bootstrap_seed_aliases",
    "derive_scope",
    "detect_conflicts_for_entity",
    "detect_conflicts_for_revision",
    "ensure_seed_entity",
    "find_seed_by_norm",
    "find_seeds_by_norm",
    "get_active_assertions_for_entity",
    "get_ontology_hubs",
    "get_seed_entities",
    "is_domain_compatible",
    "is_kind_compatible",
    "is_seed_alias",
    "kb_embed_fn",
    "list_conflicts",
    "list_conflicts_with_total",
    "query_1hop_subgraph",
    "rebuild_all_canonicals",
    "rebuild_canonical",
    "recount_entity",
    "resolve_conflict",
    "resolve_entity",
    "synthesize_canonical_for_entity",
    "update_canonicals_for_revision",
    "update_superseded_assertions",
    "validate_seed_inventory",
]
