"""지식 도서관 v3 온톨로지(kb_ontology) 패키지.

- seed_aliases: 한·영 시드 별칭 사전 (Bilingual Seed Aliases) 및 시딩
- resolver: 4단계 결정적 엔터티 해소기 (4-Stage Deterministic Entity Resolver)
"""

from __future__ import annotations

from ttakkari.kb_ontology.resolver import (
    EntityResolutionStep,
    ResolutionResult,
    is_domain_compatible,
    resolve_entity,
)
from ttakkari.kb_ontology.seed_aliases import (
    SeedEntity,
    bootstrap_seed_aliases,
    find_seed_by_norm,
    get_seed_entities,
    is_seed_alias,
)

__all__ = [
    "EntityResolutionStep",
    "ResolutionResult",
    "SeedEntity",
    "bootstrap_seed_aliases",
    "find_seed_by_norm",
    "get_seed_entities",
    "is_domain_compatible",
    "is_seed_alias",
    "resolve_entity",
]
