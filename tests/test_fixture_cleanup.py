"""DB-free reproductions for the async task cleanup contract in conftest._clean."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from task_cleanup import drain_registered_tasks, is_postgres_deadlock


def test_teardown_fails_if_dispatch_chain_outlives_drain_budget():
    async def scenario():
        handles: dict[str, asyncio.Task] = {}
        background: set[asyncio.Task] = set()
        spawned = 0

        def spawn_next(_done: asyncio.Task | None = None):
            nonlocal spawned
            if _done is not None:
                background.discard(_done)
            if spawned >= 21:
                return
            spawned += 1

            async def dispatch_completion():
                await asyncio.sleep(0)

            task = asyncio.create_task(dispatch_completion())
            background.add(task)
            task.add_done_callback(spawn_next)

        spawn_next()
        async def cancel_handle(_run_id):
            return None

        await drain_registered_tasks(handles, background, cancel_handle)

    with pytest.raises(AssertionError, match="could not drain registered tasks.*background=1"):
        asyncio.run(scenario())


def test_teardown_does_not_hide_unexpected_background_task_exception():
    async def scenario():
        async def broken_dispatch():
            raise RuntimeError("fixture dispatch failure")

        task = asyncio.create_task(broken_dispatch())
        background = {task}
        async def cancel_handle(_run_id):
            return None

        await drain_registered_tasks({}, background, cancel_handle)

    with pytest.raises(RuntimeError, match="fixture dispatch failure"):
        asyncio.run(scenario())


def test_teardown_drains_dispatch_spawned_when_run_is_cancelled():
    async def scenario():
        handles = {}
        background = set()
        waiting = asyncio.Event()

        async def dispatch_completion():
            await asyncio.sleep(0)

        async def run_task():
            try:
                await waiting.wait()
            finally:
                dispatch = asyncio.create_task(dispatch_completion())
                background.add(dispatch)
                dispatch.add_done_callback(background.discard)

        task = asyncio.create_task(run_task())
        handles["run-1"] = SimpleNamespace(task=task)
        task.add_done_callback(lambda _task: handles.pop("run-1", None))

        async def cancel_handle(run_id):
            handles[run_id].task.cancel()

        await drain_registered_tasks(handles, background, cancel_handle)
        assert not handles
        assert not background

    asyncio.run(scenario())


def test_cleanup_retry_accepts_only_postgres_deadlock_sqlstate():
    deadlock = SimpleNamespace(orig=SimpleNamespace(sqlstate="40P01"))
    lock_timeout = SimpleNamespace(orig=SimpleNamespace(sqlstate="55P03"))
    text_only = SimpleNamespace(orig=SimpleNamespace(sqlstate=None, args=("deadlock detected",)))

    assert is_postgres_deadlock(deadlock)
    assert not is_postgres_deadlock(lock_timeout)
    assert not is_postgres_deadlock(text_only)
