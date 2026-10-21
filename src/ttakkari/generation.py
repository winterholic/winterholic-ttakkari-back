"""Single-Mac generation ownership. DB controls claims; OS locks guard long work.

No lease is stolen on a heartbeat timeout: losing a DB connection does not prove
that the owning process or its agent children have stopped doing external work.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg
import psutil
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    Run,
    RuntimeCommand,
    RuntimeControl,
    RuntimeGeneration,
    RuntimeLease,
    SessionKind,
    utcnow,
)

log = logging.getLogger(__name__)
PROTOCOL = 1  # Existing API/supervisor wire contract remains compatible.
EXECUTOR_PROTOCOL = 2  # Ownership/command capability, persisted per incarnation.
PORTS = (8791, 8792, 8793)
CHANNEL = "ttakkari_runtime"


def supervisor_capabilities() -> dict:
    """실행 중인 감독이 제공하는 기능. 릴리스 요구조건과 비교하는 단일 원천."""
    return {"protocol": PROTOCOL, "slots": len(PORTS)}


class OwnershipLost(RuntimeError):
    """A stale attempt must roll back all its pending DB writes."""


def claim_owner(control: RuntimeControl) -> uuid.UUID | None:
    return control.executor_generation or control.active_generation


def _lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


async def _control(db: AsyncSession) -> RuntimeControl:
    await db.execute(insert(RuntimeControl).values(id=1, epoch=0).on_conflict_do_nothing())
    return await db.scalar(select(RuntimeControl).where(RuntimeControl.id == 1).with_for_update())


async def activate(generation_id: uuid.UUID, *, expected: uuid.UUID | None) -> int:
    async with sessionmaker()() as db:
        control = await _control(db)
        if control.active_generation != expected:
            raise ValueError("active generation changed; reconcile before retry")
        target = await db.get(RuntimeGeneration, generation_id)
        if (target is None or target.role != "api" or target.protocol not in {1, EXECUTOR_PROTOCOL}
                or target.state not in {"standby", "draining", "active"}):
            raise ValueError("target generation is not available")
        if expected and expected != generation_id:
            previous = await db.get(RuntimeGeneration, expected)
            if previous is not None:
                previous.state = "draining"
        target.state = "active"
        control.active_generation = generation_id
        control.epoch += 1
        await wake(db)
        await db.commit()
        return control.epoch


async def wake(db: AsyncSession, run_id: uuid.UUID | None = None) -> None:
    # NOTIFY is only a wakeup. All data and cursors live in ordinary DB rows.
    await db.execute(select(func.pg_notify(CHANNEL, str(run_id) if run_id else "queue")))


class GenerationRuntime:
    def __init__(self, home: Path, *, revision: str, api_url: str):
        url = urlsplit(api_url)
        if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path:
            raise ValueError("generation API must be a loopback HTTP origin")
        self.home, self.revision, self.api_url = home, revision, api_url
        self.role = "api"
        self.execution_mode = get_settings().run_executor_mode
        self.id = uuid.uuid4()
        self.state = "standby"
        self.signal = asyncio.Event()
        self._liveness = None
        self._listener: asyncpg.Connection | None = None
        self._work: set[str] = set()
        self.tokens: dict[uuid.UUID, uuid.UUID] = {}
        self.http_inflight = 0

    async def register(self) -> None:
        if self._liveness:
            raise RuntimeError("generation is already registered")
        self._liveness = _lock(self.home / "runtime" / f"{self.id}.lock")
        if self._liveness is None:
            raise RuntimeError("generation is already running")
        try:
            async with sessionmaker()() as db:
                control = await _control(db)
                previous = await db.get(RuntimeGeneration, control.active_generation) if control.active_generation else None
                resume_slot = False
                if previous and previous.api_url == self.api_url and previous.revision == self.revision:
                    probe = _lock(self.home / "runtime" / f"{previous.id}.lock")
                    if probe:
                        probe.close()
                        resume_slot = True
                rows = list(await db.scalars(select(RuntimeGeneration).where(
                    RuntimeGeneration.role == "api",
                    RuntimeGeneration.state.in_(("standby", "active", "draining")))))
                live = 0
                for row in rows:
                    probe = _lock(self.home / "runtime" / f"{row.id}.lock")
                    if probe is None:
                        if row.api_url == self.api_url:
                            raise RuntimeError("generation origin already has a live owner")
                        live += 1
                    else:
                        probe.close()
                        row.state = "offline"
                if live >= len(PORTS):
                    raise RuntimeError(f"{len(PORTS)} live generations already exist")
                db.add(RuntimeGeneration(id=self.id, revision=self.revision, api_url=self.api_url,
                    role=self.role, protocol=EXECUTOR_PROTOCOL, execution_mode=self.execution_mode,
                    pid=os.getpid(), process_started_at=psutil.Process().create_time(), state="active" if resume_slot else "standby"))
                if resume_slot:
                    await db.flush()
                    control.active_generation = self.id
                    control.epoch += 1
                    previous.state = "offline"
                    await wake(db)
                await db.commit()
        except BaseException:
            self._liveness.close()
            self._liveness = None
            raise

    async def refresh(self) -> str:
        async with sessionmaker()() as db:
            row = await db.get(RuntimeGeneration, self.id)
            if row is None:
                raise RuntimeError("generation registration disappeared")
            self.state = row.state
            row.heartbeat_at = utcnow()
            await db.commit()
        return self.state

    async def claim(self, run_id: uuid.UUID, lane: str, limit: int) -> uuid.UUID | None:
        if self.role == "api" and self.execution_mode == "remote":
            return None
        async with sessionmaker()() as db:
            control = await _control(db)
            if claim_owner(control) != self.id:
                return None
            run = await db.get(Run, run_id)
            if run is None or run.status != "queued" or (run.resume_after and run.resume_after > utcnow()):
                return None
            if await db.get(RuntimeLease, run_id):
                return None
            if await db.scalar(select(func.count()).select_from(RuntimeLease).where(RuntimeLease.lane == lane)) >= limit:
                return None
            sess = await db.get(ChatSession, run.session_id)
            if sess.kind != SessionKind.desk:
                if await db.scalar(select(RuntimeLease.run_id).where(RuntimeLease.session_id == run.session_id).limit(1)):
                    return None
                first = await db.scalar(select(Run.id).where(Run.session_id == run.session_id,
                    Run.status.in_(("queued", "running"))).order_by(Run.created_at, Run.id).limit(1))
                if first != run_id:
                    return None
            token = uuid.uuid4()
            db.add(RuntimeLease(run_id=run_id, generation_id=self.id, token=token,
                                session_id=run.session_id, lane=lane))
            await db.commit()
        self.tokens[run_id] = token
        return token

    async def fence(self, db: AsyncSession, run_id: uuid.UUID, token: uuid.UUID) -> None:
        # The lease row remains locked until the caller commits its event/result.
        # autoflush must not write stale results before checking ownership.
        with db.no_autoflush:
            row = await db.scalar(select(RuntimeLease).where(RuntimeLease.run_id == run_id).with_for_update())
        if row is None or row.generation_id != self.id or row.token != token:
            await db.rollback()
            raise OwnershipLost(str(run_id))

    async def release(self, run_id: uuid.UUID, token: uuid.UUID) -> None:
        async with sessionmaker()() as db:
            await db.execute(delete(RuntimeLease).where(RuntimeLease.run_id == run_id,
                RuntimeLease.generation_id == self.id, RuntimeLease.token == token))
            await wake(db)
            await db.commit()
        if self.tokens.get(run_id) == token:
            self.tokens.pop(run_id, None)

    async def choose_profile(self, pool, run_id: uuid.UUID, **kwargs):
        from ttakkari.runs import profiles
        async with sessionmaker()() as db:
            await _control(db)
            lease = await db.get(RuntimeLease, run_id)
            if lease is None or lease.generation_id != self.id:
                raise OwnershipLost(str(run_id))
            counts = dict((await db.execute(select(RuntimeLease.account_id, func.count())
                .where(RuntimeLease.account_id.is_not(None), RuntimeLease.run_id != run_id)
                .group_by(RuntimeLease.account_id))).all())
            prof = profiles.choose(pool, run_id, counts_override=counts, **kwargs)
            # Local Qwen has a synthetic routing identity, not an engine_accounts
            # row. Keep its ownership/lane lease without claiming a cloud account.
            lease.account_id = None if prof.engine == "qwen-local" else prof.id
            await db.commit()
            return prof

    async def command(self, kind: str, *, run_id: uuid.UUID | None = None,
                      session_id: uuid.UUID | None = None, key: str | None = None) -> uuid.UUID:
        if kind not in {"cancel", "reset", "release_waiting"}:
            raise ValueError("unsupported runtime command")
        key = key or str(uuid.uuid4())
        async with sessionmaker()() as db:
            control = await _control(db)
            target = claim_owner(control)
            if run_id:
                lease = await db.get(RuntimeLease, run_id)
                target = lease.generation_id if lease else target
            elif session_id:
                target = await db.scalar(select(RuntimeLease.generation_id).where(
                    RuntimeLease.session_id == session_id).limit(1)) or target
            cid = uuid.uuid4()
            cid = await db.scalar(insert(RuntimeCommand).values(id=cid, key=key, kind=kind,
                generation_id=target, run_id=run_id, session_id=session_id).on_conflict_do_update(
                    index_elements=[RuntimeCommand.key], set_={"key": key}).returning(RuntimeCommand.id))
            await wake(db)
            await db.commit()
            return cid

    async def commands(self) -> list[RuntimeCommand]:
        async with sessionmaker()() as db:
            return list(await db.scalars(select(RuntimeCommand).where(
                RuntimeCommand.generation_id == self.id, RuntimeCommand.acknowledged_at.is_(None)
            ).order_by(RuntimeCommand.created_at)))

    async def ack(self, command_id: uuid.UUID) -> None:
        async with sessionmaker()() as db:
            cmd = await db.get(RuntimeCommand, command_id)
            if cmd and cmd.generation_id == self.id:
                cmd.acknowledged_at = utcnow()
                await db.commit()

    @asynccontextmanager
    async def work(self, name: str):
        if not name.replace("-", "").isalnum():
            raise ValueError("invalid work lock name")
        if await self.refresh() != "active":
            yield False
            return
        lock = _lock(self.home / "runtime" / f"work-{name}.lock")
        if lock is None:
            yield False
            return
        self._work.add(name)
        try:
            # Recheck after lock acquisition against an intervening activation.
            yield await self.refresh() == "active"
        finally:
            self._work.discard(name)
            lock.close()

    async def snapshot(self) -> dict:
        await self.refresh()
        async with sessionmaker()() as db:
            owned = await db.scalar(select(func.count()).select_from(RuntimeLease).where(RuntimeLease.generation_id == self.id))
            control = await db.get(RuntimeControl, 1)
        return {"generation": str(self.id), "revision": self.revision, "pid": os.getpid(),
                "role": self.role, "execution_mode": self.execution_mode,
                "executor_protocol": EXECUTOR_PROTOCOL,
                "protocol": PROTOCOL, "state": self.state, "owned_runs": owned,
                "work": sorted(self._work), "http_inflight": self.http_inflight,
                "epoch": control.epoch if control else 0}

    async def can_retire(self) -> bool:
        snap = await self.snapshot()
        return snap["state"] == "draining" and not (snap["owned_runs"] or self._work or self.http_inflight)

    async def listen(self) -> None:
        if self._listener is not None and not self._listener.is_closed():
            return
        url = make_url(get_settings().database_url).set(drivername="postgresql")
        self._listener = await asyncpg.connect(url.render_as_string(hide_password=False))
        def received(_conn, _pid, _channel, payload):
            from ttakkari.runs import events
            self.signal.set()
            with contextlib.suppress(ValueError):
                events.notify(uuid.UUID(payload))
        await self._listener.add_listener(CHANNEL, received)
        self.signal.set()  # Requery after LISTEN: wakeups during reconnect may have been lost.

    async def close(self) -> None:
        if self._listener:
            await self._listener.close()
            self._listener = None
        if self._liveness:
            async with sessionmaker()() as db:
                row = await db.get(RuntimeGeneration, self.id)
                if row:
                    row.state = "offline"
                    await db.commit()
            self._liveness.close()
            self._liveness = None


current: GenerationRuntime | None = None


async def fence_pending(db: AsyncSession) -> None:
    frame = db.info.get("runtime_lease")
    if frame:
        runtime, run_id, token = frame
        await runtime.fence(db, run_id, token)
    elif current:
        owned = {obj.id for obj in (*db.new, *db.dirty) if isinstance(obj, Run) and obj.id in current.tokens}
        for run_id in sorted(owned):
            await current.fence(db, run_id, current.tokens[run_id])


@asynccontextmanager
async def work(name: str):
    if current is None:
        yield True
    else:
        async with current.work(name) as acquired:
            yield acquired
