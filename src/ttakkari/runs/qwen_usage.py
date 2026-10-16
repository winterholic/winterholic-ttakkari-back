"""Durable per-call usage from operational local Qwen desk requests."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.db import sessionmaker
from ttakkari.models import QwenTokenUsage

log = logging.getLogger(__name__)
KST = ZoneInfo("Asia/Seoul")


def period_bounds(now: datetime | None = None) -> dict[str, tuple[datetime, datetime]]:
    local = (now or datetime.now(UTC)).astimezone(KST)
    today = datetime.combine(local.date(), time.min, KST)
    monday = today - timedelta(days=local.weekday())
    current = local.astimezone(UTC)
    return {"today": (today.astimezone(UTC), current), "week": (monday.astimezone(UTC), current)}


async def record_call(db: AsyncSession, *, run_id: UUID, request_id: UUID, call_index: int, stage: str,
                      prompt_tokens: int | None, completion_tokens: int | None, total_tokens: int | None,
                      status: str = "metered", recorded_at: datetime | None = None) -> bool:
    if status == "metered":
        if any(v is None or v < 0 for v in (prompt_tokens, completion_tokens, total_tokens)):
            raise ValueError("metered usage requires nonnegative token counts")
        if total_tokens != prompt_tokens + completion_tokens:
            raise ValueError("total_tokens must equal prompt_tokens + completion_tokens")
    elif any(v is not None for v in (prompt_tokens, completion_tokens, total_tokens)):
        raise ValueError("unmetered calls must not contain fabricated token counts")
    stmt = insert(QwenTokenUsage).values(
        run_id=run_id, request_id=request_id, call_index=call_index, stage=stage, status=status,
        input_tokens=prompt_tokens, output_tokens=completion_tokens, total_tokens=total_tokens,
        recorded_at=recorded_at or datetime.now(UTC),
    ).on_conflict_do_nothing(constraint="uq_qwen_token_usage_request_call").returning(QwenTokenUsage.id)
    return (await db.execute(stmt)).scalar_one_or_none() is not None


async def persist_call(*, run_id: UUID, request_id: UUID, call_index: int, stage: str,
                       prompt_tokens: int | None, completion_tokens: int | None, total_tokens: int | None,
                       status: str) -> None:
    """Commit each HTTP call independently so retries/restarts cannot erase earlier usage."""
    async with sessionmaker()() as db:
        await record_call(db, run_id=run_id, request_id=request_id, call_index=call_index, stage=stage,
                          prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                          total_tokens=total_tokens, status=status)
        await db.commit()


async def _period(db: AsyncSession, start: datetime, end: datetime) -> dict:
    rows = await db.execute(select(
        func.count(QwenTokenUsage.id),
        func.count(QwenTokenUsage.id).filter(QwenTokenUsage.status == "metered"),
        func.count(QwenTokenUsage.id).filter(QwenTokenUsage.status == "usage_missing"),
        func.count(QwenTokenUsage.id).filter(QwenTokenUsage.status == "failed"),
        func.coalesce(func.sum(QwenTokenUsage.input_tokens), 0),
        func.coalesce(func.sum(QwenTokenUsage.output_tokens), 0),
        func.coalesce(func.sum(QwenTokenUsage.total_tokens), 0),
        func.max(QwenTokenUsage.recorded_at),
    ).where(QwenTokenUsage.recorded_at >= start, QwenTokenUsage.recorded_at <= end))
    calls, metered, missing, failed, inputs, outputs, total, last = rows.one()
    return {"starts_at": start.isoformat(), "ends_at": end.isoformat(), "calls": calls,
            "metered_calls": metered, "usage_missing_calls": missing, "failed_calls": failed,
            "unmetered_calls": missing + failed,
            "input_tokens": inputs, "output_tokens": outputs, "total_tokens": total,
            "last_used_at": last.isoformat() if last else None}


async def summary(db: AsyncSession, *, now: datetime | None = None, server: dict | None = None) -> dict:
    bounds = period_bounds(now)
    collected = await db.scalar(select(func.min(QwenTokenUsage.recorded_at)))
    return {"model": "Qwen3.5-4B Q4_K_M", "timezone": "Asia/Seoul",
            "collection_started_at": collected.isoformat() if collected else None,
            "server": server or {"enabled": True, "status": "unknown", "detail": None},
            "today": await _period(db, *bounds["today"]),
            "week": await _period(db, *bounds["week"])}
