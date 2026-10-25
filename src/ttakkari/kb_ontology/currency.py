"""온톨로지(주장·충돌·정본·그래프)와 검색이 공유하는 문서 현재 리비전의 단일 정의.

검색(패시지 현재성)과 온톨로지(주장 supersede, 정본 합성, 상충 탐지, 그래프 쿼리)가
동일한 단일 함수를 사용하여 문서의 '현재 리비전'을 일관되게 결정한다.

계약:
- 기존 관측 ledger(KbObservation)의 (event_time.desc(), recorded_at.desc(), id.desc())를 최우선으로 본다.
- 관측이 없는 문서는 rev_no.desc()로 폴백한다.
- 늦게 도착한 오래된 스냅샷, A→B→A 복귀, rev_no↑·event_time↓ 역전 등 모든 경우에서
  검색 현재 패시지와 정본 근거 리비전이 동일한 리비전을 가리키며 정본이 비지 않는다.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import and_, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbObservation
from ttakkari.kb_models import KbRevision

__all__ = [
    "current_revision",
    "current_revision_filter",
    "current_revision_id",
    "current_revision_ids",
    "older_revision_ids",
]


async def current_revision_ids(
    db: AsyncSession, document_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """문서별 현재 리비전 id 매핑.

    1. 관측 ledger(KbObservation)의 (event_time.desc(), recorded_at.desc(), id.desc()) 기준 최신 관측.
    2. 관측이 없는 문서는 KbRevision.rev_no.desc() 기준 최대 rev_no.
    검색의 _current_revision_filter 와 완전히 동일한 단일 계약이다.
    """
    ids = set(document_ids)
    if not ids:
        return {}
    current: dict[uuid.UUID, uuid.UUID] = {}
    observations = await db.execute(
        select(KbObservation.document_id, KbObservation.revision_id)
        .where(KbObservation.document_id.in_(ids))
        .order_by(
            KbObservation.event_time.desc(),
            KbObservation.recorded_at.desc(),
            KbObservation.id.desc(),
        )
    )
    for doc_id, rev_id in observations:
        current.setdefault(doc_id, rev_id)

    missing = ids - set(current.keys())
    if missing:
        revisions = await db.execute(
            select(KbRevision.document_id, KbRevision.id)
            .where(KbRevision.document_id.in_(missing))
            .order_by(KbRevision.document_id, KbRevision.rev_no.desc())
        )
        for doc_id, rev_id in revisions:
            current.setdefault(doc_id, rev_id)
    return current


async def current_revision_id(
    db: AsyncSession, document_id: uuid.UUID
) -> uuid.UUID | None:
    """단일 문서의 현재 리비전 ID를 반환한다."""
    return (await current_revision_ids(db, [document_id])).get(document_id)


async def current_revision(
    db: AsyncSession, document_id: uuid.UUID
) -> KbRevision | None:
    """단일 문서의 현재 리비전 모델 객체를 반환한다."""
    rev_id = await current_revision_id(db, document_id)
    if rev_id is None:
        return None
    return await db.get(KbRevision, rev_id)


async def older_revision_ids(
    db: AsyncSession, revision: KbRevision
) -> set[uuid.UUID]:
    """주어진 revision 에 의해 대체되는 동일 문서의 과거 리비전 ID 집합.

    - revision 이 문서의 현재 리비전인 경우: 동일 문서의 다른 모든 리비전(id != revision.id) 및
      명시적 supersedes_id 가 대체 대상이다.
    - revision 이 현재 리비전이 아닌(과거 스냅샷인) 경우: 명시적 supersedes_id 만 대상이며
      현재 리비전은 절대 대체 대상에 포함되지 않는다.
    """
    curr_id = await current_revision_id(db, revision.document_id)
    older: set[uuid.UUID] = set()
    if revision.supersedes_id is not None:
        older.add(revision.supersedes_id)
    if curr_id == revision.id:
        other_revs = await db.scalars(
            select(KbRevision.id).where(
                KbRevision.document_id == revision.document_id,
                KbRevision.id != revision.id,
            )
        )
        older.update(other_revs)
    else:
        if curr_id is not None:
            older.discard(curr_id)
    return older


def current_revision_filter():
    """SQL 쿼리용 현재 리비전 필터 조건식 (kb_library._current_revision_filter 와 동등)."""
    newer = KbRevision.__table__.alias("newer")
    obs = KbObservation.__table__.alias("current_observation")
    observed = (
        select(obs.c.revision_id)
        .where(obs.c.document_id == KbRevision.document_id)
        .order_by(
            obs.c.event_time.desc(),
            obs.c.recorded_at.desc(),
            obs.c.id.desc(),
        )
        .limit(1)
        .correlate(KbRevision)
        .scalar_subquery()
    )
    legacy = ~select(literal(1)).where(
        newer.c.document_id == KbRevision.document_id,
        newer.c.rev_no > KbRevision.rev_no,
    ).exists()
    return or_(
        and_(observed.is_not(None), KbRevision.id == observed),
        and_(observed.is_(None), legacy),
    )
