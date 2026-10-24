"""Shared drain switch for executor-owned consumers of API-accepted work (Creative assets, game builds).

Not a queue. The durable queue is each domain's own record (production.json, game_builds). This only answers
two questions the executor needs on shutdown: "may a consumer claim new work?" and "is a claimed job still running?".
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable

from ttakkari import generation

DRAIN_SECONDS = 1800.0
_draining = False
_inflight: set[asyncio.Task] = set()


def delegated() -> bool:
    """True when this process is a remote-mode API: it accepts and reports work, the executor runs it."""
    runtime = generation.current
    return bool(runtime and runtime.role == "api" and runtime.execution_mode == "remote")


def draining() -> bool:
    return _draining


def begin_drain() -> None:
    """Stop claiming new work. Claimed jobs keep running and queued records stay for the next consumer."""
    global _draining
    _draining = True


def reopen() -> None:
    global _draining
    _draining = False


def _retrieve(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()  # the caller may already be gone; avoid "exception was never retrieved"


async def run_tracked[T](work: Awaitable[T]) -> T:
    """Run a claimed job so that cancelling its consumer loop (shutdown) does not interrupt it mid-way."""
    task = asyncio.ensure_future(work)
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    task.add_done_callback(_retrieve)
    return await asyncio.shield(task)


async def wait_idle() -> None:
    while _inflight:
        await asyncio.gather(*tuple(_inflight), return_exceptions=True)


def inflight() -> int:
    """Claimed jobs that have not finished. A generation must not retire while this is non-zero."""
    return sum(not task.done() for task in _inflight)


async def drain(timeout: float | None = DRAIN_SECONDS) -> bool:
    """Close claims and wait for claimed jobs. True: all finished naturally.

    False: the bound expired and what was left got cancelled. None waits without a bound (executor replacement).
    """
    begin_drain()
    loop = asyncio.get_running_loop()
    deadline = None if timeout is None else loop.time() + timeout
    while pending := tuple(task for task in _inflight if not task.done()):
        remaining = None if deadline is None else max(0.0, deadline - loop.time())
        _, unfinished = await asyncio.wait(pending, timeout=remaining)
        if unfinished and deadline is not None and loop.time() >= deadline:
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*unfinished, return_exceptions=True)
            return False
    return True
