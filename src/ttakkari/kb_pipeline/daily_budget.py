"""Database-serialized L2 daily reservations and per-call usage fencing."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.kb_pipeline_models import KbL2BudgetReservation, KbL2BudgetUsage
from ttakkari.models import AppSetting

log = logging.getLogger(__name__)
BUDGET_KEY_PREFIX = "kb_l2_daily_budget:"
DEFAULT_LEASE_SECONDS = 3600


class DailyBudgetExhausted(RuntimeError):
    diagnosis = "daily_budget_exhausted"
    consumed = False


@dataclass(frozen=True)
class BudgetGrant:
    reservation_id: uuid.UUID | None
    granted_calls: int
    remaining_calls: int


def _utc_now(now: datetime | None) -> datetime:
    dt = now or datetime.now(UTC)
    if dt.tzinfo is None:
        raise ValueError("budget timestamps must be timezone-aware")
    return dt.astimezone(UTC)


def _day_key(day: date) -> str:
    return f"{BUDGET_KEY_PREFIX}{day.isoformat()}"


async def _expire_leases(db: AsyncSession, day: date, now: datetime) -> None:
    rows = list(
        await db.scalars(
            select(KbL2BudgetReservation)
            .where(
                # Leases are clamped to their UTC day, so earlier days' leftovers are always due.
                KbL2BudgetReservation.budget_date <= day,
                KbL2BudgetReservation.status == "active",
                KbL2BudgetReservation.lease_expires_at <= now,
            )
            .with_for_update()
        )
    )
    for reservation in rows:
        # A call is written before network dispatch. If the worker died without a
        # final response, count that in-flight slot as used; never refund uncertainty.
        await db.execute(
            update(KbL2BudgetUsage)
            .where(
                KbL2BudgetUsage.reservation_id == reservation.id,
                KbL2BudgetUsage.status == "in_flight",
            )
            .values(status="consumed", error_code="lease_expired_outcome_unknown", finished_at=now)
        )
        reservation.status = "expired"
        reservation.updated_at = now


async def _committed_calls(db: AsyncSession, day: date) -> int:
    """Active leases hold their full allowance; closed ones keep only consumed calls."""
    held = await db.scalar(
        select(func.coalesce(func.sum(KbL2BudgetReservation.reserved_calls), 0)).where(
            KbL2BudgetReservation.budget_date == day,
            KbL2BudgetReservation.status == "active",
        )
    )
    consumed = await db.scalar(
        select(func.count())
        .select_from(KbL2BudgetUsage)
        .join(KbL2BudgetReservation, KbL2BudgetReservation.id == KbL2BudgetUsage.reservation_id)
        .where(
            KbL2BudgetReservation.budget_date == day,
            KbL2BudgetReservation.status != "active",
            KbL2BudgetUsage.status == "consumed",
        )
    )
    return int(held or 0) + int(consumed or 0)


async def reserve_daily_budget(
    db: AsyncSession,
    *,
    reservation_key: str,
    requested_calls: int,
    max_budget: int,
    now: datetime | None = None,
    already_used: int = 0,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> BudgetGrant:
    """Atomically reserve calls across all processes and generations.

    `reservation_key` is the stage attempt token, so duplicate delivery of the same
    attempt reuses its reservation. Terminal attempts cannot allocate twice.
    """
    if not reservation_key or len(reservation_key) > 160:
        raise ValueError("reservation_key must be 1..160 characters")
    if requested_calls <= 0 or max_budget <= 0:
        return BudgetGrant(None, 0, 0)
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")

    dt = _utc_now(now)
    day = dt.date()
    key = _day_key(day)
    await db.execute(
        pg_insert(AppSetting)
        .values(key=key, value={"baseline": max(0, already_used)})
        .on_conflict_do_nothing(index_elements=[AppSetting.key])
    )
    setting = await db.scalar(select(AppSetting).where(AppSetting.key == key).with_for_update())
    assert setting is not None
    values: dict[str, Any] = dict(setting.value or {})
    ledger_calls = int(
        await db.scalar(
            select(func.count())
            .select_from(KbL2BudgetUsage)
            .join(
                KbL2BudgetReservation,
                KbL2BudgetReservation.id == KbL2BudgetUsage.reservation_id,
            )
            .where(
                KbL2BudgetReservation.budget_date == day,
                KbL2BudgetUsage.status == "consumed",
            )
        )
        or 0
    )
    # Stage detail is the compatibility baseline for pre-0073 work. Subtract calls
    # already recorded in this ledger so new successful calls are not counted twice.
    unledgered_stage_calls = max(0, already_used - ledger_calls)
    baseline = max(
        0,
        int(values.get("baseline", 0)),
        int(values.get("claimed", 0)),
        unledgered_stage_calls,
    )

    # The shared setting row is the per-day mutex. All reservers reap leases and
    # measure the same ledger while holding it.
    await _expire_leases(db, day, dt)
    existing = await db.scalar(
        select(KbL2BudgetReservation)
        .where(KbL2BudgetReservation.reservation_key == reservation_key)
        .with_for_update()
    )
    if existing is not None:
        if existing.budget_date != day or existing.status != "active" or existing.lease_expires_at <= dt:
            return BudgetGrant(existing.id, 0, max(0, max_budget - baseline - await _committed_calls(db, day)))
        held = baseline + await _committed_calls(db, day)
        return BudgetGrant(existing.id, existing.reserved_calls, max(0, max_budget - held))

    committed = baseline + await _committed_calls(db, day)
    granted = min(requested_calls, max(0, max_budget - committed))
    if granted <= 0:
        return BudgetGrant(None, 0, 0)

    # Never let a lease cross the budget-day boundary.
    midnight = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
    expires = min(dt + timedelta(seconds=lease_seconds), midnight)
    reservation = KbL2BudgetReservation(
        budget_date=day,
        reservation_key=reservation_key,
        reserved_calls=granted,
        status="active",
        lease_expires_at=expires,
        next_call_no=1,
        created_at=dt,
        updated_at=dt,
    )
    db.add(reservation)
    await db.flush()
    values["baseline"] = baseline
    values["updated_at"] = dt.isoformat()
    setting.value = values
    db.add(setting)
    await db.flush()
    remaining = max(0, max_budget - committed - granted)
    log.info("L2 daily reservation created: granted=%d remaining=%d", granted, remaining)
    return BudgetGrant(reservation.id, granted, remaining)


async def begin_budgeted_call(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> uuid.UUID:
    """Fence and write one call intent before dispatching it to a provider."""
    dt = _utc_now(now)
    reservation = await db.scalar(
        select(KbL2BudgetReservation)
        .where(KbL2BudgetReservation.id == reservation_id)
        .with_for_update()
    )
    if (
        reservation is None
        or reservation.status != "active"
        or reservation.budget_date != dt.date()
        or reservation.lease_expires_at <= dt
    ):
        raise DailyBudgetExhausted("L2 reservation lease is no longer active")
    in_use = int(
        await db.scalar(
            select(func.count())
            .select_from(KbL2BudgetUsage)
            .where(
                KbL2BudgetUsage.reservation_id == reservation.id,
                KbL2BudgetUsage.status.in_(("in_flight", "consumed")),
            )
        )
        or 0
    )
    if in_use >= reservation.reserved_calls:
        raise DailyBudgetExhausted("L2 reservation call allowance is spent")
    call_id = uuid.uuid4()
    db.add(
        KbL2BudgetUsage(
            id=call_id,
            reservation_id=reservation.id,
            call_no=reservation.next_call_no,
            status="in_flight",
            started_at=dt,
        )
    )
    reservation.next_call_no += 1
    reservation.updated_at = dt
    await db.flush()
    return call_id


async def finish_budgeted_call(
    db: AsyncSession,
    call_id: uuid.UUID,
    *,
    consumed: bool,
    error_code: str | None = None,
    now: datetime | None = None,
) -> None:
    dt = _utc_now(now)
    row = await db.scalar(select(KbL2BudgetUsage).where(KbL2BudgetUsage.id == call_id).with_for_update())
    if row is None or row.status != "in_flight":
        return
    row.status = "consumed" if consumed else "released"
    row.error_code = (error_code or "")[:64] or None
    row.finished_at = dt
    await db.flush()


async def finish_daily_reservation(
    db: AsyncSession,
    reservation_id: uuid.UUID,
    *,
    cancelled: bool = False,
    now: datetime | None = None,
) -> None:
    """Close a lease; uncertain in-flight calls are charged, unused slots release."""
    dt = _utc_now(now)
    row = await db.scalar(
        select(KbL2BudgetReservation)
        .where(KbL2BudgetReservation.id == reservation_id)
        .with_for_update()
    )
    if row is None or row.status != "active":
        return
    await db.execute(
        update(KbL2BudgetUsage)
        .where(
            KbL2BudgetUsage.reservation_id == reservation_id,
            KbL2BudgetUsage.status == "in_flight",
        )
        .values(status="consumed", error_code="cancelled_outcome_unknown", finished_at=dt)
    )
    row.status = "cancelled" if cancelled else "completed"
    row.updated_at = dt
    await db.flush()


async def get_daily_budget_status(
    db: AsyncSession,
    max_budget: int,
    *,
    now: datetime | None = None,
) -> tuple[int, int]:
    """Read committed/held usage. This is observability only; never an admission check."""
    dt = _utc_now(now)
    setting = await db.scalar(select(AppSetting).where(AppSetting.key == _day_key(dt.date())))
    baseline = int((setting.value or {}).get("baseline", 0)) if setting else 0
    committed = baseline + await _committed_calls(db, dt.date())
    return committed, max(0, max_budget - committed)
