"""L2 daily budget boundaries through the real ExtractingProcessor path (disposable PG)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from test_kb_v3_l2_budget_concurrency import (  # noqa: F401  (pause_default_kb_worker is an autouse fixture)
    pause_default_kb_worker,
    ScriptedAdapter,
    _Unconsumed,
    _context,
    _ledger_counts,
)
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_pipeline import ExtractingProcessor
from ttakkari.kb_pipeline.daily_budget import (
    DailyBudgetExhausted,
    begin_budgeted_call,
    reserve_daily_budget,
)
from ttakkari.kb_pipeline.subscription_transport import (
    DIAG_TIMEOUT,
    L2TransportError,
    ScopeCapacityUnavailableError,
)
from ttakkari.kb_pipeline_models import KbL2BudgetReservation, KbPipelineStage

MULTI_PASSAGE_TEXT = "\n\n".join(
    f"# 개요 {i}\n\n" + (f"예산 경계 시험용 문단 {i}. " * 40) for i in range(6)
)


def _budget(monkeypatch, daily: int, per_doc: int = 8) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "kb_l2_daily_budget", daily)
    monkeypatch.setattr(settings, "kb_l2_max_calls_per_doc", per_doc)


async def _passage_count(ctx) -> int:
    from ttakkari.kb_models import KbPassage  # noqa: PLC0415

    async with sessionmaker()() as db:
        return int(
            await db.scalar(
                select(func.count()).select_from(KbPassage).where(KbPassage.revision_id == ctx.revision_id)
            )
            or 0
        )


async def _record_finished_stage(ctx, result) -> None:
    """The pipeline runner persists processor detail; calling the processor directly must mimic it."""
    from ttakkari.kb_pipeline.types import STAGE_EXTRACTING  # noqa: PLC0415

    async with sessionmaker()() as db:
        row = await db.scalar(
            select(KbPipelineStage).where(
                KbPipelineStage.job_id == ctx.job_id, KbPipelineStage.stage == STAGE_EXTRACTING
            )
        )
        if row is None:
            row = KbPipelineStage(job_id=ctx.job_id, stage=STAGE_EXTRACTING)
            db.add(row)
        row.status = "succeeded"
        row.finished_at = datetime.now(UTC)
        row.detail = result.detail
        await db.commit()


async def test_budget_one_single_run_makes_exactly_one_call(monkeypatch):
    _budget(monkeypatch, 1, per_doc=1)
    ctx = await _context("예산 1에서 단독 실행하는 문서. 고유 내용 G.")
    adapter = ScriptedAdapter()

    result = await ExtractingProcessor(adapter_factory=lambda: adapter)(ctx)

    assert adapter.calls == 1
    assert result.detail["l2"]["calls_made"] == 1
    # The post-reservation remainder is observability only and must not cap the call.
    assert result.detail["l2"]["daily_budget_remaining_after_reservation"] == 0
    assert await _ledger_counts() == {"consumed": 1}


async def test_per_document_allowance_is_separate_from_global_remainder(monkeypatch):
    _budget(monkeypatch, 3, per_doc=5)
    ctx = await _context(MULTI_PASSAGE_TEXT)
    passages = await _passage_count(ctx)
    assert passages >= 4, "fixture must yield more passages than the daily budget"
    adapter = ScriptedAdapter()

    result = await ExtractingProcessor(adapter_factory=lambda: adapter)(ctx)

    assert adapter.calls == 3
    assert result.detail["l2"]["calls_made"] == 3
    assert result.detail["l2"]["daily_budget_remaining_after_reservation"] == 0


async def test_zero_budget_never_calls_and_writes_no_ledger(monkeypatch):
    _budget(monkeypatch, 0, per_doc=2)
    ctx = await _context("예산 0 문서. 고유 내용 H.")
    adapter = ScriptedAdapter()

    result = await ExtractingProcessor(adapter_factory=lambda: adapter)(ctx)

    assert adapter.calls == 0
    assert result.detail["l2"]["calls_made"] == 0
    assert await _ledger_counts() == {}
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbL2BudgetReservation)) == 0


async def test_finished_calls_are_counted_once_across_consecutive_documents(monkeypatch):
    # Finished stage detail and the usage ledger describe the same call. Counting both
    # would deny the second document under a budget of 2.
    _budget(monkeypatch, 2, per_doc=1)
    adapters = [ScriptedAdapter() for _ in range(3)]
    for index, adapter in enumerate(adapters):
        ctx = await _context(f"연속 문서 {index}. 고유 내용 I{index}.")
        result = await ExtractingProcessor(adapter_factory=lambda adapter=adapter: adapter)(ctx)
        await _record_finished_stage(ctx, result)

    assert [a.calls for a in adapters] == [1, 1, 0]
    assert await _ledger_counts() == {"consumed": 2}
    async with sessionmaker()() as db:
        stage_calls = [
            (row.detail or {}).get("l2", {}).get("calls_made", 0)
            for row in await db.scalars(select(KbPipelineStage))
        ]
    assert sum(stage_calls) == 2


async def test_timeout_is_charged_but_account_refusal_is_not(monkeypatch):
    _budget(monkeypatch, 2, per_doc=1)
    timed_out = ScriptedAdapter(L2TransportError(DIAG_TIMEOUT, "cli timed out"))
    refused = ScriptedAdapter(ScopeCapacityUnavailableError("no account"))
    healthy = ScriptedAdapter()
    for index, adapter in enumerate((timed_out, refused, healthy)):
        ctx = await _context(f"타임아웃과 거부 문서 {index}. 고유 내용 J{index}.")
        await ExtractingProcessor(adapter_factory=lambda adapter=adapter: adapter)(ctx)

    assert healthy.calls == 1
    # timeout: provider may have run (consumed); refusal: never dispatched (released).
    assert await _ledger_counts() == {"consumed": 2, "released": 1}


async def test_unavailable_adapter_reserves_nothing(monkeypatch):
    _budget(monkeypatch, 1, per_doc=1)

    class Unavailable(ScriptedAdapter):
        async def availability(self):
            return False, "scope_capacity_unavailable"

    adapter = Unavailable()
    ctx = await _context("가용 계정 없는 문서. 고유 내용 K.")
    result = await ExtractingProcessor(adapter_factory=lambda: adapter)(ctx)

    assert adapter.calls == 0
    assert result.detail["l2"]["calls_made"] == 0
    assert await _ledger_counts() == {}
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbL2BudgetReservation)) == 0


async def test_utc_day_change_gets_fresh_budget_and_fences_stale_lease():
    day_one = datetime(2026, 10, 25, 23, 59, 30, tzinfo=UTC)
    day_two = day_one + timedelta(seconds=60)
    suffix = uuid.uuid4().hex
    async with sessionmaker()() as db:
        late = await reserve_daily_budget(
            db, reservation_key=f"late-{suffix}", requested_calls=1, max_budget=1, now=day_one
        )
        await db.commit()
        assert late.granted_calls == 1
        row = await db.get(KbL2BudgetReservation, late.reservation_id)
        assert row.lease_expires_at == datetime(2026, 10, 26, tzinfo=UTC)

        # The lease never carries yesterday's allowance into the new UTC day.
        with pytest.raises(DailyBudgetExhausted):
            await begin_budgeted_call(db, late.reservation_id, now=day_two)
        await db.rollback()

        fresh = await reserve_daily_budget(
            db, reservation_key=f"fresh-{suffix}", requested_calls=1, max_budget=1, now=day_two
        )
        assert fresh.granted_calls == 1
        await db.commit()
        # Yesterday's unfinished lease expired without any in-flight call to charge.
        stale = await db.get(KbL2BudgetReservation, late.reservation_id)
        assert stale.status == "expired"


async def test_pre_ledger_stage_baseline_blocks_the_budget():
    suffix = uuid.uuid4().hex
    async with sessionmaker()() as db:
        grant = await reserve_daily_budget(
            db, reservation_key=f"baseline-{suffix}", requested_calls=1, max_budget=1, already_used=1
        )
        await db.commit()
    assert grant.granted_calls == 0
