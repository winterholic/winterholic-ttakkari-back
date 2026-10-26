"""Absolute day/job spend ceiling for every paid or quota-consuming collector call (CP-S2-05).

The ceiling is enforced by reserving the worst-case cost in a committed transaction BEFORE the provider call:
- day bucket (shared by teacher, judge and synthetic generation) is locked first, then the job bucket, always in
  that order, so concurrent reservations serialize and can never jointly exceed a limit.
- retries, judges and synthetic generation are separate reservations with their own request_key and are all charged.
- a failed, cancelled or receipt-less call is NOT refunded: the reservation becomes `unknown` and keeps its full
  amount until a provider receipt resolves it. Only a receipt can release the difference.
- the day is the Asia/Seoul calendar day at reservation time; settlement hits the reservation's own day, so a call
  that straddles midnight is charged to the day it was admitted in.
- an engine that cannot enforce input/output caps, price them, and return usage is never admitted (teacher off).
Collection volume N never changes these ceilings; `tests/test_collector_budget.py` replays N and 10N.
"""
from __future__ import annotations

import asyncio
import math
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from ttakkari.collector.budget_models import (
    CollectorBudgetBucket,
    CollectorBudgetReservation,
)
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker

KST = ZoneInfo("Asia/Seoul")
PURPOSES = ("teacher", "judge", "synthetic")


class BudgetDenied(Exception):
    """The call must not be made. `code` is stable for UI/state mapping; the job should end in budget_wait/hold."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class BudgetConflict(Exception):
    """Same request_key was reused with different parameters, or a settled reservation was settled differently."""


@dataclass(frozen=True)
class EngineBound:
    """What an engine can actually guarantee. Anything less than all four keeps it off."""
    name: str
    enforces_input_cap: bool
    enforces_output_cap: bool
    returns_usage: bool
    micro_usd_per_1k_input: int | None
    micro_usd_per_1k_output: int | None

    @property
    def bounded(self) -> bool:
        return (self.enforces_input_cap and self.enforces_output_cap and self.returns_usage
                and self.micro_usd_per_1k_input is not None and self.micro_usd_per_1k_output is not None)


@dataclass(frozen=True)
class CallUsage:
    tokens: int
    micro_usd: int
    receipt: dict


@dataclass(frozen=True)
class Reservation:
    request_key: str
    day: str
    job_id: str | None
    state: str
    tokens: int
    micro_usd: int
    replay: bool


def budget_day(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(KST).date().isoformat()


def worst_case(engine: EngineBound, input_tokens: int) -> tuple[int, int]:
    s = get_settings()
    output = s.collector_budget_request_output_tokens
    usd = math.ceil(input_tokens * engine.micro_usd_per_1k_input / 1000) + math.ceil(output * engine.micro_usd_per_1k_output / 1000)
    return input_tokens + output, usd


async def _bucket(db, kind: str, key: str, limits: tuple[int, int, int]) -> CollectorBudgetBucket:
    tokens, calls, usd = limits
    await db.execute(insert(CollectorBudgetBucket).values(
        kind=kind, key=key, limit_tokens=tokens, limit_calls=calls, limit_micro_usd=usd,
        reserved_tokens=0, reserved_calls=0, reserved_micro_usd=0, created_at=datetime.now(UTC)).on_conflict_do_nothing())
    row = await db.get(CollectorBudgetBucket, (kind, key), with_for_update=True, populate_existing=True)
    # The operator's current limit always wins, both when raised and when lowered.
    row.limit_tokens, row.limit_calls, row.limit_micro_usd = tokens, calls, usd
    return row


def _limits(kind: str) -> tuple[int, int, int]:
    s = get_settings()
    if kind == "day":
        return s.collector_budget_day_tokens, s.collector_budget_day_calls, s.collector_budget_day_micro_usd
    return s.collector_budget_job_tokens, s.collector_budget_job_calls, s.collector_budget_job_micro_usd


async def reserve(db, *, request_key: str, purpose: str, engine: EngineBound, input_tokens: int,
                  job_id: uuid.UUID | str | None = None, now: datetime | None = None) -> Reservation:
    """Reserve the worst case. The caller MUST commit before calling the provider. Idempotent per request_key."""
    s = get_settings()
    if purpose not in PURPOSES:
        raise BudgetDenied("unknown_purpose", purpose)
    if not s.collector_teacher_enabled:
        raise BudgetDenied("teacher_disabled")
    if not engine.bounded:
        raise BudgetDenied("engine_unbounded", engine.name)
    if not 0 < input_tokens <= s.collector_budget_request_input_tokens:
        raise BudgetDenied("request_input_cap", f"{input_tokens} > {s.collector_budget_request_input_tokens}")
    tokens, usd = worst_case(engine, input_tokens)
    day = budget_day(now)
    job = str(job_id) if job_id is not None else None
    day_bucket = await _bucket(db, "day", day, _limits("day"))
    existing = await db.scalar(select(CollectorBudgetReservation).where(CollectorBudgetReservation.request_key == request_key))
    if existing is not None:
        if (existing.job_id, existing.purpose, existing.engine, existing.reserved_tokens) != (job, purpose, engine.name, tokens):
            raise BudgetConflict("request_key reused with different parameters")
        return Reservation(request_key, existing.day, existing.job_id, existing.state,
                           existing.charged_tokens, existing.charged_micro_usd, True)
    job_bucket = await _bucket(db, "job", job, _limits("job")) if job else None
    for code, bucket in (("day_budget", day_bucket), ("job_budget", job_bucket)):
        if bucket is None:
            continue
        if (bucket.reserved_calls + 1 > bucket.limit_calls or bucket.reserved_tokens + tokens > bucket.limit_tokens
                or bucket.reserved_micro_usd + usd > bucket.limit_micro_usd):
            raise BudgetDenied(code, f"{bucket.kind}:{bucket.key}")
    for bucket in (day_bucket, job_bucket):
        if bucket is not None:
            bucket.reserved_calls += 1
            bucket.reserved_tokens += tokens
            bucket.reserved_micro_usd += usd
    db.add(CollectorBudgetReservation(
        request_key=request_key, day=day, job_id=job, purpose=purpose, engine=engine.name, state="reserved",
        reserved_tokens=tokens, reserved_micro_usd=usd, charged_tokens=tokens, charged_micro_usd=usd))
    await db.flush()
    return Reservation(request_key, day, job, "reserved", tokens, usd, False)


async def _locked(db, request_key: str):
    probe = await db.scalar(select(CollectorBudgetReservation).where(CollectorBudgetReservation.request_key == request_key))
    if probe is None:
        raise KeyError(request_key)
    day_bucket = await _bucket(db, "day", probe.day, _limits("day"))
    job_bucket = await _bucket(db, "job", probe.job_id, _limits("job")) if probe.job_id else None
    row = await db.get(CollectorBudgetReservation, probe.id, with_for_update=True, populate_existing=True)
    return row, day_bucket, job_bucket


async def settle(db, request_key: str, usage: CallUsage) -> Reservation:
    """Charge the provider's reported usage. A reservation can only shrink with a receipt; usage above the
    reservation (a provider cap violation) is charged in full and flagged instead of hidden."""
    if not usage.receipt:
        raise BudgetDenied("receipt_required")
    row, day_bucket, job_bucket = await _locked(db, request_key)
    if row.state == "settled":
        if (row.charged_tokens, row.charged_micro_usd) != (usage.tokens, usage.micro_usd):
            raise BudgetConflict("reservation already settled with different usage")
        return Reservation(request_key, row.day, row.job_id, "settled", row.charged_tokens, row.charged_micro_usd, True)
    delta_tokens, delta_usd = usage.tokens - row.charged_tokens, usage.micro_usd - row.charged_micro_usd
    for bucket in (day_bucket, job_bucket):
        if bucket is not None:
            bucket.reserved_tokens += delta_tokens
            bucket.reserved_micro_usd += delta_usd
    row.charged_tokens, row.charged_micro_usd = usage.tokens, usage.micro_usd
    row.state, row.settled_at = "settled", datetime.now(UTC)
    row.receipt = {**usage.receipt, **({"overage": True} if usage.tokens > row.reserved_tokens or usage.micro_usd > row.reserved_micro_usd else {})}
    await db.flush()
    return Reservation(request_key, row.day, row.job_id, "settled", row.charged_tokens, row.charged_micro_usd, False)


async def mark_unknown(db, request_key: str) -> None:
    """Outcome not known (error, timeout, cancel, crash). Keep the full worst case occupied."""
    row = await db.scalar(select(CollectorBudgetReservation).where(
        CollectorBudgetReservation.request_key == request_key).with_for_update())
    if row is not None and row.state == "reserved":
        row.state = "unknown"
        await db.flush()


async def sweep_orphans(*, older_than_seconds: int = 900) -> int:
    """A crash between reserve and settle leaves `reserved`; it is the same as unknown, never a refund."""
    cutoff = datetime.now(UTC).timestamp() - older_than_seconds
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(CollectorBudgetReservation).where(
            CollectorBudgetReservation.state == "reserved").with_for_update(skip_locked=True)))
        count = 0
        for row in rows:
            if row.created_at.timestamp() < cutoff:
                row.state = "unknown"
                count += 1
        await db.commit()
        return count


async def snapshot(db, now: datetime | None = None) -> dict:
    """Remaining absolute limits for the UI. Unknown reservations are shown separately and stay deducted."""
    day = budget_day(now)
    bucket = await db.get(CollectorBudgetBucket, ("day", day))
    limits = _limits("day")
    used = (bucket.reserved_tokens, bucket.reserved_calls, bucket.reserved_micro_usd) if bucket else (0, 0, 0)
    unknown = await db.scalar(select(func.count()).select_from(CollectorBudgetReservation).where(
        CollectorBudgetReservation.day == day, CollectorBudgetReservation.state.in_(("unknown", "reserved"))))
    s = get_settings()
    return {
        "day": day, "timezone": "Asia/Seoul", "teacher_enabled": s.collector_teacher_enabled,
        "tokens": {"limit": limits[0], "used": used[0], "remaining": max(0, limits[0] - used[0])},
        "calls": {"limit": limits[1], "used": used[1], "remaining": max(0, limits[1] - used[1])},
        "micro_usd": {"limit": limits[2], "used": used[2], "remaining": max(0, limits[2] - used[2])},
        "unsettled": unknown or 0,
        "per_job": {"tokens": s.collector_budget_job_tokens, "calls": s.collector_budget_job_calls,
                    "micro_usd": s.collector_budget_job_micro_usd},
    }


async def guarded_call(*, request_key: str, purpose: str, engine: EngineBound, input_tokens: int,
                       call: Callable[[], Awaitable[tuple[object, CallUsage]]],
                       job_id: uuid.UUID | str | None = None,
                       replay_result_key: str | None = None) -> object:
    """reserve+commit -> provider call -> settle. The only sanctioned way to spend. A BudgetDenied escapes before
    the call; any failure after it leaves the reservation occupied as `unknown`."""
    async with sessionmaker()() as db:
        reservation = await reserve(db, request_key=request_key, purpose=purpose, engine=engine,
                                    input_tokens=input_tokens, job_id=job_id)
        await db.commit()
    if reservation.replay:
        # An earlier attempt may still be running or may have crashed after the provider charged; never call twice.
        if reservation.state == "settled" and replay_result_key:
            async with sessionmaker()() as db:
                prior = await db.scalar(select(CollectorBudgetReservation).where(
                    CollectorBudgetReservation.request_key == request_key
                ))
                recovered = (prior.receipt or {}).get(replay_result_key) if prior else None
            if recovered is not None:
                return recovered
        raise BudgetDenied("already_attempted", f"{request_key} is {reservation.state}; resolve it with a receipt")
    try:
        output, usage = await call()
        async with sessionmaker()() as db:
            await settle(db, request_key, usage)
            await db.commit()
        return output
    except BaseException:
        await asyncio.shield(_mark(request_key))
        raise


async def _mark(request_key: str) -> None:
    async with sessionmaker()() as db:
        await mark_unknown(db, request_key)
        await db.commit()


async def recover_settled_result(
    *, request_key: str, purpose: str, engine: EngineBound, job_id: uuid.UUID | str | None, result_key: str
) -> tuple[bool, object | None]:
    """Read a result durably coupled to a settled usage receipt; unresolved attempts remain fenced."""
    async with sessionmaker()() as db:
        row = await db.scalar(select(CollectorBudgetReservation).where(
            CollectorBudgetReservation.request_key == request_key
        ))
        if row is None:
            return False, None
        job = str(job_id) if job_id is not None else None
        if (row.job_id, row.purpose, row.engine) != (job, purpose, engine.name):
            raise BudgetConflict("request_key reused with different parameters")
        if row.state == "settled":
            result = (row.receipt or {}).get(result_key)
            if result is not None:
                return True, result
        raise BudgetDenied("already_attempted", f"{request_key} is {row.state} without a recoverable result")
