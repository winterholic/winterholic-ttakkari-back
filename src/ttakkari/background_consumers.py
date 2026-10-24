"""Consumers for requests that used to run as in-memory API tasks (platform v2 Step 6).

NotebookLM jobs, Reverse analyses and manual context indexing are recorded durably by the API
(``queued`` job rows / ``ctx_sources.index_requested_at``). In a remote-mode API (``deferred()``)
nothing else happens there. The executor, or a local-mode API as the rollback path, runs the
consumers below, which claim rows with a conditional update and run each to completion.

Contract
- A claim moves ``queued`` to ``running`` once; ``running`` rows are never claimed again.
- ``stop()`` stops claiming first and lets in-flight work finish; ``queued`` rows stay queued.
- Restarting or redeploying the API does not touch work owned by the executor.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from ttakkari import generation
from ttakkari.config import get_settings

log = logging.getLogger(__name__)
DRAIN_SECONDS = 1800.0
IDLE_POLL_CAP_SECONDS = 2.0


def deferred() -> bool:
    """True in a remote-mode API process: accept and record only, the executor consumes."""
    runtime = generation.current
    return bool(runtime and runtime.role == "api" and runtime.execution_mode == "remote")


async def _pump_notebooklm() -> int:
    from ttakkari.notebooklm.service import service
    return await service.pump_queued()


async def _pump_reverse() -> int:
    from ttakkari import reverse
    return await reverse.pump_queued()


async def _pump_context_index() -> int:
    from ttakkari.context import index_queue
    return await index_queue.pump()


PUMPS: tuple[tuple[str, Callable[[], Awaitable[int]]], ...] = (
    ("notebooklm", _pump_notebooklm), ("reverse", _pump_reverse), ("context-index", _pump_context_index))


def inflight() -> int:
    from ttakkari import reverse
    from ttakkari.context import index_queue
    from ttakkari.notebooklm.service import service
    return service.inflight() + reverse.inflight() + index_queue.inflight()


async def _cancel_inflight() -> None:
    """Only after the drain window. Each domain's own cancel path records what is known."""
    from ttakkari import reverse
    from ttakkari.context import index_queue
    from ttakkari.notebooklm.service import service
    tasks = [t for t in (*reverse._tasks, *index_queue._tasks, *service._tasks.values()) if not t.done()]
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


class BackgroundConsumers:
    def __init__(self) -> None:
        self._loops: list[asyncio.Task] = []
        self._stopping: asyncio.Event | None = None

    @property
    def running(self) -> bool:
        return bool(self._loops)

    async def start(self) -> None:
        if self._loops:
            return
        self._stopping = asyncio.Event()
        # Quota-waiting Reverse jobs go back to the queue at start, as the old startup did.
        with contextlib.suppress(Exception):
            from ttakkari import reverse
            if n := await reverse.resume_waiting_reverse_jobs():
                log.info("requeued %d waiting reverse jobs", n)
        self._loops = [asyncio.create_task(self._loop(name, pump), name=f"consumer:{name}") for name, pump in PUMPS]

    async def _loop(self, name: str, pump: Callable[[], Awaitable[int]]) -> None:
        stopping = self._stopping
        assert stopping is not None
        while not stopping.is_set():
            started = 0
            try:
                started = await pump()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("background consumer failed: %s", name)
            if not started:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stopping.wait(), min(get_settings().runtime_poll_seconds,
                                                               IDLE_POLL_CAP_SECONDS))

    def stop_claiming(self) -> None:
        """Call as soon as a stop signal arrives. Claimed work is untouched; queued rows stay queued."""
        if self._stopping is not None:
            self._stopping.set()

    async def drain(self, drain_seconds: float | None = DRAIN_SECONDS) -> None:
        """Wait for work; None preserves it indefinitely, a finite window allows cancellation."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + drain_seconds if drain_seconds is not None else None
        while inflight() and (deadline is None or loop.time() < deadline):
            await asyncio.sleep(0.1)
        if deadline is None:
            return  # Executor replacement is natural completion, never timeout cancellation.
        if inflight():
            log.warning("background consumers: cancelling %d in-flight jobs after %.0fs drain", inflight(),
                        drain_seconds)
        await _cancel_inflight()

    async def stop(self, drain_seconds: float | None = DRAIN_SECONDS) -> None:
        """stop_claiming(), wait for the loops to leave, then drain()."""
        self.stop_claiming()
        loops, self._loops = self._loops, []
        await asyncio.gather(*loops, return_exceptions=True)  # a pump is short; this never cancels a claim
        await self.drain(drain_seconds)


consumers = BackgroundConsumers()


async def start() -> None:
    await consumers.start()


async def stop(drain_seconds: float | None = DRAIN_SECONDS) -> None:
    await consumers.stop(drain_seconds)
