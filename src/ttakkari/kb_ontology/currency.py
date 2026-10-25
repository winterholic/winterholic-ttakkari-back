"""온톨로지(주장·충돌·정본·그래프)가 쓰는 문서 현재 리비전의 단일 정의.

같은 문서 안에서는 rev_no 가 큰 리비전이 현재다. 새 리비전이 들어오면 더 낮은 rev_no 의 주장은 superseded 가 되므로
(canonicals.update_superseded_assertions) 현재성도 같은 기준이어야 한다. 관측 시각(event_time)으로 문서 안의 현재를
정하면 늦게 도착한 오래된 스냅샷이 현재 리비전의 주장을 가리고 낡은 리비전만 남아 정본이 비는 역전이 생긴다.

문서 사이의 시간 비교(최신성 가산 등)는 이 모듈의 일이 아니며 event_time 을 그대로 쓴다.
검색의 패시지 현재성(kb_library._current_revision_filter)은 관측 ledger 를 쓰는 별도 계약이다(A→B→A 복귀 등).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.kb_models import KbRevision

__all__ = ["current_revision_id", "current_revision_ids", "older_revision_ids"]


async def current_revision_ids(
    db: AsyncSession, document_ids: Iterable[uuid.UUID]
) -> dict[uuid.UUID, uuid.UUID]:
    """문서별 현재 리비전 id(= 최대 rev_no). 리비전이 없는 문서는 포함하지 않는다."""
    ids = set(document_ids)
    if not ids:
        return {}
    rows = await db.execute(
        select(KbRevision.document_id, KbRevision.id)
        .where(KbRevision.document_id.in_(ids))
        .order_by(KbRevision.document_id, KbRevision.rev_no.desc())
    )
    current: dict[uuid.UUID, uuid.UUID] = {}
    for document_id, revision_id in rows:
        current.setdefault(document_id, revision_id)
    return current


async def current_revision_id(db: AsyncSession, document_id: uuid.UUID) -> uuid.UUID | None:
    return (await current_revision_ids(db, [document_id])).get(document_id)


async def older_revision_ids(db: AsyncSession, revision: KbRevision) -> set[uuid.UUID]:
    """revision 에게 대체되는 리비전 id: 명시적 supersedes_id 와 같은 문서의 더 낮은 rev_no 전부."""
    older: set[uuid.UUID] = set()
    if revision.supersedes_id is not None:
        older.add(revision.supersedes_id)
    older.update(
        await db.scalars(
            select(KbRevision.id).where(
                KbRevision.document_id == revision.document_id,
                KbRevision.rev_no < revision.rev_no,
            )
        )
    )
    return older
