"""CP-S2-02: collector job claim, attempt-token fence, page commit, cancellation, orphan recovery and the consume batch.

PostgreSQL 은 tests/conftest.py 의 테스트 DB 다(운영 DB 거부). SKIP LOCKED 경합은 서로 다른 세션으로 동시에 돌린다.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from ttakkari.collector import jobs, runner
from ttakkari.collector.contracts import (
    AttemptFencedError,
    AuthContext,
    CollectorConflictError,
    JobNotFoundError,
    JobStateError,
)
from ttakkari.collector.intake import submit_envelope
from ttakkari.collector.jobs import OutboxItem
from ttakkari.collector_models import CollectorJob, CollectorOutbox
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import RuntimeGeneration

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "docs/design/collector-platform/contracts/fixtures"
AUTH = AuthContext(tenant_id="fixture-tenant", scope="personal", principal_id="fixture-owner")
T0 = datetime(2026, 10, 21, 3, 0, tzinfo=UTC)


async def make_job(n: int = 0) -> uuid.UUID:
    env = json.loads((FIXTURES_DIR / "valid-accept.json").read_text(encoding="utf-8"))
    env["envelope_id"] = str(uuid.uuid4())
    async with sessionmaker()() as db:
        result = await submit_envelope(db, AUTH, f"job-{n}-{uuid.uuid4().hex[:6]}", env)
        await db.commit()
    return result.job_id


async def claim(owner: str = "gen-a", limit: int = 1, now: datetime | None = None):
    async with sessionmaker()() as db:
        claimed = await jobs.claim_batch(db, owner_generation=owner, limit=limit, now=now)
        await db.commit()
    return claimed


async def row(job_id: uuid.UUID) -> CollectorJob:
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, job_id)
        db.expunge(job)
        return job


async def finish(c, **kw):
    async with sessionmaker()() as db:
        stored = await jobs.finish(db, c.id, c.attempt_token, **kw)
        await db.commit()
    return stored


async def test_claim_sets_owner_token_attempt_and_runs_each_job_once():
    ids = {await make_job(i) for i in range(3)}

    async def claimer(i):
        return await claim(f"gen-{i}", limit=1)

    results = await asyncio.gather(*(claimer(i) for i in range(10)))
    claimed = [c for batch in results for c in batch]
    assert {c.id for c in claimed} == ids
    assert len(claimed) == 3  # 10 경합자, 3 job: SKIP LOCKED 라 중복 claim 0
    assert len({c.attempt_token for c in claimed}) == 3
    for c in claimed:
        stored = await row(c.id)
        assert (stored.status, stored.attempts, stored.owner_generation) == ("running", 1, c.owner_generation)
        assert stored.attempt_token == c.attempt_token and stored.heartbeat_at is not None


async def test_stale_or_wrong_token_is_fenced_and_changes_nothing():
    job_id = await make_job()
    (c,) = await claim()
    async with sessionmaker()() as db:
        with pytest.raises(AttemptFencedError):
            await jobs.commit_page(db, job_id, uuid.uuid4(), cursor={"page": 9})
        with pytest.raises(AttemptFencedError):
            await jobs.finish(db, job_id, uuid.uuid4(), status="succeeded")
        with pytest.raises(AttemptFencedError):
            await jobs.heartbeat(db, job_id, uuid.uuid4())
    stored = await row(job_id)
    assert stored.status == "running" and stored.cursor is None and stored.attempt_token == c.attempt_token

    assert await finish(c, status="succeeded") == "succeeded"
    # 끝난 attempt 의 늦은 commit 도 막힌다
    async with sessionmaker()() as db:
        with pytest.raises(AttemptFencedError):
            await jobs.commit_page(db, job_id, c.attempt_token, cursor={"page": 1})
    assert (await row(job_id)).attempt_token is None


async def test_page_commit_moves_cursor_and_outbox_together():
    job_id = await make_job()
    (c,) = await claim()
    child = OutboxItem(sink="export", envelope_id=uuid.uuid4(), payload_hash="a" * 64)
    async with sessionmaker()() as db:
        assert await jobs.commit_page(db, job_id, c.attempt_token, cursor={"page": 1}, stage="fetch", outbox=[child]) == "continue"
        await db.commit()
    stored = await row(job_id)
    assert stored.cursor == {"page": 1} and stored.stage == "fetch"
    async with sessionmaker()() as db:
        sinks = sorted((await db.scalars(select(CollectorOutbox.sink).where(CollectorOutbox.job_id == job_id))).all())
    assert sinks == ["default", "export"]


async def test_failed_page_leaves_neither_cursor_nor_outbox():
    job_id = await make_job()
    (c,) = await claim()
    child = OutboxItem(sink="export", envelope_id=uuid.uuid4(), payload_hash="a" * 64)
    async with sessionmaker()() as db:
        await jobs.commit_page(db, job_id, c.attempt_token, cursor={"page": 1}, outbox=[child])
        await db.commit()

    # 같은 sink/envelope 의 중복 outbox 는 cursor 이동까지 되돌린다
    async with sessionmaker()() as db:
        with pytest.raises(CollectorConflictError):
            await jobs.commit_page(db, job_id, c.attempt_token, cursor={"page": 2}, outbox=[child])
        await db.commit()
    assert (await row(job_id)).cursor == {"page": 1}

    # fence 실패(토큰 불일치)는 outbox 를 하나도 만들지 않는다
    other = OutboxItem(sink="export", envelope_id=uuid.uuid4(), payload_hash="b" * 64)
    async with sessionmaker()() as db:
        with pytest.raises(AttemptFencedError):
            await jobs.commit_page(db, job_id, uuid.uuid4(), cursor={"page": 3}, outbox=[other])
        await db.commit()
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count(CollectorOutbox.id)).where(CollectorOutbox.job_id == job_id)) == 2

    # 호출자가 rollback 하면 같은 transaction 의 page 전체가 사라진다
    async with sessionmaker()() as db:
        await jobs.commit_page(db, job_id, c.attempt_token, cursor={"page": 4},
                               outbox=[OutboxItem("export", uuid.uuid4(), "c" * 64)])
        await db.rollback()
    assert (await row(job_id)).cursor == {"page": 1}


async def test_cancel_queued_is_immediate_and_running_stops_at_page_boundary():
    queued = await make_job(1)
    running = await make_job(2)
    async with sessionmaker()() as db:
        assert await jobs.cancel(db, "fixture-tenant", "personal", queued) == "cancelled"
        await db.commit()
    assert (await row(queued)).status == "cancelled"

    claimed = await claim(limit=5)  # 취소된 job 은 claim 되지 않는다
    assert [x.id for x in claimed] == [running]
    (c,) = claimed
    async with sessionmaker()() as db:
        assert await jobs.cancel(db, "fixture-tenant", "personal", running) == "cancelling"
        await db.commit()
    assert (await row(running)).status == "running"
    async with sessionmaker()() as db:
        assert await jobs.commit_page(db, running, c.attempt_token, cursor={"page": 1}) == "cancelled"
        await db.commit()
    stored = await row(running)
    assert stored.status == "cancelled" and stored.attempt_token is None and stored.last_error["code"] == "cancelled"
    with pytest.raises(AttemptFencedError):
        async with sessionmaker()() as db:
            await jobs.finish(db, running, c.attempt_token, status="succeeded")


async def test_cancel_wins_over_retry_wait_at_finish():
    job_id = await make_job()
    (c,) = await claim()
    async with sessionmaker()() as db:
        await jobs.cancel(db, "fixture-tenant", "personal", job_id)
        await db.commit()
    assert await finish(c, status="retry_wait", error={"code": "http_503"}) == "cancelled"


@pytest.mark.parametrize(("ending", "stored", "finished_as"), [
    ("succeeded", "cancelled", "succeeded"), ("partial", "cancelled", "partial"), ("budget_wait", "cancelled", None),
    ("failed", "failed", None), ("policy_blocked", "policy_blocked", None), ("quarantined", "quarantined", None),
])
async def test_cancel_recorded_before_the_attempt_ends_wins_even_if_the_work_finished(ending, stored, finished_as):
    """S2-02 verification counterexample: cancel_requested set first, then the handler ends 'succeeded'.
    The caller was told 'cancelling', so the job ends cancelled. Real failures are not rewritten."""
    job_id = await make_job()
    (c,) = await claim()
    async with sessionmaker()() as db:
        assert await jobs.cancel(db, "fixture-tenant", "personal", job_id) == "cancelling"
        await db.commit()
    assert await finish(c, status=ending, error={"code": "x"} if ending in ("failed", "policy_blocked", "quarantined") else None) == stored
    final = await row(job_id)
    assert final.status == stored and final.attempt_token is None
    if stored == "cancelled":
        assert final.last_error["code"] == "cancelled" and final.last_error.get("finished_as") == finished_as


async def test_consume_cancel_after_the_last_page_commit_still_ends_cancelled():
    job_id = await make_job()

    async def handler(job, ctx):
        assert await ctx.commit_page({"page": 1}) == "continue"  # last boundary passes: no cancel yet
        async with sessionmaker()() as db:  # cancel lands after it, before the attempt ends
            await jobs.cancel(db, job.tenant_id, job.scope, job.id)
            await db.commit()
        return runner.HandlerResult("succeeded")

    runner.register_handler(handler)
    assert await runner.consume_once(limit=1) == 1
    final = await row(job_id)
    assert final.status == "cancelled" and final.last_error["finished_as"] == "succeeded"


async def test_cancel_other_scope_or_finished_job():
    job_id = await make_job()
    async with sessionmaker()() as db:
        with pytest.raises(JobNotFoundError):
            await jobs.cancel(db, "fixture-tenant", "work", job_id)
        with pytest.raises(JobNotFoundError):
            await jobs.cancel(db, "other-tenant", "personal", job_id)
    (c,) = await claim()
    await finish(c, status="succeeded")
    async with sessionmaker()() as db:
        with pytest.raises(JobStateError):
            await jobs.cancel(db, "fixture-tenant", "personal", job_id)


async def test_retry_wait_backoff_is_durable_and_attempts_are_capped():
    job_id = await make_job()
    now = T0
    for attempt in (1, 2):
        (c,) = await claim(now=now)
        assert c.attempts == attempt
        stored = await finish(c, status="retry_wait", error={"code": "http_503"}, now=now)
        assert stored == "retry_wait"
        job = await row(job_id)
        assert job.not_before is not None and job.not_before > now and job.attempt_token is None
        assert await claim(now=now) == []  # not_before 전에는 claim 되지 않는다
        now = job.not_before + timedelta(seconds=1)
    (c,) = await claim(now=now)
    assert c.attempts == 3
    assert await finish(c, status="retry_wait", error={"code": "http_503"}, now=now) == "failed"
    job = await row(job_id)
    assert job.status == "failed" and job.last_error["code"] == "attempts_exhausted"
    assert await claim(now=now + timedelta(days=1)) == []  # 무한 재시도 없음


def test_retry_delay_is_exponential_jittered_and_honors_retry_after():
    rng = random.Random(7)
    first, second, third = (jobs.retry_delay(n, rng=rng).total_seconds() for n in (1, 2, 3))
    assert 30 <= first <= 36 and 60 <= second <= 72 and 120 <= third <= 144
    assert jobs.retry_delay(30, rng=rng).total_seconds() <= 3600 * 1.2
    assert jobs.retry_delay(1, retry_after=900, rng=rng).total_seconds() == 900


@pytest.mark.parametrize("status", ["policy_blocked", "quarantined", "partial", "budget_wait"])
async def test_non_retry_outcomes_are_not_reclaimed(status):
    job_id = await make_job()
    (c,) = await claim()
    assert await finish(c, status=status, error={"code": status}) == status
    assert await claim(now=T0 + timedelta(days=30)) == []
    assert (await row(job_id)).status == status


async def test_finish_rejects_unknown_status():
    await make_job()
    (c,) = await claim()
    async with sessionmaker()() as db:
        with pytest.raises(JobStateError):
            await jobs.finish(db, c.id, c.attempt_token, status="queued")


# ---- 복구: heartbeat 나이만으로 살아 있는 세대의 job 을 빼앗지 않는다 --------------------------------------------------

async def age_heartbeat(job_id: uuid.UUID, minutes: int = 30) -> None:
    async with sessionmaker()() as db:
        job = await db.get(CollectorJob, job_id)
        job.heartbeat_at = datetime.now(UTC) - timedelta(minutes=minutes)
        await db.commit()


async def recover(states: dict[str, str], stale: timedelta = timedelta(minutes=5)):
    async with sessionmaker()() as db:
        report = await jobs.recover_orphans(db, owner_state=lambda o: states.get(o, "unknown"), stale_after=stale)
        await db.commit()
    return report


async def test_recovery_needs_confirmed_offline_owner():
    for i in range(4):
        await make_job(i)
    claimed = {}
    for owner in ("gen-alive", "gen-unknown", "gen-offline", "gen-fresh"):
        (c,) = await claim(owner)
        claimed[owner] = c
    ids = {o: c.id for o, c in claimed.items()}
    for owner in ("gen-alive", "gen-unknown", "gen-offline"):
        await age_heartbeat(ids[owner])

    report = await recover({"gen-alive": "alive", "gen-offline": "offline"})  # gen-unknown 은 상태 불명
    assert report.recovered == [ids["gen-offline"]]
    assert report.alive == [ids["gen-alive"]]
    assert report.held == [ids["gen-unknown"]]
    assert ids["gen-fresh"] not in report.recovered + report.held + report.alive  # heartbeat 가 최근이면 후보도 아니다

    assert (await row(ids["gen-alive"])).status == "running"
    assert (await row(ids["gen-unknown"])).status == "running"
    recovered = await row(ids["gen-offline"])
    assert recovered.status == "retry_wait" and recovered.attempt_token is None
    assert recovered.last_error["code"] == "owner_lost" and recovered.owner_generation is None

    # 이전 소유자가 뒤늦게 commit 해도 fence 에 막힌다
    old = claimed["gen-offline"]
    async with sessionmaker()() as db:
        with pytest.raises(AttemptFencedError):
            await jobs.commit_page(db, old.id, old.attempt_token, cursor={"page": 5})
    # 복구된 job 은 새 attempt 로 다시 claim 된다
    (again,) = await claim("gen-new")
    assert again.id == old.id and again.attempts == 2 and again.attempt_token != old.attempt_token


async def test_recovery_of_exhausted_job_fails_it_instead_of_retrying():
    job_id = await make_job()
    now = T0
    for _ in range(3):
        (c,) = await claim("gen-dead", now=now)
        if c.attempts < 3:
            await finish(c, status="retry_wait", now=now)
            now = (await row(job_id)).not_before + timedelta(seconds=1)
    await age_heartbeat(job_id)
    report = await recover({"gen-dead": "offline"})
    assert report.recovered == [job_id]
    stored = await row(job_id)
    assert stored.status == "failed" and stored.last_error["code"] == "attempts_exhausted"


# ---- owner 상태 판정 ---------------------------------------------------------------------------------------------

def _dead_pid() -> int:
    """이미 종료된 프로세스의 pid. 재사용될 수 있으나 테스트 시간 안에서는 거의 없다."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


async def test_owner_state_resolution():
    dead_pid = _dead_pid()
    gone_id, offline_id, standby_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessionmaker()() as db:
        for gid, state in ((offline_id, "offline"), (standby_id, "standby")):
            db.add(RuntimeGeneration(id=gid, revision="r", api_url=f"http://127.0.0.1:{gid.int % 1000 + 9000}", pid=1,
                                     process_started_at=0.0, state=state))
        await db.commit()
    states = await runner._owner_states(
        [f"single-{os.getpid()}", f"single-{dead_pid}", "single-x", "not-a-uuid", str(gone_id), str(offline_id), str(standby_id)])
    assert states[f"single-{os.getpid()}"] == "alive"
    assert states[f"single-{dead_pid}"] == "offline"
    assert states["single-x"] == states["not-a-uuid"] == states[str(gone_id)] == "unknown"
    assert states[str(offline_id)] == "offline"
    assert states[str(standby_id)] == "offline"  # 상태는 standby 인데 liveness lock 을 아무도 잡지 않는다


async def test_owner_with_held_liveness_lock_is_alive():
    from ttakkari import generation

    gid = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(RuntimeGeneration(id=gid, revision="r", api_url="http://127.0.0.1:9777", pid=1, process_started_at=0.0,
                                 state="active"))
        await db.commit()
    handle = generation._lock(get_settings().home / "runtime" / f"{gid}.lock")
    assert handle is not None
    try:
        assert (await runner._owner_states([str(gid)]))[str(gid)] == "alive"
    finally:
        handle.close()
    assert (await runner._owner_states([str(gid)]))[str(gid)] == "offline"


# ---- consume batch ----------------------------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_handler():
    runner.register_handler(None)
    yield
    runner.register_handler(None)


async def test_consume_without_handler_claims_nothing():
    job_id = await make_job()
    assert await runner.consume_once() == 0
    stored = await row(job_id)
    assert stored.status == "queued" and stored.attempts == 0


async def test_consume_runs_handler_pages_and_finishes():
    first, second = await make_job(1), await make_job(2)
    seen = []

    async def handler(job, ctx):
        seen.append(job.id)
        assert await ctx.commit_page({"page": 1}, stage="fetch") == "continue"
        return runner.HandlerResult("succeeded")

    runner.register_handler(handler)
    assert await runner.consume_once(limit=5) == 2
    assert set(seen) == {first, second}
    for job_id in (first, second):
        stored = await row(job_id)
        assert (stored.status, stored.cursor, stored.stage, stored.attempt_token) == ("succeeded", {"page": 1}, "fetch", None)


async def test_consume_handler_error_retries_within_attempt_cap():
    job_id = await make_job()

    async def boom(job, ctx):
        raise RuntimeError("parser exploded")

    runner.register_handler(boom)
    assert await runner.consume_once(limit=1) == 1
    stored = await row(job_id)
    assert stored.status == "retry_wait" and stored.last_error == {"code": "handler_error", "type": "RuntimeError"}
    assert stored.not_before > datetime.now(UTC)


async def test_consume_stops_claiming_once_the_generation_is_draining(monkeypatch):
    job_id = await make_job()
    calls = []

    async def handler(job, ctx):
        calls.append(job.id)
        return runner.HandlerResult("succeeded")

    runner.register_handler(handler)
    monkeypatch.setattr(runner, "accepting", lambda: False)
    assert await runner.consume_once(limit=5) == 0
    assert calls == []
    stored = await row(job_id)
    assert stored.status == "queued" and stored.attempts == 0


async def test_drain_mid_batch_finishes_current_job_then_stops(monkeypatch):
    first, second = await make_job(1), await make_job(2)
    state = {"accept": True}

    async def handler(job, ctx):
        state["accept"] = False  # 첫 job 처리 중 세대가 draining 으로 바뀐다
        return runner.HandlerResult("succeeded")

    runner.register_handler(handler)
    monkeypatch.setattr(runner, "accepting", lambda: state["accept"])
    assert await runner.consume_once(limit=5) == 1
    statuses = {j: (await row(j)).status for j in (first, second)}
    assert sorted(statuses.values()) == ["queued", "succeeded"]


async def test_consume_cancel_during_handler_is_respected():
    job_id = await make_job()

    async def handler(job, ctx):
        async with sessionmaker()() as db:
            await jobs.cancel(db, job.tenant_id, job.scope, job.id)
            await db.commit()
        assert await ctx.commit_page({"page": 1}) == "cancelled"
        return runner.HandlerResult("succeeded")  # 취소된 attempt 의 finish 는 fence 에 막혀 무시된다

    runner.register_handler(handler)
    assert await runner.consume_once(limit=1) == 1
    assert (await row(job_id)).status == "cancelled"


async def test_consume_recovers_orphans_of_dead_single_process_before_claiming():
    job_id = await make_job()
    (c,) = await claim(f"single-{_dead_pid()}")
    await age_heartbeat(c.id)
    ran = []

    async def handler(job, ctx):
        ran.append(job.id)
        return runner.HandlerResult("succeeded")

    runner.register_handler(handler)
    assert await runner.consume_once(limit=2) == 1
    assert ran == [job_id]
    stored = await row(job_id)
    assert stored.status == "succeeded" and stored.attempts == 2


def test_collector_consume_is_registered_only_when_enabled():
    from ttakkari import generation_service

    names = lambda **kw: {n for n, _, _ in generation_service.maintenance_jobs(get_settings().model_copy(update=kw))}
    assert "collector-consume" not in names()
    assert "collector-consume" in names(collector_consume_enabled=True)
    assert get_settings().collector_consume_enabled is False


async def test_get_job_is_scoped():
    job_id = await make_job()
    async with sessionmaker()() as db:
        assert (await jobs.get_job(db, "fixture-tenant", "personal", job_id)).id == job_id
        with pytest.raises(JobNotFoundError):
            await jobs.get_job(db, "fixture-tenant", "work", job_id)
