"""영속 예약 작업 실행·관리 서비스 (ADR 0064).

PostgreSQL scheduled_actions 테이블을 사용해 타이머·지연 작업을 영속화하고,
SELECT FOR UPDATE SKIP LOCKED 로 복수 세대/프로세스 환경에서 중복 없는 정확히 1회 실행을 보장한다.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import generation
from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    ScheduledAction,
    utcnow,
)

log = logging.getLogger(__name__)

SUPPORTED_ACTIONS = frozenset({"stop_all", "cancel_run", "send_message"})


def parse_due_at(val: str | datetime | float, base: datetime | None = None) -> datetime:
    """ISO 8601 문자열, 상대 시간 표현(+10m, -10s, +300s, +1h, +2d), 타임스탬프를 UTC datetime으로 변환한다."""
    base = base or utcnow()
    if isinstance(val, (int, float)):
        return datetime.fromtimestamp(val, UTC)
    if isinstance(val, datetime):
        return val if val.tzinfo is not None else val.replace(tzinfo=UTC)

    s = str(val).strip()
    # 상대 시간 파싱: +300s, -10s, +10m, +1h, +2d 또는 +300 / -10
    m = re.match(r"^([+-]?)(\d+)\s*([smhd]?)$", s, re.IGNORECASE)
    if m:
        sign = -1 if m.group(1) == "-" else 1
        num = int(m.group(2))
        unit = m.group(3).lower()
        multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "": 1}[unit]
        return base + timedelta(seconds=sign * num * multiplier)

    # ISO 8601 파싱
    iso_str = s[:-1] + "+00:00" if s.endswith("Z") else s
    dt = datetime.fromisoformat(iso_str)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def create_schedule(
    db: AsyncSession,
    action_type: str,
    due_at: str | datetime | float,
    payload: dict[str, Any] | None = None,
    description: str | None = None,
    *,
    max_attempts: int = 3,
) -> ScheduledAction:
    """영속 예약 작업을 등록하고 DB에 커밋한다. 활성 세대에 NOTIFY wake를 전송한다."""
    parsed_due = parse_due_at(due_at)
    action_payload = dict(payload or {})

    # 작업 유형별 유효성 검사
    if action_type == "cancel_run":
        if "run_id" not in action_payload:
            raise ValueError("cancel_run action requires 'run_id' in payload")
        uuid.UUID(str(action_payload["run_id"]))
    elif action_type == "send_message":
        if "session_id" not in action_payload:
            raise ValueError("send_message action requires 'session_id' in payload")
        uuid.UUID(str(action_payload["session_id"]))
        if not action_payload.get("message") and not action_payload.get("prompt"):
            raise ValueError("send_message action requires 'message' or 'prompt' in payload")

    item = ScheduledAction(
        action_type=action_type,
        payload=action_payload,
        due_at=parsed_due,
        status="pending",
        max_attempts=max_attempts,
        description=description,
    )
    db.add(item)
    await db.flush()
    await generation.wake(db)
    await db.commit()
    return item


async def list_schedules(
    db: AsyncSession,
    status: str | None = None,
    limit: int = 50,
) -> list[ScheduledAction]:
    """예약 작업 목록을 조회한다."""
    stmt = select(ScheduledAction).order_by(ScheduledAction.due_at.asc(), ScheduledAction.id.asc()).limit(limit)
    if status and status != "all":
        stmt = stmt.where(ScheduledAction.status == status)
    return list(await db.scalars(stmt))


async def get_schedule(db: AsyncSession, schedule_id: uuid.UUID) -> ScheduledAction | None:
    """예약 작업 단건을 조회한다."""
    return await db.get(ScheduledAction, schedule_id)


async def cancel_schedule(db: AsyncSession, schedule_id: uuid.UUID) -> bool:
    """대기 중인 예약 작업을 취소한다."""
    item = await db.get(ScheduledAction, schedule_id)
    if item is None or item.status not in {"pending", "leased"}:
        return False
    item.status = "cancelled"
    item.updated_at = utcnow()
    await db.commit()
    return True


async def acquire_due_schedules(
    db: AsyncSession,
    owner_id: str,
    limit: int = 10,
    lease_duration_seconds: float = 60.0,
    now: datetime | None = None,
) -> list[ScheduledAction]:
    """도래한 예약 작업을 FOR UPDATE SKIP LOCKED 로 중복 없이 차지한다."""
    now = now or utcnow()
    leased_until = now + timedelta(seconds=lease_duration_seconds)
    stmt = (
        select(ScheduledAction)
        .where(
            or_(
                ScheduledAction.status == "pending",
                and_(
                    ScheduledAction.status == "leased",
                    ScheduledAction.leased_until < now,
                    ScheduledAction.attempts < ScheduledAction.max_attempts,
                ),
            ),
            ScheduledAction.due_at <= now,
        )
        .order_by(ScheduledAction.due_at.asc(), ScheduledAction.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = list(await db.scalars(stmt))
    for row in rows:
        row.status = "leased"
        row.owner_id = owner_id
        row.leased_until = leased_until
        row.attempts += 1
        row.updated_at = now
    return rows


async def execute_action(schedule: ScheduledAction, now: datetime | None = None) -> dict[str, Any]:
    """차지된 예약 작업을 실제로 실행한다."""
    from ttakkari.runs.manager import manager

    now = now or utcnow()
    # 지각 허용 한도 정책 (late_limit_seconds): 초과 시 건너뜀
    late_limit = schedule.payload.get("late_limit_seconds")
    if late_limit is not None and (now - schedule.due_at).total_seconds() > float(late_limit):
        return {
            "status": "expired",
            "reason": f"due_at({schedule.due_at.isoformat()}) was more than {late_limit}s ago",
        }

    action = schedule.action_type
    payload = schedule.payload or {}

    if action == "stop_all":
        async with sessionmaker()() as db:
            runs = list(await db.scalars(
                select(Run.id).where(Run.status.in_([RunStatus.queued, RunStatus.running]))
            ))
        stopped = []
        for r in runs:
            if await manager.cancel(r):
                stopped.append(str(r))
        log.info("scheduled stop_all executed: %d runs stopped", len(stopped))
        return {"stopped": stopped, "count": len(stopped)}

    if action == "cancel_run":
        run_id = uuid.UUID(str(payload["run_id"]))
        cancelled = await manager.cancel(run_id)
        log.info("scheduled cancel_run executed for run %s: %s", run_id, cancelled)
        return {"cancelled": cancelled, "run_id": str(run_id)}

    if action == "send_message":
        from ttakkari.api.sessions import queue_run
        session_id = uuid.UUID(str(payload["session_id"]))
        prompt = payload.get("message") or payload.get("prompt") or ""
        async with sessionmaker()() as db:
            sess = await db.get(ChatSession, session_id)
            if sess is None:
                raise ValueError(f"ChatSession {session_id} not found")
            run = await queue_run(db, sess, prompt, origin=RunOrigin.orchestrator)
        log.info("scheduled send_message executed for session %s: run %s queued", session_id, run.id)
        return {"queued_run_id": str(run.id), "session_id": str(session_id)}

    raise ValueError(f"unsupported scheduled action type: {action}")


async def complete_schedule(
    db: AsyncSession,
    schedule_id: uuid.UUID,
    result: dict[str, Any] | None = None,
) -> None:
    """예약 작업의 정상 완료 또는 만료를 기록한다."""
    item = await db.get(ScheduledAction, schedule_id)
    if item is None:
        return
    is_expired = result and result.get("status") == "expired"
    item.status = "expired" if is_expired else "completed"
    item.executed_at = utcnow()
    item.result = result
    item.updated_at = utcnow()


async def fail_schedule(
    db: AsyncSession,
    schedule_id: uuid.UUID,
    error: str,
) -> None:
    """예약 작업의 실패를 기록하고, 최대 시도 횟수 미만이면 재시도 가능하게 돌린다."""
    item = await db.get(ScheduledAction, schedule_id)
    if item is None:
        return
    item.last_error = error[:2000]
    item.updated_at = utcnow()
    if item.attempts >= item.max_attempts:
        item.status = "failed"
    else:
        item.status = "pending"
        item.leased_until = None


async def process_due_schedules(
    owner_id: str | None = None,
    limit: int = 10,
    now: datetime | None = None,
) -> int:
    """도래한 예약 작업을 차지하고 실행 후 결과를 저장한다. 실행된 작업 수를 반환한다."""
    owner = owner_id or f"runner-{uuid.uuid4().hex[:8]}"
    async with sessionmaker()() as db:
        items = await acquire_due_schedules(db, owner_id=owner, limit=limit, now=now)
        await db.commit()

    if not items:
        return 0

    processed = 0
    for item in items:
        try:
            res = await execute_action(item, now=now)
            async with sessionmaker()() as db:
                await complete_schedule(db, item.id, result=res)
                await db.commit()
            processed += 1
        except Exception as exc:
            log.exception("scheduled action %s failed", item.id)
            async with sessionmaker()() as db:
                await fail_schedule(db, item.id, error=str(exc))
                await db.commit()
            processed += 1

    return processed
