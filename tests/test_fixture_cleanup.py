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
        cancelled = asyncio.Event()

        async def broken_dispatch():
            raise RuntimeError("fixture dispatch failure")

        async def never_finishes():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        task = asyncio.create_task(broken_dispatch())
        pending = asyncio.create_task(never_finishes())
        background = {task, pending}
        task.add_done_callback(background.discard)
        pending.add_done_callback(background.discard)
        async def cancel_handle(_run_id):
            return None

        with pytest.raises(RuntimeError, match="fixture dispatch failure"):
            await drain_registered_tasks({}, background, cancel_handle)
        assert cancelled.is_set()
        assert pending.done()
        assert not background

    asyncio.run(scenario())


def test_teardown_drains_dispatch_spawned_when_run_is_cancelled():
    async def scenario():
        handles = {}
        background = set()
        waiting = asyncio.Event()
        entered = asyncio.Event()
        dispatch_finalized = False

        async def dispatch_completion():
            await asyncio.sleep(0)

        async def run_task():
            nonlocal dispatch_finalized
            entered.set()
            try:
                await waiting.wait()
            finally:
                dispatch_finalized = True
                dispatch = asyncio.create_task(dispatch_completion())
                background.add(dispatch)
                dispatch.add_done_callback(background.discard)

        task = asyncio.create_task(run_task())
        handles["run-1"] = SimpleNamespace(task=task)
        task.add_done_callback(lambda _task: handles.pop("run-1", None))

        async def cancel_handle(run_id):
            handles[run_id].task.cancel()

        await entered.wait()
        await drain_registered_tasks(handles, background, cancel_handle)
        assert dispatch_finalized
        assert not handles
        assert not background

    asyncio.run(scenario())


def test_teardown_cleans_other_tasks_before_reraising_handle_error():
    async def scenario():
        background = set()
        background_cancelled = asyncio.Event()
        entered = asyncio.Event()

        async def broken_handle():
            entered.set()
            raise RuntimeError("fixture handle failure")

        async def never_finishes():
            try:
                await asyncio.Event().wait()
            finally:
                background_cancelled.set()

        handle_task = asyncio.create_task(broken_handle())
        handles = {"run-1": SimpleNamespace(task=handle_task)}
        handle_task.add_done_callback(lambda _task: handles.pop("run-1", None))
        background_task = asyncio.create_task(never_finishes())
        background.add(background_task)
        background_task.add_done_callback(background.discard)
        await entered.wait()

        async def cancel_handle(_run_id):
            return None

        with pytest.raises(RuntimeError, match="fixture handle failure"):
            await drain_registered_tasks(
                handles,
                background,
                cancel_handle,
            )

        assert background_cancelled.is_set()
        assert background_task.done()
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
