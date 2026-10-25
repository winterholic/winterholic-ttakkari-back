"""Collector Platform: bounded consume batch run inside generation.work('collector-consume') (CP-S2-02).

The stage logic (fetch/extract/score/deliver) arrives in later packages as a handler registered here. Until a handler
is registered the batch claims nothing, so enabling the loop can never mark a job done without real work.

Drain safety: the caller holds generation.work(), which yields False once the generation is no longer active, and
`accepting()` is re-checked before every claim, so a draining generation finishes the current job and claims no more.
"""
from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal

import psutil
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari import generation as generation_module
from ttakkari.collector import jobs
from ttakkari.collector.contracts import AttemptFencedError
from ttakkari.collector.jobs import ClaimedJob, OutboxItem, OwnerState
from ttakkari.collector_models import CollectorJob
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import RuntimeGeneration

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HandlerResult:
    status: str = "succeeded"
    error: dict[str, Any] | None = None
    retry_after: float | None = None


class JobContext:
    """What a stage handler may do with its claim. Each call is its own short, fenced transaction."""

    def __init__(self, job: ClaimedJob) -> None:
        self.job = job

    async def commit_page(
        self, cursor: dict[str, Any] | None, *, stage: str | None = None, outbox: Iterable[OutboxItem] = ()
    ) -> Literal["continue", "cancelled"]:
        async with sessionmaker()() as db:
            outcome = await jobs.commit_page(
                db, self.job.id, self.job.attempt_token, cursor=cursor, stage=stage, outbox=list(outbox)
            )
            await db.commit()
        return outcome

    async def commit_work(
        self,
        work: Callable[[AsyncSession], Awaitable[tuple[dict[str, Any] | None, Iterable[OutboxItem]]]],
        *,
        stage: str | None = None,
    ) -> Literal["continue", "cancelled"]:
        """Run `work(db)` and commit its writes with the cursor and outbox it returns, in one fenced transaction.

        `work` may add rows to `db` (items, dedupe bands) and returns (next cursor, extra outbox). If the attempt is
        fenced or an outbox key collides, nothing from `work` is committed either."""
        async with sessionmaker()() as db:
            cursor, outbox = await work(db)
            outcome = await jobs.commit_page(
                db, self.job.id, self.job.attempt_token, cursor=cursor, stage=stage, outbox=list(outbox)
            )
            await db.commit()
        return outcome

    async def heartbeat(self) -> None:
        async with sessionmaker()() as db:
            await jobs.heartbeat(db, self.job.id, self.job.attempt_token)
            await db.commit()


JobHandler = Callable[[ClaimedJob, JobContext], Awaitable[HandlerResult]]
_handler: JobHandler | None = None


def register_handler(handler: JobHandler | None) -> None:
    global _handler
    _handler = handler


def current_owner() -> str:
    runtime = generation_module.current
    return str(runtime.id) if runtime is not None else f"single-{os.getpid()}"


def accepting() -> bool:
    runtime = generation_module.current
    return runtime is None or runtime.state == "active"


async def _owner_states(owners: Iterable[str]) -> dict[str, OwnerState]:
    states: dict[str, OwnerState] = {}
    async with sessionmaker()() as db:
        for owner in owners:
            if owner.startswith("single-"):
                try:
                    pid = int(owner.removeprefix("single-"))
                except ValueError:
                    states[owner] = "unknown"
                    continue
                states[owner] = "alive" if psutil.pid_exists(pid) else "offline"
                continue
            try:
                generation_id = uuid.UUID(owner)
            except ValueError:
                states[owner] = "unknown"
                continue
            row = await db.get(RuntimeGeneration, generation_id)
            if row is None:
                states[owner] = "unknown"
            elif row.state == "offline":
                states[owner] = "offline"
            else:
                # A live generation holds its liveness lock for its whole life; a free lock means it is gone.
                probe = generation_module._lock(get_settings().home / "runtime" / f"{generation_id}.lock")
                if probe is None:
                    states[owner] = "alive"
                else:
                    probe.close()
                    states[owner] = "offline"
    return states


async def recover() -> jobs.RecoveryReport:
    stale = timedelta(seconds=get_settings().collector_stale_seconds)
    async with sessionmaker()() as db:
        owners = set(
            await db.scalars(
                select(CollectorJob.owner_generation).where(
                    CollectorJob.status == "running", CollectorJob.owner_generation.is_not(None)
                ).distinct()
            )
        )
    states = await _owner_states(owners)
    async with sessionmaker()() as db:
        report = await jobs.recover_orphans(db, owner_state=lambda o: states.get(o, "unknown"), stale_after=stale)
        await db.commit()
    if report.held:
        log.warning("collector: %d running job(s) held, owner liveness unconfirmed", len(report.held))
    return report


async def run_one(job: ClaimedJob, handler: JobHandler) -> str | None:
    """Run the handler and end the attempt. Returns the stored status, or None if the attempt was fenced."""
    try:
        result = await handler(job, JobContext(job))
    except AttemptFencedError:
        return None
    except Exception as e:
        log.exception("collector handler failed for job %s", job.id)
        result = HandlerResult("retry_wait", {"code": "handler_error", "type": type(e).__name__})
    try:
        async with sessionmaker()() as db:
            if result.status == "yielded":
                stored = await jobs.yield_attempt(db, job.id, job.attempt_token)
            else:
                stored = await jobs.finish(
                    db, job.id, job.attempt_token, status=result.status, error=result.error, retry_after=result.retry_after
                )
            await db.commit()
        return stored
    except AttemptFencedError:
        return None


async def consume_once(*, limit: int | None = None, managed_only: bool = False) -> int:
    """One bounded batch: recover orphans of dead owners, then claim and run up to `limit` jobs one at a time."""
    await recover()
    handler = _handler
    if handler is None:
        return 0
    owner = current_owner()
    batch = limit or get_settings().collector_batch_size
    done = 0
    for _ in range(batch):
        if not accepting():
            break
        async with sessionmaker()() as db:
            claimed = await jobs.claim_batch(db, owner_generation=owner, limit=1, managed_only=managed_only)
            await db.commit()
        if not claimed:
            break
        await run_one(claimed[0], handler)
        done += 1
    return done
