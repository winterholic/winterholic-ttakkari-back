"""CP-S2-05: the absolute day/job ceiling holds under concurrency, retries, unknown outcomes and the midnight boundary.

The provider is a fake. What is proven is the reservation/settlement discipline, not any real model's behavior.
"""
import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from ttakkari.collector import budget
from ttakkari.collector.budget import (
    BudgetConflict,
    BudgetDenied,
    CallUsage,
    EngineBound,
)
from ttakkari.collector.budget_models import (
    CollectorBudgetBucket,
    CollectorBudgetReservation,
)
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker

GOOD = EngineBound("fake-bounded", True, True, True, 100, 400)


@pytest.fixture
def limits(monkeypatch):
    s = get_settings()
    for name, value in {
        "collector_teacher_enabled": True, "collector_budget_day_tokens": 60_000, "collector_budget_day_calls": 20,
        "collector_budget_day_micro_usd": 1_000_000, "collector_budget_job_tokens": 12_000, "collector_budget_job_calls": 4,
        "collector_budget_job_micro_usd": 200_000, "collector_budget_request_input_tokens": 2_000,
        "collector_budget_request_output_tokens": 1_000}.items():
        monkeypatch.setattr(s, name, value)
    return s


async def try_reserve(key, *, job=None, engine=GOOD, tokens=1000, purpose="teacher", now=None):
    async with sessionmaker()() as db:
        try:
            r = await budget.reserve(db, request_key=key, purpose=purpose, engine=engine, input_tokens=tokens,
                                     job_id=job, now=now)
            await db.commit()
            return r
        except BudgetDenied as exc:
            await db.rollback()
            return exc


async def test_unbounded_or_disabled_engine_never_spends(limits, monkeypatch):
    for engine in (EngineBound("no-cap", False, True, True, 1, 1), EngineBound("no-usage", True, True, False, 1, 1),
                   EngineBound("no-price", True, True, True, None, 1)):
        denied = await try_reserve("k-" + engine.name, engine=engine)
        assert isinstance(denied, BudgetDenied) and denied.code == "engine_unbounded"
    monkeypatch.setattr(limits, "collector_teacher_enabled", False)
    denied = await try_reserve("k-off")
    assert isinstance(denied, BudgetDenied) and denied.code == "teacher_disabled"
    assert (await try_reserve("k-big", tokens=2001)).code == "teacher_disabled"  # the master switch is checked first
    monkeypatch.setattr(limits, "collector_teacher_enabled", True)
    assert (await try_reserve("k-big", tokens=2001)).code == "request_input_cap"
    assert (await try_reserve("k-zero", tokens=0)).code == "request_input_cap"
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(CollectorBudgetReservation)) == 0


@pytest.mark.parametrize("volume", [30, 300])
async def test_parallel_requests_never_exceed_day_calls_and_n_to_10n_is_the_same(limits, volume):
    results = await asyncio.gather(*(try_reserve(f"day-{volume}-{i}") for i in range(volume)))
    admitted = [r for r in results if isinstance(r, budget.Reservation)]
    denied = [r for r in results if isinstance(r, BudgetDenied)]
    assert len(admitted) == 20  # absolute: the same for N and 10N
    assert {d.code for d in denied} == {"day_budget"}
    async with sessionmaker()() as db:
        bucket = await db.get(CollectorBudgetBucket, ("day", budget.budget_day()))
        assert (bucket.reserved_calls, bucket.reserved_tokens) == (20, 20 * 2000)
        assert bucket.reserved_micro_usd == 20 * 500
    snap = await _snapshot()
    assert snap["calls"]["remaining"] == 0 and snap["tokens"]["remaining"] == 20_000


async def _snapshot():
    async with sessionmaker()() as db:
        return await budget.snapshot(db)


async def test_usd_ceiling_binds_before_calls(limits, monkeypatch):
    monkeypatch.setattr(limits, "collector_budget_day_micro_usd", 1_000_000)
    expensive = EngineBound("pricey", True, True, True, 200_000, 600_000)  # 1000 in + 1000 out = 800k
    first = await try_reserve("u1", engine=expensive, tokens=1000)
    assert isinstance(first, budget.Reservation)
    assert (await try_reserve("u2", engine=expensive, tokens=1000)).code == "day_budget"


async def test_job_bucket_limits_calls_per_job_and_jobs_share_the_day(limits):
    for i in range(4):
        assert isinstance(await try_reserve(f"j-{i}", job="job-a", tokens=500), budget.Reservation)
    assert (await try_reserve("j-4", job="job-a", tokens=500)).code == "job_budget"
    assert isinstance(await try_reserve("j-b", job="job-b", tokens=500), budget.Reservation)
    snap = await _snapshot()
    assert snap["calls"]["used"] == 5  # a denied job request consumed nothing


async def test_reservation_is_idempotent_and_conflicting_reuse_is_refused(limits):
    first = await try_reserve("same", tokens=800)
    again = await try_reserve("same", tokens=800)
    assert again.replay and not first.replay
    assert (await _snapshot())["calls"]["used"] == 1
    async with sessionmaker()() as db:
        with pytest.raises(BudgetConflict):
            await budget.reserve(db, request_key="same", purpose="teacher", engine=GOOD, input_tokens=900)


async def test_retries_judges_and_synthetic_generation_are_all_charged(limits):
    for i, purpose in enumerate(("teacher", "teacher", "judge", "synthetic")):
        assert isinstance(await try_reserve(f"p-{i}", purpose=purpose), budget.Reservation)
    assert (await _snapshot())["calls"]["used"] == 4
    assert (await try_reserve("p-x", purpose="bogus")).code == "unknown_purpose"


async def test_success_releases_only_the_difference_and_requires_a_receipt(limits):
    await try_reserve("s1", tokens=1000)  # reserves 2000 tokens
    async with sessionmaker()() as db:
        with pytest.raises(BudgetDenied) as e:
            await budget.settle(db, "s1", CallUsage(tokens=500, micro_usd=1, receipt={}))
        assert e.value.code == "receipt_required"
        await budget.settle(db, "s1", CallUsage(tokens=1500, micro_usd=150_000, receipt={"provider_request": "r1"}))
        await db.commit()
    snap = await _snapshot()
    assert snap["tokens"]["used"] == 1500 and snap["calls"]["used"] == 1
    async with sessionmaker()() as db:  # settling twice with the same numbers is a no-op; different numbers conflict
        await budget.settle(db, "s1", CallUsage(tokens=1500, micro_usd=150_000, receipt={"provider_request": "r1"}))
        with pytest.raises(BudgetConflict):
            await budget.settle(db, "s1", CallUsage(tokens=10, micro_usd=1, receipt={"provider_request": "r1"}))
    assert (await _snapshot())["tokens"]["used"] == 1500


async def test_usage_above_the_reservation_is_charged_and_flagged(limits):
    await try_reserve("over", tokens=1000)
    async with sessionmaker()() as db:
        await budget.settle(db, "over", CallUsage(tokens=2600, micro_usd=1, receipt={"provider_request": "x"}))
        await db.commit()
    async with sessionmaker()() as db:
        row = await db.scalar(select(CollectorBudgetReservation).where(CollectorBudgetReservation.request_key == "over"))
        assert row.receipt["overage"] is True and row.charged_tokens == 2600
    assert (await _snapshot())["tokens"]["used"] == 2600


async def test_failed_or_cancelled_call_keeps_the_full_reservation_until_a_receipt(limits):
    async def boom():
        raise RuntimeError("provider timeout")
    with pytest.raises(RuntimeError):
        await budget.guarded_call(request_key="f1", purpose="teacher", engine=GOOD, input_tokens=1000, call=boom)
    snap = await _snapshot()
    assert (snap["tokens"]["used"], snap["calls"]["used"], snap["unsettled"]) == (2000, 1, 1)
    # a retry under the same key is refused: the first attempt may already have been billed
    with pytest.raises(BudgetDenied) as e:
        await budget.guarded_call(request_key="f1", purpose="teacher", engine=GOOD, input_tokens=1000, call=boom)
    assert e.value.code == "already_attempted"
    # the new retry is a new reservation and is charged as well
    async def never_finishes():
        await asyncio.sleep(30)
    task = asyncio.create_task(budget.guarded_call(request_key="f2", purpose="teacher", engine=GOOD,
                                                   input_tokens=1000, call=never_finishes))
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    snap = await _snapshot()
    assert (snap["tokens"]["used"], snap["calls"]["used"], snap["unsettled"]) == (4000, 2, 2)
    # a provider receipt resolves the unknown and releases only the confirmed difference
    async with sessionmaker()() as db:
        await budget.settle(db, "f1", CallUsage(tokens=0, micro_usd=0, receipt={"provider_confirms": "no usage"}))
        await db.commit()
    assert (await _snapshot())["tokens"]["used"] == 2000


async def test_guarded_call_happy_path_and_orphan_sweep(limits):
    async def ok():
        return "label", CallUsage(tokens=1200, micro_usd=1, receipt={"provider_request": "ok"})
    assert await budget.guarded_call(request_key="g1", purpose="judge", engine=GOOD, input_tokens=1000, call=ok) == "label"
    assert (await _snapshot())["tokens"]["used"] == 1200
    await try_reserve("crash", tokens=1000)  # process died between reserve and settle
    async with sessionmaker()() as db:
        row = await db.scalar(select(CollectorBudgetReservation).where(CollectorBudgetReservation.request_key == "crash"))
        row.created_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
    assert await budget.sweep_orphans(older_than_seconds=60) == 1
    snap = await _snapshot()
    assert snap["tokens"]["used"] == 1200 + 2000 and snap["unsettled"] == 1  # never refunded


async def test_a_call_admitted_before_midnight_is_charged_to_its_own_day(limits):
    kst = budget.KST
    before = datetime(2026, 10, 23, 23, 59, tzinfo=kst)
    after = before + timedelta(minutes=2)
    assert budget.budget_day(before) == "2026-10-23" and budget.budget_day(after) == "2026-10-24"
    r = await try_reserve("m1", tokens=1000, now=before)
    assert r.day == "2026-10-23"
    async with sessionmaker()() as db:
        await budget.settle(db, "m1", CallUsage(tokens=900, micro_usd=1, receipt={"provider_request": "m"}))
        await db.commit()
    async with sessionmaker()() as db:
        old = await db.get(CollectorBudgetBucket, ("day", "2026-10-23"))
        new = await db.get(CollectorBudgetBucket, ("day", "2026-10-24"))
        assert old.reserved_tokens == 900 and new is None
    assert isinstance(await try_reserve("m2", tokens=1000, now=after), budget.Reservation)
    async with sessionmaker()() as db:
        assert (await db.get(CollectorBudgetBucket, ("day", "2026-10-24"))).reserved_calls == 1


async def test_lowering_a_limit_takes_effect_immediately(limits, monkeypatch):
    assert isinstance(await try_reserve("l1"), budget.Reservation)
    monkeypatch.setattr(limits, "collector_budget_day_calls", 1)
    assert (await try_reserve("l2")).code == "day_budget"

pytestmark = pytest.mark.security
