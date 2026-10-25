"""지식 도서관 v3 온톨로지(kb_ontology) 패키지.

- seed_aliases: 한·영 시드 별칭 사전(Bilingual Seed Aliases)과 멱등 시딩
- compat: 동음이의어·도메인·kind 호환 규칙
- resolver: 결정적 엔터티 해소기. 유사도는 병합 후보만 만들고 자동 연결하지 않는다.
"""

from __future__ import annotations

from ttakkari.kb_ontology.compat import KNOWN_HOMONYMS, is_domain_compatible, is_kind_compatible
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
    "ResolutionResult",
    "SeedEntity",
    "bootstrap_seed_aliases",
    "derive_scope",
    "ensure_seed_entity",
    "find_seed_by_norm",
    "find_seeds_by_norm",
    "get_seed_entities",
    "is_domain_compatible",
    "is_kind_compatible",
    "is_seed_alias",
    "kb_embed_fn",
    "recount_entity",
    "resolve_entity",
    "validate_seed_inventory",
]
