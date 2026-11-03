from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_library import register_text
from ttakkari.kb_pipeline import ExtractingProcessor, StageContext
from ttakkari.kb_pipeline.types import STAGE_EXTRACTING
from ttakkari.kb_pipeline_models import KbPipelineJob
from ttakkari.kb_store import segment_revision


@pytest.fixture(autouse=True)
def pause_default_kb_worker():
    """The session app runs a live kb-pipeline worker that claims registered jobs and rotates their
    attempt token; direct-processor tests would then race it (observed as StaleWriterError flakes)."""
    from ttakkari.kb_pipeline import get_default_worker

    worker = get_default_worker()
    was_paused = worker.claiming_paused
    worker.pause_claiming()
    yield
    if not was_paused:
        worker.resume_claiming()


class BarrierAdapter:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    async def availability(self):
        return True, None

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        # Keep overlapping ExtractingProcessor calls in flight after both have read
        # the finished-stage count. This was the race at the fixed integration SHA.
        await asyncio.sleep(0.05)
        return {"entities": [], "assertions": [], "relations": []}


async def _context(text: str) -> StageContext:
    async with sessionmaker()() as db:
        registered = await register_text(
            db, title="budget race", text=text, source_key=None, lang="ko", locator=None, scope="personal"
        )
        job = await db.scalar(
            select(KbPipelineJob).where(KbPipelineJob.revision_id == registered.revision_id)
        )
        assert job is not None
        job.attempt_token = uuid.uuid4()
        token = job.attempt_token
        await segment_revision(db, registered.revision_id)
        await db.commit()
        return StageContext(
            job_id=job.id,
            document_id=registered.document_id,
            revision_id=registered.revision_id,
            stage=STAGE_EXTRACTING,
            attempt_token=token,
            origin_scope="personal",
            visibility="global",
            session_factory=sessionmaker(),
        )


@pytest.mark.parametrize("adapter_name", ["legacy_cli", "subscription_cli"])
async def test_two_extractors_share_one_daily_call_budget(monkeypatch, adapter_name: str):
    settings = get_settings()
    monkeypatch.setattr(settings, "kb_l2_daily_budget", 1)
    monkeypatch.setattr(settings, "kb_l2_max_calls_per_doc", 1)

    first = await _context("예산 경합을 검증하는 첫 문서입니다. 고유 내용 A.")
    second = await _context("예산 경합을 검증하는 두 번째 문서입니다. 고유 내용 B.")
    adapter = BarrierAdapter(adapter_name)
    processor = ExtractingProcessor(adapter_factory=lambda: adapter)
    read_barrier = asyncio.Barrier(2)
    read_calls_today = processor._l2_calls_today

    async def synchronized_read(ctx):
        value = await read_calls_today(ctx)
        await read_barrier.wait()
        return value

    monkeypatch.setattr(processor, "_l2_calls_today", synchronized_read)

    results = await asyncio.gather(processor(first), processor(second))

    assert adapter.calls == 1
    assert sum(result.detail.get("l2", {}).get("calls_made", 0) for result in results) == 1


async def test_failed_unconsumed_call_releases_slot_and_retry_is_idempotent():
    from ttakkari.kb_pipeline.daily_budget import (
        begin_budgeted_call,
        finish_budgeted_call,
        finish_daily_reservation,
        get_daily_budget_status,
        reserve_daily_budget,
    )

    async with sessionmaker()() as db:
        grant = await reserve_daily_budget(
            db, reservation_key="retry-attempt", requested_calls=1, max_budget=1
        )
        await db.commit()
        assert grant.granted_calls == 1
        duplicate = await reserve_daily_budget(
            db, reservation_key="retry-attempt", requested_calls=1, max_budget=1
        )
        assert duplicate.reservation_id == grant.reservation_id
        assert duplicate.granted_calls == 1
        await db.commit()

        first_call = await begin_budgeted_call(db, grant.reservation_id)
        await db.commit()
        await finish_budgeted_call(db, first_call, consumed=False, error_code="pre_dispatch")
        await db.commit()

        retry_call = await begin_budgeted_call(db, grant.reservation_id)
        await db.commit()
        await finish_budgeted_call(db, retry_call, consumed=True)
        await finish_daily_reservation(db, grant.reservation_id)
        await db.commit()

        used, remaining = await get_daily_budget_status(db, 1)
        assert (used, remaining) == (1, 0)


async def test_crashed_lease_charges_inflight_call_and_releases_unused_capacity():
    from datetime import UTC, datetime, timedelta

    from ttakkari.kb_pipeline.daily_budget import (
        begin_budgeted_call,
        get_daily_budget_status,
        reserve_daily_budget,
    )
    from ttakkari.kb_pipeline_models import KbL2BudgetReservation, KbL2BudgetUsage

    now = datetime.now(UTC).replace(microsecond=0)
    async with sessionmaker()() as db:
        grant = await reserve_daily_budget(
            db,
            reservation_key="crashed-attempt",
            requested_calls=3,
            max_budget=3,
            now=now,
            lease_seconds=1,
        )
        call_id = await begin_budgeted_call(db, grant.reservation_id, now=now)
        await db.commit()

        retry = await reserve_daily_budget(
            db,
            reservation_key="other-attempt",
            requested_calls=3,
            max_budget=3,
            now=now + timedelta(seconds=2),
        )
        assert retry.granted_calls == 2
        reservation = await db.get(KbL2BudgetReservation, grant.reservation_id)
        usage = await db.get(KbL2BudgetUsage, call_id)
        assert reservation.status == "expired"
        assert usage.status == "consumed"
        assert usage.error_code == "lease_expired_outcome_unknown"
        used, remaining = await get_daily_budget_status(
            db, 3, now=now + timedelta(seconds=2)
        )
        assert (used, remaining) == (3, 0)  # one uncertain call + two newly held calls
        await db.rollback()


async def test_finished_stage_call_is_not_double_counted_with_its_ledger_row():
    from ttakkari.kb_pipeline.daily_budget import (
        begin_budgeted_call,
        finish_budgeted_call,
        finish_daily_reservation,
        reserve_daily_budget,
    )

    async with sessionmaker()() as db:
        first = await reserve_daily_budget(
            db, reservation_key="completed-stage-1", requested_calls=1, max_budget=2
        )
        call_id = await begin_budgeted_call(db, first.reservation_id)
        await finish_budgeted_call(db, call_id, consumed=True)
        await finish_daily_reservation(db, first.reservation_id)
        await db.commit()

        # Stage details now report one completed call; the same call is already
        # represented by the immutable usage ledger and must not be counted twice.
        next_stage = await reserve_daily_budget(
            db,
            reservation_key="completed-stage-2",
            requested_calls=1,
            max_budget=2,
            already_used=1,
        )
        assert next_stage.granted_calls == 1


async def test_cancelled_inflight_call_stays_charged_on_attempt_resume():
    from ttakkari.kb_pipeline.daily_budget import (
        begin_budgeted_call,
        finish_daily_reservation,
        get_daily_budget_status,
        reserve_daily_budget,
    )
    from ttakkari.kb_pipeline_models import KbL2BudgetReservation, KbL2BudgetUsage

    async with sessionmaker()() as db:
        first = await reserve_daily_budget(
            db, reservation_key="cancelled-attempt", requested_calls=1, max_budget=1
        )
        call_id = await begin_budgeted_call(db, first.reservation_id)
        await db.commit()
        await finish_daily_reservation(db, first.reservation_id, cancelled=True)
        await db.commit()

        resumed = await reserve_daily_budget(
            db, reservation_key="resumed-attempt", requested_calls=1, max_budget=1
        )
        assert resumed.granted_calls == 0
        closed = await db.get(KbL2BudgetReservation, first.reservation_id)
        usage = await db.get(KbL2BudgetUsage, call_id)
        assert closed.status == "cancelled"
        assert usage.status == "consumed"
        assert usage.error_code == "cancelled_outcome_unknown"
        # A duplicate delivery of the cancelled attempt finds its closed reservation and
        # must not allocate again.
        duplicate = await reserve_daily_budget(
            db, reservation_key="cancelled-attempt", requested_calls=1, max_budget=1
        )
        assert duplicate.reservation_id == first.reservation_id
        assert duplicate.granted_calls == 0
        used, remaining = await get_daily_budget_status(db, 1)
        assert (used, remaining) == (1, 0)


class _Unconsumed(RuntimeError):
    diagnosis = "pre_dispatch"
    consumed = False


class ScriptedAdapter:
    def __init__(self, failure: BaseException | None = None) -> None:
        self.failure = failure
        self.calls = 0

    async def availability(self):
        return True, None

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return {"entities": [], "assertions": [], "relations": []}


async def _ledger_counts() -> dict[str, int]:
    from sqlalchemy import func

    from ttakkari.kb_pipeline_models import KbL2BudgetUsage

    async with sessionmaker()() as db:
        rows = await db.execute(
            select(KbL2BudgetUsage.status, func.count()).group_by(KbL2BudgetUsage.status)
        )
        return {status: count for status, count in rows}


async def test_unconsumed_failure_at_budget_one_leaves_slot_for_next_document(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "kb_l2_daily_budget", 1)
    monkeypatch.setattr(settings, "kb_l2_max_calls_per_doc", 1)
    first = await _context("실패해도 예산을 쓰지 않는 문서 A. 고유 내용 C.")
    second = await _context("뒤이어 정상 호출되는 문서 B. 고유 내용 D.")
    failing = ScriptedAdapter(_Unconsumed("never dispatched"))
    healthy = ScriptedAdapter()

    first_result = await ExtractingProcessor(adapter_factory=lambda: failing)(first)
    second_result = await ExtractingProcessor(adapter_factory=lambda: healthy)(second)

    assert first_result.detail["l2"]["calls_made"] == 0
    assert second_result.detail["l2"]["calls_made"] == 1
    assert healthy.calls == 1
    assert await _ledger_counts() == {"released": 1, "consumed": 1}


async def test_cancelled_call_at_budget_one_stays_charged_after_resume(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "kb_l2_daily_budget", 1)
    monkeypatch.setattr(settings, "kb_l2_max_calls_per_doc", 1)
    first = await _context("취소되는 호출 문서 A. 고유 내용 E.")
    cancelled = ScriptedAdapter(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await ExtractingProcessor(adapter_factory=lambda: cancelled)(first)

    # Resume gets a fresh attempt token; the cancelled call may have reached the
    # provider, so it stays charged and the single slot is not handed out again.
    async with sessionmaker()() as db:
        job = await db.get(KbPipelineJob, first.job_id)
        job.attempt_token = uuid.uuid4()
        await db.commit()
        resumed_ctx = StageContext(**{**first.__dict__, "attempt_token": job.attempt_token})
    resumed = ScriptedAdapter()
    result = await ExtractingProcessor(adapter_factory=lambda: resumed)(resumed_ctx)

    assert resumed.calls == 0
    assert result.detail["l2"]["calls_made"] == 0
    assert await _ledger_counts() == {"consumed": 1}


async def test_redelivered_attempt_does_not_double_call_or_double_count(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "kb_l2_daily_budget", 2)
    monkeypatch.setattr(settings, "kb_l2_max_calls_per_doc", 1)
    ctx = await _context("같은 시도가 두 번 배달되는 문서. 고유 내용 F.")
    adapter = ScriptedAdapter()
    processor = ExtractingProcessor(adapter_factory=lambda: adapter)

    await processor(ctx)
    await processor(ctx)

    assert adapter.calls == 1
    assert await _ledger_counts() == {"consumed": 1}
