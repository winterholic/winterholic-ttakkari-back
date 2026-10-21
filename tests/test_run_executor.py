"""PostgreSQL ownership/protocol tests; run on the existing private cluster fixture.

Not executable in the nested Codex sandbox with known initdb/shmget denial.
No shared database fallback is allowed.
"""
from __future__ import annotations

import asyncio
import importlib.util
import uuid
from contextlib import asynccontextmanager

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ttakkari.db import sessionmaker
from ttakkari.executor import ExecutorRuntime, disable
from ttakkari.generation import GenerationRuntime, OwnershipLost, activate
from ttakkari.generation_service import _commands
from ttakkari.models import (
    ChatSession,
    Run,
    RuntimeCommand,
    RuntimeControl,
    RuntimeGeneration,
    RuntimeLease,
)
from ttakkari.runs.manager import RunManager


def migration():
    spec = importlib.util.spec_from_file_location("executor_migration", "alembic/versions/0063_run_executor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
async def executor_schema():
    async with sessionmaker()() as db:
        conn = await db.connection()
        def install(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration().install_guards()
        await conn.run_sync(install)
        await db.commit()
    try:
        yield
    finally:
        # Only this fixture's guard objects; preserve ORM columns for other tests.
        async with sessionmaker()() as db:
            for table, name in (("runtime_commands", "command"), ("runtime_leases", "lease"),
                                ("runtime_generations", "api"), ("runtime_control", "control")):
                await db.execute(text(f"DROP TRIGGER executor_{name} ON {table}"))
            for fn in ("route_executor_command", "guard_executor_lease", "guard_executor_api", "guard_executor_control"):
                await db.execute(text(f"DROP FUNCTION {fn}()"))
            await db.commit()


@asynccontextmanager
async def owners(tmp_path):
    api = GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    api.execution_mode = "remote"
    executor = ExecutorRuntime(tmp_path, revision="e" * 40)
    await api.register()
    await activate(api.id, expected=None)
    try:
        await executor.register(activate=True)
        yield api, executor
    finally:
        await executor.close()
        await api.close()


async def queued(new_session):
    ws, sess = await new_session()
    async with sessionmaker()() as db:
        run = Run(session_id=uuid.UUID(sess["id"]), workspace_id=uuid.UUID(ws["id"]),
                  prompt="executor ownership", engine="claude")
        db.add(run)
        await db.commit()
        return run


async def test_executor_claim_survives_api_cutover_and_duplicate_start(tmp_path, new_session, executor_schema):
    run = await queued(new_session)
    async with owners(tmp_path) as (a, executor):
        claims = await asyncio.gather(*(executor.claim(run.id, "workers", 8) for _ in range(8)))
        assert sum(token is not None for token in claims) == 1
        token = next(token for token in claims if token)
        b = GenerationRuntime(tmp_path, revision="b" * 40, api_url="http://127.0.0.1:18792")
        b.execution_mode = "remote"
        try:
            await b.register()
            await activate(b.id, expected=a.id)
            assert await a.can_retire()
            assert await b.claim(run.id, "workers", 8) is None
            async with sessionmaker()() as db:
                await executor.fence(db, run.id, token)
            assert (await executor.snapshot())["owned_runs"] == 1
        finally:
            await executor.release(run.id, token)
            await b.close()


async def test_raw_old_claim_and_registration_and_rollback_are_rejected(tmp_path, new_session, executor_schema):
    run = await queued(new_session)
    async with owners(tmp_path) as (api, executor):
        async with sessionmaker()() as db:
            db.add(RuntimeLease(run_id=run.id, generation_id=api.id, token=uuid.uuid4(),
                                session_id=run.session_id, lane="workers"))
            with pytest.raises(DBAPIError, match="API cannot claim"):
                await db.commit()
        async with sessionmaker()() as db:
            db.add(RuntimeGeneration(id=uuid.uuid4(), revision="f" * 40, api_url="http://127.0.0.1:18793",
                                     pid=1, process_started_at=1, state="standby"))  # old binary defaults
            with pytest.raises(DBAPIError, match="incompatible API registration"):
                await db.commit()
        token = await executor.claim(run.id, "workers", 1)
        with pytest.raises(DBAPIError, match="existing lease"):
            await disable()
        await executor.release(run.id, token)
        await disable()
        async with sessionmaker()() as db:
            assert (await db.get(RuntimeControl, 1)).executor_generation is None


async def test_cancel_ack_retry_reset_and_idle_executor_replacement(tmp_path, new_session, executor_schema, monkeypatch):
    run = await queued(new_session)
    async with owners(tmp_path) as (api, executor):
        key = "cancel-once"
        cid = await api.command("cancel", run_id=run.id, key=key)
        assert cid == await api.command("cancel", run_id=run.id, key=key)
        manager = RunManager(runtime=executor)
        # An ack transport failure after cancellation must leave a durable retry.
        original = executor.ack
        async def fail_ack(_id):
            raise ConnectionError("ack reply lost")
        monkeypatch.setattr(executor, "ack", fail_ack)
        with pytest.raises(ConnectionError):
            await _commands(executor, manager)
        async with sessionmaker()() as db:
            assert (await db.get(Run, run.id)).status == "cancelled"
            assert (await db.get(RuntimeCommand, cid)).acknowledged_at is None
        monkeypatch.setattr(executor, "ack", original)
        await _commands(executor, manager)
        assert await executor.commands() == []
        async with sessionmaker()() as db:
            sess = await db.get(ChatSession, run.session_id)
            sess.engine_sessions = {"claude:test": "old-provider-conversation"}
            await db.commit()
        reset = await api.command("reset", session_id=run.session_id, key="reset-once")
        await _commands(executor, manager)
        async with sessionmaker()() as db:
            assert (await db.get(RuntimeCommand, reset)).acknowledged_at is not None
        pending = await api.command("release_waiting", key="persist-through-restart")
        await executor.close()
        replacement = ExecutorRuntime(tmp_path, revision="f" * 40)
        try:
            await replacement.register(activate=True)
            assert [c.id for c in await replacement.commands()] == [pending]
            with pytest.raises(DBAPIError, match="pending commands"):
                await disable()
            await replacement.ack(pending)
            await disable()
        finally:
            await replacement.close()


async def test_crashed_owner_is_quarantined_even_with_expired_heartbeat(tmp_path, new_session, executor_schema):
    run = await queued(new_session)
    async with owners(tmp_path) as (api, executor):
        token = await executor.claim(run.id, "workers", 1)
        await executor.close()  # Simulates loss of owner lock, lease intentionally retained.
        replacement = ExecutorRuntime(tmp_path, revision="f" * 40)
        try:
            with pytest.raises(RuntimeError, match="existing lease"):
                await replacement.register(activate=True)
            assert await api.claim(run.id, "workers", 1) is None
            async with sessionmaker()() as db:
                lease = await db.get(RuntimeLease, run.id)
                assert lease.generation_id == executor.id and lease.token == token
            await executor.release(run.id, token)
            with pytest.raises(OwnershipLost):
                async with sessionmaker()() as db:
                    await executor.fence(db, run.id, token)
        finally:
            await replacement.close()


async def test_executor_concurrent_registration_has_one_winner(tmp_path, executor_schema):
    api = GenerationRuntime(tmp_path, revision="a"*40, api_url="http://127.0.0.1:18791")
    api.execution_mode = "remote"
    await api.register()
    await activate(api.id, expected=None)
    candidates = [ExecutorRuntime(tmp_path, revision="e"*40) for _ in range(2)]
    try:
        results = await asyncio.gather(*(r.register(activate=True) for r in candidates), return_exceptions=True)
        assert sum(result is None for result in results) == 1
        assert sum(isinstance(result, RuntimeError) for result in results) == 1
    finally:
        for candidate in candidates:
            await candidate.close()
        await api.close()


async def test_migration_round_trip_and_active_downgrade_refusal(tmp_path, executor_schema):
    from alembic.autogenerate import compare_metadata

    from ttakkari.models import Base

    def run(sync, operation):
        with Operations.context(MigrationContext.configure(sync)):
            operation()

    async with sessionmaker()() as db:
        conn = await db.connection()
        await conn.run_sync(run, migration().downgrade)
        assert await db.scalar(text("SELECT count(*) FROM information_schema.columns WHERE table_name="
                                    "'runtime_control' AND column_name='executor_generation'")) == 0
        await conn.run_sync(run, migration().upgrade)
        def drift(sync):
            def include(obj, name, kind, reflected, compare_to):
                if kind == "table":
                    return name in {"runtime_generations", "runtime_control"}
                table = getattr(obj, "table", None)
                return table is None or table.name in {"runtime_generations", "runtime_control"}
            return compare_metadata(MigrationContext.configure(sync, opts={"include_object": include}), Base.metadata)
        assert await conn.run_sync(drift) == []
        await db.commit()
    async with owners(tmp_path), sessionmaker()() as db:
        conn = await db.connection()
        with pytest.raises(DBAPIError, match="drain executor"):
            await conn.run_sync(run, migration().downgrade)
