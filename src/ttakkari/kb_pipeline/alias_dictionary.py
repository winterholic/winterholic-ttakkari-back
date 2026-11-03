"""L1 사전: 시드 별칭(kb_ontology.seed_aliases)을 추출기 입력 형태로 바꾼다.

L1 은 문서 토큰을 사전의 정규화 별칭과 맞춰 언급(mention)을 만든다. 사전은 해소기의 시드와 같은 원천이다.
오탐을 줄이려고 2자 이하 별칭과 일반 단어와 겹치는 별칭(AMBIGUOUS_ALIASES)은 L1 에서 뺀다.
이 사전은 언급 후보일 뿐이며 엔터티 동일성의 확정은 해소기(linking 단계)가 한다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from functools import lru_cache

from ttakkari.common.entity_norm import norm_name

log = logging.getLogger(__name__)

MIN_ALIAS_CHARS = 3
# 시드에 있지만 일반 단어로 더 자주 쓰여 L1 에서는 언급으로 치지 않는 표기(정규화 형태).
AMBIGUOUS_ALIASES = {"go", "고", "service", "서비스", "server", "서버", "client", "클라이언트", "node", "노드"}


@dataclass(frozen=True)
class SeedDictionary:
    """norm_alias -> canonical_name 과 canonical_name -> 시드 메타(kind, domain, summary, aliases)."""

    aliases: dict[str, str] = field(default_factory=dict)
    entities: dict[str, dict] = field(default_factory=dict)


@lru_cache(maxsize=1)
def load_seed_dictionary() -> SeedDictionary:
    try:
        from ttakkari.kb_ontology.seed_aliases import get_seed_entities
    except ImportError as exc:  # 해소기 패키지가 없는 배포에서는 L1 사전이 비어 있다(정직하게 0건)
        log.warning("seed alias dictionary unavailable: %s", exc)
        return SeedDictionary()

    aliases: dict[str, str] = {}
    entities: dict[str, dict] = {}
    for seed in get_seed_entities():
        entities[seed.canonical_name] = {
            "kind": seed.entity_kind,
            "domain": seed.domain,
            "summary": seed.summary,
            "aliases": [a for a, _lang, _kind in seed.aliases],
        }
        for text in [seed.canonical_name, *[a for a, _l, _k in seed.aliases]]:
            n = norm_name(text)
            if len(n) < MIN_ALIAS_CHARS or n in AMBIGUOUS_ALIASES or " " in n:
                continue
            aliases.setdefault(n, seed.canonical_name)
    return SeedDictionary(aliases=aliases, entities=entities)


def load_seed_alias_dictionary() -> dict[str, str]:
    """norm_alias -> canonical_name (ExtractionOptions.known_aliases 형태)."""
    return dict(load_seed_dictionary().aliases)
