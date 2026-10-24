"""끝난 기획 세션 자동 보관(ADR 0013). 기획이 쌓여 접수 오른쪽 패널과 작업판(최근 BOARD_LEADS 개)이 지저분해지지 않게 한다.

- 대상은 접수(desk) 아래 기획(orchestrator)뿐이다. 옛 메인 오케스트레이터(부모 없음)·일반 대화·작업 세션 단독은 건드리지 않는다.
- 기획과 그 작업 세션 어디에도 대기·실행 Run 이 없고, 마지막 활동이 lead_archive_after_hours 넘게 지난 것만 작업 세션과 함께 보관한다.
- 접수 행을 FOR UPDATE 로 잠그고 다시 확인한다. 접수가 같은 기획에 넘기는 중(desk._delegate 도 같은 행을 잠근다)이면 그 뒤에 판단한다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunStatus, SessionKind, utcnow

log = logging.getLogger(__name__)
ACTIVE = [RunStatus.queued, RunStatus.running]


async def archive_idle_leads(now: datetime | None = None) -> int:
    """보관한 기획 수를 돌려준다. now 는 테스트가 시간을 앞당길 때만 준다."""
    hours = get_settings().lead_archive_after_hours
    if hours <= 0:
        return 0
    cutoff = (now or utcnow()) - timedelta(hours=hours)
    desks = select(ChatSession.id).where(ChatSession.kind == SessionKind.desk)
    async with sessionmaker()() as db:
        desk_ids = list(await db.scalars(
            select(ChatSession.parent_id).distinct().where(
                ChatSession.kind == SessionKind.orchestrator, ChatSession.archived_at.is_(None),
                ChatSession.parent_id.in_(desks))))
    total = 0
    for desk_id in desk_ids:
        # 접수 하나씩 짧은 트랜잭션으로. 잠금을 오래 쥐면 그 접수의 넘기기가 멈춘다.
        async with sessionmaker()() as db:
            n = await _archive_under(db, desk_id, cutoff)
            await db.commit()
        total += n
    if total:
        log.info("archived %d idle lead sessions", total)
    return total


async def _archive_under(db: AsyncSession, desk_id: uuid.UUID, cutoff: datetime) -> int:
    await db.scalar(select(ChatSession.id).where(ChatSession.id == desk_id).with_for_update())
    leads = list(await db.scalars(select(ChatSession).where(
        ChatSession.parent_id == desk_id, ChatSession.kind == SessionKind.orchestrator,
        ChatSession.archived_at.is_(None))))
    n = 0
    for lead in leads:
        from ttakkari.orchestrator import work
        contract = await work.load(db, lead.id)
        if contract.get("state") and contract["state"]["status"] != "complete":
            continue  # Unfinished/blocked/paused goals remain discoverable on the desk board.
        workers = list(await db.scalars(select(ChatSession).where(ChatSession.parent_id == lead.id)))
        ids = [lead.id, *(w.id for w in workers)]
        if await db.scalar(select(func.count()).select_from(Run).where(Run.session_id.in_(ids), Run.status.in_(ACTIVE))):
            continue
        last_run = await db.scalar(select(func.max(func.coalesce(Run.finished_at, Run.created_at)))
                                   .where(Run.session_id.in_(ids)))
        last = max([lead.updated_at, *(w.updated_at for w in workers), *([last_run] if last_run else [])])
        if last > cutoff:
            continue
        now = utcnow()
        for s in (lead, *workers):
            if s.archived_at is None:
                s.archived_at = now
        record(db, actor="system", action="session.auto_archive", target_type="session", target_id=lead.id,
               detail={"workers": len(workers), "last_activity": last.isoformat()})
        n += 1
    return n
