"""문서 주제(domain) 값의 단일 정의.

문서의 주제는 두 곳에서 온다. 사용자가 등록 때 고른 값(kb_sources.source_meta['domain'])이 있으면 그것이 우선이고,
없으면 문서에서 추출된 엔터티의 domain(dev, infra, ai, security, business, science, general)에서 자동 분류한다.
프론트의 주제 slug 는 'dev-infra' 처럼 엔터티 domain 두 개를 묶은 값이 있어 여기서 서로 바꾼다.
"""

from __future__ import annotations

from collections import Counter

# 주제 slug -> 엔터티 domain 들
ENTITY_DOMAINS_BY_TOPIC: dict[str, tuple[str, ...]] = {
    "dev-infra": ("dev", "infra"),
    "ai": ("ai",),
    "security": ("security",),
    "business": ("business",),
    "science": ("science",),
}
_TOPIC_BY_ENTITY_DOMAIN = {d: topic for topic, domains in ENTITY_DOMAINS_BY_TOPIC.items() for d in domains}


def entity_domains_for_topic(topic: str) -> tuple[str, ...]:
    """주제 slug 가 가리키는 엔터티 domain 들. 알 수 없는 slug 는 그 값 자체를 엔터티 domain 으로 본다."""
    return ENTITY_DOMAINS_BY_TOPIC.get(topic, (topic,))


def topic_for_entity_domains(counts: Counter[str] | dict[str, int]) -> str | None:
    """엔터티 domain 분포에서 문서 주제를 정한다. 'general' 은 분류 정보가 아니므로 무시한다. 동률은 이름순이다."""
    tally: Counter[str] = Counter()
    for domain, n in counts.items():
        topic = _TOPIC_BY_ENTITY_DOMAIN.get(domain)
        if topic is not None:
            tally[topic] += n
    if not tally:
        return None
    best = max(tally.values())
    return min(t for t, n in tally.items() if n == best)
