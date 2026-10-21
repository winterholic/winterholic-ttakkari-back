"""The flush fence must keep ownership semantics without deadlocking in-process sessions.

A Run session that has flushed holds the lease fence until it commits, while
GenerationRuntime.choose_profile updates the same lease from another session and
the Run session waits for it. The fence therefore may not block non-key UPDATEs,
but it must still block release/reconcile (DELETE under FOR UPDATE) and reject a
replaced lease. Real PostgreSQL; the conftest cluster is used.
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import update
from test_generations import generations, queued

from ttakkari.db import sessionmaker
from ttakkari.generation import OwnershipLost, activate
from ttakkari.models import Run, RuntimeLease


async def _fenced_run_session(runtime, run_id, token):
    db = sessionmaker()()
    db.info["runtime_lease"] = (runtime, run_id, token)
    run = await db.get(Run, run_id)
    run.prompt = "changed under fence"  # pending ORM write: the next query autoflushes
    await db.get(RuntimeLease, run_id)  # autoflush fires before_flush -> fence lock taken
    return db


async def test_fence_does_not_block_account_update_from_another_session(tmp_path, new_session):
    async with generations(tmp_path) as (a, _b):
        await activate(a.id, expected=None)
        run = await queued(new_session)
        token = await a.claim(run.id, "workers", 3)
        assert token is not None
        db = await _fenced_run_session(a, run.id, token)
        try:
            async with sessionmaker()() as other:
                # Same statement shape as choose_profile's lease.account_id write.
                await asyncio.wait_for(
                    other.execute(update(RuntimeLease).where(RuntimeLease.run_id == run.id).values(account_id=None)), 5)
                await asyncio.wait_for(other.commit(), 5)
        finally:
            await db.commit()
            await db.close()
        await a.release(run.id, token)


async def test_fence_still_blocks_release_until_the_run_transaction_ends(tmp_path, new_session):
    async with generations(tmp_path) as (a, _b):
        await activate(a.id, expected=None)
        run = await queued(new_session)
        token = await a.claim(run.id, "workers", 3)
        db = await _fenced_run_session(a, run.id, token)
        releasing = asyncio.create_task(a.release(run.id, token))
        try:
            await asyncio.sleep(1)
            assert not releasing.done(), "release must wait for the fenced Run transaction"
        finally:
            await db.commit()
            await db.close()
        await asyncio.wait_for(releasing, 5)
        async with sessionmaker()() as check:
            assert await check.get(RuntimeLease, run.id) is None


async def test_fence_rejects_a_replaced_lease(tmp_path, new_session):
    async with generations(tmp_path) as (a, _b):
        await activate(a.id, expected=None)
        run = await queued(new_session)
        token = await a.claim(run.id, "workers", 3)
        await a.release(run.id, token)
        db = sessionmaker()()
        db.info["runtime_lease"] = (a, run.id, token)
        try:
            (await db.get(Run, run.id)).prompt = "stale write"
            with pytest.raises(OwnershipLost):
                await db.flush()
        finally:
            await db.rollback()
            await db.close()
