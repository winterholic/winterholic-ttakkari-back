from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.generation import GenerationRuntime, OwnershipLost, activate
from ttakkari.models import Run, RuntimeCommand, RuntimeLease


@asynccontextmanager
async def generations(tmp_path):
    a = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    b = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
    await a.register()
    await b.register()
    try:
        yield a, b
    finally:
        await a.close()
        await b.close()


async def queued(new_session):
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                  prompt="generation test", engine="claude")
        db.add(run)
        await db.commit()
        return run


async def test_standby_does_not_recover_another_generations_live_run(tmp_path, new_session):
    run = await queued(new_session)
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        assert await a.claim(run.id, "workers", 1)
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            row.status = "running"
            row.pid = 12345
            await db.commit()
        assert await b.claim(run.id, "workers", 1) is None
        assert (await b.snapshot())["state"] == "standby"
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            assert row.status == "running" and row.pid == 12345


async def test_cutover_blocks_old_claims_and_keeps_existing_ownership(tmp_path, new_session):
    first, second = await queued(new_session), await queued(new_session)
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        token = await a.claim(first.id, "workers", 2)
        assert token
        await activate(b.id, expected=a.id)
        assert await a.claim(second.id, "workers", 2) is None
        assert await b.claim(first.id, "workers", 2) is None
        assert await b.claim(second.id, "workers", 2)
        async with sessionmaker()() as db:
            await a.fence(db, first.id, token)
        assert (await a.snapshot())["owned_runs"] == 1
        assert not await a.can_retire()
        await a.release(first.id, token)
        assert await a.can_retire()


async def test_lane_limit_is_global_across_draining_and_active_generations(tmp_path, new_session):
    first, second = await queued(new_session), await queued(new_session)
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        token = await a.claim(first.id, "workers", 1)
        await activate(b.id, expected=a.id)
        assert await b.claim(second.id, "workers", 1) is None
        await a.release(first.id, token)
        results = await asyncio.gather(*(b.claim(second.id, "workers", 1) for _ in range(8)))
        assert sum(x is not None for x in results) == 1


async def test_session_order_survives_cutover_and_released_token_cannot_write(tmp_path, new_session):
    first = await queued(new_session)
    async with sessionmaker()() as db:
        second = Run(session_id=first.session_id, workspace_id=first.workspace_id,
                     prompt="next", engine="claude")
        db.add(second)
        await db.commit()
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        token = await a.claim(first.id, "workers", 8)
        await activate(b.id, expected=a.id)
        assert await b.claim(second.id, "workers", 8) is None
        await a.release(first.id, token)
        async with sessionmaker()() as db:
            row = await db.get(Run, first.id)
            row.status = "succeeded"
            await db.commit()
        assert await b.claim(second.id, "workers", 8)
        async with sessionmaker()() as db:
            with pytest.raises(OwnershipLost):
                await a.fence(db, first.id, token)


async def test_activation_compare_and_swap_rejects_stale_deployer(tmp_path):
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        with pytest.raises(ValueError, match="active"):
            await activate(b.id, expected=None)
        assert (await a.snapshot())["state"] == "active"


async def test_commands_route_to_owner_and_deduplicate(tmp_path, new_session):
    run = await queued(new_session)
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        await a.claim(run.id, "workers", 1)
        await activate(b.id, expected=a.id)
        cid = await b.command("cancel", run_id=run.id, key="same-cancel")
        assert cid == await b.command("cancel", run_id=run.id, key="same-cancel")
        assert await b.commands() == []
        commands = await a.commands()
        assert len(commands) == 1 and commands[0].id == cid
        await a.ack(cid)
        assert await a.commands() == []
        async with sessionmaker()() as db:
            assert (await db.get(RuntimeCommand, cid)).acknowledged_at is not None


async def test_singleton_work_is_not_duplicated_during_cutover(tmp_path):
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        async with a.work("stt") as acquired:
            assert acquired
            await activate(b.id, expected=a.id)
            async with b.work("stt") as other:
                assert not other
            assert not await a.can_retire()
        async with b.work("stt") as acquired:
            assert acquired
        async with a.work("stt") as acquired:
            assert not acquired


async def test_heartbeat_timeout_does_not_steal_ownership(tmp_path, new_session):
    run = await queued(new_session)
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        token = await a.claim(run.id, "workers", 8)
        await activate(b.id, expected=a.id)
        async with sessionmaker()() as db:
            lease = await db.scalar(select(RuntimeLease).where(RuntimeLease.run_id == run.id))
            assert lease.generation_id == a.id and lease.token == token
        assert await b.claim(run.id, "workers", 8) is None


async def test_manager_drain_keeps_real_child_and_other_manager_cancels_owner(tmp_path, new_session):
    from ttakkari.runs.manager import RunManager

    run = await queued(new_session)
    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        row.prompt = "SLEEP"
        await db.commit()
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        ma, mb = RunManager(runtime=a), RunManager(runtime=b)
        ma.start(run.id)
        try:
            for _ in range(100):
                if (h := ma._handles.get(run.id)) and h.proc:
                    break
                await asyncio.sleep(0.03)
            assert h and h.proc and h.proc.returncode is None
            original_pid = h.proc.pid
            await activate(b.id, expected=a.id)
            assert h.proc.pid == original_pid and h.proc.returncode is None
            assert await mb.is_owned(run.id)
            assert await mb.cancel(run.id)
            for command in await a.commands():
                assert command.kind == "cancel"
                await ma.cancel(command.run_id)
                await a.ack(command.id)
            await asyncio.wait_for(h.task, 5)
            async with sessionmaker()() as db:
                assert (await db.get(Run, run.id)).status == "cancelled"
            assert await a.can_retire()
        finally:
            await ma.shutdown()
            await mb.shutdown()


def test_generation_mode_is_explicit_and_pool_budget_is_bounded(monkeypatch):
    from ttakkari.config import Settings
    monkeypatch.setenv("TTAKKARI_RUNTIME_GENERATION", "true")
    s = Settings(_env_file=None)
    assert s.runtime_generation is True
    assert s.database_pool_size + s.database_pool_overflow <= 10


async def test_other_generation_receives_committed_event_wakeup(tmp_path, new_session):
    from ttakkari.generation import wake
    from ttakkari.runs import events
    run = await queued(new_session)
    async with generations(tmp_path) as (_a, b):
        await b.listen()
        signal = events.signal_for(run.id)
        async with sessionmaker()() as db:
            await wake(db, run.id)
            assert not signal.is_set()
            await db.commit()
        await asyncio.wait_for(signal.wait(), 2)


async def test_account_selection_counts_draining_owners(tmp_path, new_session):
    from ttakkari.runs import profiles
    first, second = await queued(new_session), await queued(new_session)
    async with sessionmaker()() as db:
        pool = await profiles.candidates(db, "auto")
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        await a.claim(first.id, "workers", 8)
        chosen = await a.choose_profile(pool, first.id)
        await activate(b.id, expected=a.id)
        await b.claim(second.id, "workers", 8)
        other = await b.choose_profile(pool, second.id)
        assert chosen.id != other.id


async def test_standby_does_not_reset_stt_or_preview_jobs(tmp_path, monkeypatch):
    from ttakkari import generation
    from ttakkari.artifacts import preview
    from ttakkari.meetings.local_stt import jobs
    async with generations(tmp_path) as (a, _b):
        monkeypatch.setattr(generation, "current", a)
        # Any old startup sweep would open a DB session before starting a worker.
        def forbidden():
            raise AssertionError("standby attempted global recovery")
        monkeypatch.setattr(jobs, "sessionmaker", forbidden)
        monkeypatch.setattr(preview, "sessionmaker", forbidden)
        try:
            await jobs.start()
            await preview.start_worker()
        finally:
            await jobs.stop()
            await preview.stop_worker()


async def test_retried_enqueue_with_same_key_creates_one_run(client, auth, new_session):
    _ws, sess = await new_session()
    headers = auth | {"Idempotency-Key": "lost-response-retry"}
    first = await client.post(f"/api/sessions/{sess['id']}/runs", headers=headers, json={"prompt": "hello"})
    second = await client.post(f"/api/sessions/{sess['id']}/runs", headers=headers, json={"prompt": "hello"})
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    different = await client.post(f"/api/sessions/{sess['id']}/runs", headers=headers, json={"prompt": "different"})
    assert different.status_code == 409


async def test_shared_cooldown_is_read_before_selecting_an_account(tmp_path, monkeypatch):
    from ttakkari import generation
    from ttakkari.runs import profiles
    async with sessionmaker()() as db:
        pool = await profiles.candidates(db, "auto")
    profiles.cool_down(pool[0].id, 600)
    await profiles.flush_persist()
    profiles.reset_cooldowns()  # another process has no local copy yet
    async with generations(tmp_path) as (a, _b):
        monkeypatch.setattr(generation, "current", a)
        async with sessionmaker()() as db:
            free, blocked = await profiles.availability(db, "auto")
        assert pool[0].id not in {p.id for p in free}
        assert pool[0].id in {p.id for p, _ in blocked}


def test_newer_shared_usage_replaces_older_cache_but_not_a_fresher_stream():
    from ttakkari.runs import usage
    aid = uuid.uuid4()
    usage.seed(aid, usage.Usage(windows=[usage.Window("five_hour", "5h", 20, None)], updated_at=100))
    usage.seed(aid, usage.Usage(windows=[usage.Window("five_hour", "5h", 99, None)], updated_at=200), refresh=True)
    assert usage.cached(aid).windows[0].percent == 99
    usage.seed(aid, usage.Usage(windows=[usage.Window("five_hour", "5h", 30, None)], updated_at=150), refresh=True)
    assert usage.cached(aid).windows[0].percent == 99


async def test_restart_of_active_slot_accepts_new_work_without_stealing_orphan(tmp_path, new_session):
    first, second = await queued(new_session), await queued(new_session)
    a = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    await a.register()
    await activate(a.id, expected=None)
    await a.claim(first.id, "workers", 2)
    await a.close()
    restarted = GenerationRuntime(tmp_path, revision="a" * 40, api_url=a.api_url)
    try:
        await restarted.register()
        assert (await restarted.snapshot())["state"] == "active"
        assert await restarted.claim(first.id, "workers", 2) is None
        assert await restarted.claim(second.id, "workers", 2)
    finally:
        await restarted.close()


async def test_legacy_recovery_cannot_interrupt_a_generation_owned_run(tmp_path, new_session):
    from ttakkari.runs.manager import recover_interrupted
    run = await queued(new_session)
    async with generations(tmp_path) as (a, _b):
        await activate(a.id, expected=None)
        await a.claim(run.id, "workers", 1)
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            row.status = "running"
            await db.commit()
        with pytest.raises(RuntimeError, match="generation"):
            await recover_interrupted()
        async with sessionmaker()() as db:
            assert (await db.get(Run, run.id)).status == "running"


async def test_changed_usage_is_persisted_immediately_in_generation_mode(tmp_path, monkeypatch):
    import time

    from ttakkari import generation
    from ttakkari.runs import profiles, usage, usage_store
    async with sessionmaker()() as db:
        account = (await profiles.accounts(db))[0]
    async with generations(tmp_path) as (a, _b):
        monkeypatch.setattr(generation, "current", a)
        before = usage.Usage(windows=[usage.Window("five_hour", "5h", 30, None)], source="stream", updated_at=time.time())
        capped = usage.Usage(windows=[usage.Window("five_hour", "5h", 99, None)], source="stream", updated_at=time.time() + 0.01)
        assert await usage_store.record(account.id, before)
        assert await usage_store.record(account.id, capped)


async def test_failed_claim_never_finishes_another_generations_run(tmp_path, new_session, monkeypatch):
    from ttakkari.runs.manager import RunManager
    run = await queued(new_session)
    async with generations(tmp_path) as (a, b):
        await activate(a.id, expected=None)
        await a.claim(run.id, "workers", 2)
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            row.status = "running"
            await db.commit()
        await activate(b.id, expected=a.id)

        async def broken_claim(*args):
            raise ConnectionError("claim transaction unavailable")

        monkeypatch.setattr(b, "claim", broken_claim)
        mb = RunManager(runtime=b)
        assert mb.start(run.id)
        h = mb._handles[run.id]
        await h.task
        async with sessionmaker()() as db:
            assert (await db.get(Run, run.id)).status == "running"


async def test_desk_retry_cannot_commit_events_after_losing_ownership(tmp_path, new_session, monkeypatch):
    from sqlalchemy import delete

    from ttakkari import generation
    from ttakkari.models import ChatSession
    from ttakkari.runs import desk, profiles
    run = await queued(new_session)
    async with sessionmaker()() as db:
        await profiles.accounts(db)
    async with generations(tmp_path) as (a, _b):
        await activate(a.id, expected=None)
        await a.claim(run.id, "desk", 2)
        monkeypatch.setattr(generation, "current", a)

        async def lose_ownership(*args):
            async with sessionmaker()() as db:
                await db.execute(delete(RuntimeLease).where(RuntimeLease.run_id == run.id))
                await db.commit()
            return desk.Attempt("invalid decision", None, 1)

        monkeypatch.setattr(desk, "_ask", lose_ownership)
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            sess = await db.get(ChatSession, row.session_id)
            with pytest.raises(OwnershipLost):
                await desk.handle(db, row, sess)
        async with sessionmaker()() as db:
            assert (await db.get(Run, run.id)).last_seq == 0
