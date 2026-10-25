"""관계 자동 승격 정책(리드 결정 1)과 승격 진입점 호출.

candidate 관계가 다음 두 조건을 모두 만족하면 active 로 올린다.
 (a) 출처가 L2(모델 추출) 또는 명시적 정의/서술 패턴이다(basis in PROMOTABLE_BASES).
 (b) quote_verifier 를 통과한 정확한 원문 인용 근거가 1개 이상 있다(근거 행 id 가 있어야 호출된다).
헤딩 계층 등 구조 유추 관계(basis="structural")는 candidate 로 남는다.

승격은 스키마의 단일 진입점 activate_relation_with_evidence 만 쓴다. 이 모듈은 상태를 직접 바꾸지 않는다.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.kb_ontology_models import KbRelation, activate_relation_with_evidence

PROMOTABLE_BASES: frozenset[str] = frozenset({"explicit", "l2"})
RELATION_TYPES: frozenset[str] = frozenset(
    {"part_of", "manages", "depends_on", "uses", "implements", "alternative_to", "related_to"}
)


def is_promotable(basis: str | None) -> bool:
    return basis in PROMOTABLE_BASES


def normalize_relation_type(value: str | None) -> str:
    """모델이 허용 목록 밖의 유형을 내면 related_to 로 낮춘다(스키마 CHECK 위반으로 단계가 죽지 않게)."""
    return value if value in RELATION_TYPES else "related_to"


async def promote_with_evidence(
    db: AsyncSession,
    relation: KbRelation,
    evidence_id: uuid.UUID,
    *,
    basis: str | None,
    confidence: float = 1.0,
) -> bool:
    """승격 대상이면 근거를 결속하고 active 로 올린다. 이미 active 였다면 False(새 승격 아님)."""
    if not is_promotable(basis):
        return False
    was_active = relation.status == "active"
    await activate_relation_with_evidence(db, relation, evidence_id, confidence=confidence)
    return not was_active
