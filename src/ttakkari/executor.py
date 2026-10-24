"""Independent Run owner. No HTTP server, deployment queue, or lease stealing."""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

import psutil
from sqlalchemy import func, select, update

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.db import dispose_engine, sessionmaker
from ttakkari.generation import EXECUTOR_PROTOCOL, GenerationRuntime, _control, _lock
from ttakkari.models import (
    RuntimeCommand,
    RuntimeGeneration,
    RuntimeLease,
    utcnow,
)

log = logging.getLogger(__name__)


def validate_handoff(*, leases: int, apis: list[tuple[int, str]], pending_kinds=()) -> None:
    if leases:
        raise RuntimeError("executor handoff blocked: existing lease requires drain or orphan reconciliation")
    if not apis:
        raise RuntimeError("executor activation requires a registered remote API")
    if any(protocol != EXECUTOR_PROTOCOL for protocol, _ in apis):
        raise RuntimeError("executor activation requires protocol2 APIs")
    if any(mode != "remote" for _, mode in apis):
        raise RuntimeError("executor activation requires remote APIs")
    if set(pending_kinds) - {"cancel", "reset", "release_waiting"}:
        raise RuntimeError("executor activation refuses unknown command protocol")


class ExecutorRuntime(GenerationRuntime):
    def __init__(self, home, *, revision):
        super().__init__(home, revision=revision, api_url="http://127.0.0.1:1")
        self.role, self.execution_mode = "executor", "local"
        self.api_url = "executor://local"
        self._draining = False

    @property
    def draining(self) -> bool:
        return self._draining or self.state == "draining"

    async def register(self, *, activate: bool = False) -> None:
        if not activate:
            raise RuntimeError("executor is disabled; explicit --activate is required after independent verification")
        if self._liveness:
            raise RuntimeError("executor is already registered")
        # Different incarnations still share one process lock. DB leases survive
        # a crash; acquiring this lock alone does not authorize replacement.
        self._liveness = _lock(self.home / "runtime" / "executor.lock")
        if self._liveness is None:
            raise RuntimeError("executor already has a live owner")
        try:
            async with sessionmaker()() as db:
                control = await _control(db)
                leases = await db.scalar(select(func.count()).select_from(RuntimeLease))
                apis = list((await db.execute(select(RuntimeGeneration.protocol, RuntimeGeneration.execution_mode)
                    .where(RuntimeGeneration.role == "api",
                           RuntimeGeneration.state.in_(("standby", "active", "draining"))))).all())
                pending_kinds = list(await db.scalars(select(RuntimeCommand.kind).where(
                    RuntimeCommand.acknowledged_at.is_(None))))
                validate_handoff(leases=leases, apis=apis, pending_kinds=pending_kinds)
                previous = control.executor_generation
                db.add(RuntimeGeneration(id=self.id, revision=self.revision, api_url=self.api_url,
                    role=self.role, protocol=EXECUTOR_PROTOCOL, execution_mode=self.execution_mode,
                    pid=os.getpid(), process_started_at=psutil.Process().create_time(), state="active"))
                await db.flush()
                # Includes commands written before first activation, when their
                # target was still an API. No old lease exists at this boundary.
                await db.execute(update(RuntimeCommand).where(RuntimeCommand.acknowledged_at.is_(None))
                                 .values(generation_id=self.id))
                if previous:
                    # No children/leases exist. Keep unacknowledged commands
                    # durable across this idle replacement, under control lock.
                    row = await db.get(RuntimeGeneration, previous)
                    row.state = "offline"
                control.executor_generation = self.id
                await generation.wake(db)
                await db.commit()
                self.state = "active"
        except BaseException:
            self._liveness.close()
            self._liveness = None
            raise

    async def refresh(self) -> str:
        await super().refresh()
        if self._draining and self.state != "draining":
            self.state = "draining"
        async with sessionmaker()() as db:
            control = await db.get(generation.RuntimeControl, 1)
            if control.executor_generation != self.id:
                raise generation.OwnershipLost("executor pointer changed")
        return self.state


async def disable() -> None:
    """Idle rollback only. Database trigger refuses pending commands/leases."""
    async with sessionmaker()() as db:
        control = await _control(db)
        control.executor_generation = None
        await generation.wake(db)
        await db.commit()


async def drain(
    runtime: ExecutorRuntime,
    manager,
    *,
    poll_interval: float = 0.2,
    timeout: float | None = None,
) -> bool:
    """Drain in-flight Runs, durable queues (STT/preview), and periodic maintenance before termination.

    1. Stops new claims immediately: marks runtime state as 'draining'.
    2. Keeps processing runtime commands (cancel, reset) so in-flight work can be cleanly controlled.
    3. Waits for manager._handles (active Run subprocesses) and runtime._work (in-flight STT/preview/periodic) to drain.
    4. Emits periodic heartbeat so readiness and controller checks observe progress.
    5. Returns True when fully drained, False if timeout expired.
    """
    from ttakkari.generation_service import _commands

    runtime._draining = True
    runtime.state = "draining"
    try:
        async with sessionmaker()() as db:
            row = await db.get(RuntimeGeneration, runtime.id)
            if row:
                row.state = "draining"
                row.heartbeat_at = utcnow()
                await db.commit()
    except Exception:
        log.exception("failed to persist executor draining state to database")

    deadline = (time.monotonic() + timeout) if timeout is not None else None
    log.info("executor drain started: active_runs=%d active_work=%s",
             len(manager._handles), list(runtime._work))

    # Process any pending commands immediately before entering drain loop
    try:
        await _commands(runtime, manager)
    except Exception:
        log.exception("error processing commands at drain start")

    while manager._handles or runtime._work:
        if deadline is not None and time.monotonic() > deadline:
            log.warning("executor drain timed out: active_runs=%d active_work=%s",
                        len(manager._handles), list(runtime._work))
            return False
        try:
            await _commands(runtime, manager)
            await runtime.refresh()
        except generation.OwnershipLost:
            log.warning("executor ownership changed during drain; aborting drain")
            break
        except Exception:
            log.exception("error during executor drain iteration")
        await asyncio.sleep(poll_interval)

    # Final pass to acknowledge any commands that completed alongside final handles
    try:
        await _commands(runtime, manager)
    except Exception:
        log.exception("error during final drain command acknowledgment")

    log.info("executor drain completed cleanly: 0 active runs, 0 active work")
    return True


async def start_background_services() -> list[asyncio.Task]:
    """Own durable non-Run queues and periodic work alongside the Run executor."""
    from ttakkari.artifacts import preview
    from ttakkari.generation_service import maintenance_jobs, periodic
    from ttakkari.meetings.local_stt import jobs as stt_jobs

    await preview.start_worker()
    await stt_jobs.start()
    return [asyncio.create_task(periodic(name, operation, interval), name=f"periodic:{name}")
            for name, operation, interval in maintenance_jobs(get_settings())]


async def serve(*, activate: bool = False, drain_timeout: float | None = None) -> None:
    from ttakkari.artifacts import preview
    from ttakkari.deployment import revision
    from ttakkari.generation_service import coordinate_runs
    from ttakkari.meetings.local_stt import jobs as stt_jobs
    from ttakkari.runs import profiles, usage_store
    from ttakkari.runs.manager import manager

    settings = get_settings()
    if not settings.release_revision:
        raise RuntimeError("executor requires a pinned immutable release revision")
    runtime = ExecutorRuntime(settings.home, revision=revision())
    stop = asyncio.Event()
    background_tasks: list[asyncio.Task] = []
    preview_started = stt_started = False
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    generation.current = runtime
    try:
        await runtime.register(activate=activate)
        await runtime.listen()
        usage_store.install()
        background_tasks = await start_background_services()
        preview_started = stt_started = True
        while not stop.is_set():
            try:
                runtime.signal.clear()
                await runtime.listen()
                state = await runtime.refresh()
                async with sessionmaker()() as db:
                    await profiles.load_cooldowns(db)
                    await usage_store.load_latest(db, refresh=True)
                await coordinate_runs(runtime, manager, state)
            except generation.OwnershipLost:
                raise
            except Exception:
                log.exception("executor tick failed; leases retained, no recovery sweep")
            waiter = asyncio.create_task(runtime.signal.wait())
            stopper = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait((waiter, stopper), timeout=settings.runtime_poll_seconds,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                waiter.cancel()
                stopper.cancel()
                await asyncio.gather(waiter, stopper, return_exceptions=True)
    finally:
        try:
            if runtime.state in ("active", "draining"):
                await drain(runtime, manager, timeout=drain_timeout)
        finally:
            try:
                for task in background_tasks:
                    task.cancel()
                await asyncio.gather(*background_tasks, return_exceptions=True)
                if stt_started:
                    await stt_jobs.stop()
                if preview_started:
                    await preview.stop_worker()
                await manager.shutdown()
                await profiles.flush_persist()
                await usage_store.flush()
            finally:
                await runtime.close()
                generation.current = None
                for sig in (signal.SIGTERM, signal.SIGINT):
                    loop.remove_signal_handler(sig)
                await dispose_engine()
