"""Fail-closed recovery for quarantined executor leases."""
from __future__ import annotations

import asyncio
import errno
import os
import signal
import uuid

import psutil
from sqlalchemy import delete, select

from ttakkari.db import sessionmaker
from ttakkari.generation import _control
from ttakkari.models import (
    Run,
    RunStatus,
    RuntimeGeneration,
    RuntimeLease,
    RuntimeProcessGroup,
    utcnow,
)


def disposition(*, owner_dead: bool | None, groups: list[tuple[str, bool]]) -> str:
    if owner_dead is None:
        return "unknown_owner"
    if not owner_dead:
        return "owner_alive"
    if not groups:
        return "missing_process_group_evidence"
    if any(not identity_ok or state == "unknown" for state, identity_ok in groups):
        return "unknown_process_group"
    if any(state == "present" for state, _ in groups):
        return "process_group_live"
    return "safe_to_release"


def _process_dead(pid: int, started_at: float) -> bool | None:
    try:
        process = psutil.Process(pid)
        actual_started_at = process.create_time()
    except psutil.NoSuchProcess:
        return True
    except (psutil.AccessDenied, OSError):
        return None
    return abs(actual_started_at - started_at) > 1.0


def _group_state(record: RuntimeProcessGroup) -> tuple[str, bool]:
    try:
        os.killpg(record.pgid, 0)
    except ProcessLookupError:
        return "absent", True
    except PermissionError:
        return "unknown", False
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return "absent", True
        return "unknown", False
    try:
        process = psutil.Process(record.pid)
        same_process = abs(process.create_time() - record.process_started_at) <= 1.0
        same_group = os.getpgid(record.pid) == record.pgid
    except psutil.NoSuchProcess:
        return "unknown", False
    except (psutil.AccessDenied, OSError, PermissionError):
        return "unknown", False
    return ("present", True) if same_process and same_group else ("unknown", False)


async def _stop_verified_group(record: RuntimeProcessGroup) -> bool:
    state, identity_ok = _group_state(record)
    if state == "absent":
        return True
    if state != "present" or not identity_ok:
        return False
    try:
        os.killpg(record.pgid, signal.SIGTERM)
    except (ProcessLookupError, OSError, PermissionError):
        return _group_state(record)[0] == "absent"
    for _ in range(50):
        await asyncio.sleep(0.1)
        state, identity_ok = _group_state(record)
        if state == "absent":
            return True
        if state != "present" or not identity_ok:
            return False
    # Escalate only while the original group leader still matches its recorded PID/start-time/PGID.
    state, identity_ok = _group_state(record)
    if state != "present" or not identity_ok:
        return False
    try:
        os.killpg(record.pgid, signal.SIGKILL)
    except (ProcessLookupError, OSError, PermissionError):
        return _group_state(record)[0] == "absent"
    for _ in range(50):
        await asyncio.sleep(0.1)
        state, identity_ok = _group_state(record)
        if state == "absent":
            return True
        if state != "present" or not identity_ok:
            return False
    return False


async def _read_orphans() -> list[dict]:
    async with sessionmaker()() as db:
        leases = list(await db.scalars(select(RuntimeLease).order_by(RuntimeLease.run_id)))
        result = []
        for lease in leases:
            owner = await db.get(RuntimeGeneration, lease.generation_id)
            groups = list(await db.scalars(select(RuntimeProcessGroup).where(
                RuntimeProcessGroup.run_id == lease.run_id,
                RuntimeProcessGroup.generation_id == lease.generation_id,
                RuntimeProcessGroup.lease_token == lease.token).order_by(RuntimeProcessGroup.attempt)))
            owner_dead = _process_dead(owner.pid, owner.process_started_at) if owner else None
            states = [_group_state(group) for group in groups]
            result.append({"run_id": str(lease.run_id), "owner_id": str(lease.generation_id),
                "owner_dead": owner_dead, "lease_token_matches": bool(owner and owner.id == lease.generation_id),
                "groups": [{"attempt": group.attempt, "pid": group.pid, "pgid": group.pgid,
                            "state": state, "identity_verified": identity_ok}
                           for group, (state, identity_ok) in zip(groups, states, strict=True)],
                "disposition": disposition(owner_dead=owner_dead, groups=states),
                "_lease_token": lease.token, "_group_rows": groups})
        return result


async def _apply_one(entry: dict) -> bool:
    if entry["disposition"] not in {"process_group_live", "safe_to_release"}:
        return False
    groups: list[RuntimeProcessGroup] = entry["_group_rows"]
    if _process_dead_for_entry(entry) is not True:
        return False
    for group in groups:
        state, _ = _group_state(group)
        if state == "present" and not await _stop_verified_group(group):
            return False
        if _group_state(group)[0] != "absent":
            return False

    run_id = uuid.UUID(entry["run_id"])
    token = entry["_lease_token"]
    async with sessionmaker()() as db:
        await _control(db)
        lease = await db.scalar(select(RuntimeLease).where(RuntimeLease.run_id == run_id).with_for_update())
        if lease is None or lease.generation_id != uuid.UUID(entry["owner_id"]) or lease.token != token:
            return False
        owner = await db.get(RuntimeGeneration, lease.generation_id)
        if owner is None or _process_dead(owner.pid, owner.process_started_at) is not True:
            return False
        current_groups = list(await db.scalars(select(RuntimeProcessGroup).where(
            RuntimeProcessGroup.run_id == run_id, RuntimeProcessGroup.generation_id == lease.generation_id,
            RuntimeProcessGroup.lease_token == token).with_for_update()))
        if ({(x.attempt, x.pid, x.process_started_at, x.pgid) for x in current_groups}
                != {(x.attempt, x.pid, x.process_started_at, x.pgid) for x in groups}
                or any(_group_state(x)[0] != "absent" for x in current_groups)):
            return False
        run = await db.get(Run, run_id)
        if run is None or run.status != RunStatus.running:
            return False
        from ttakkari.runs import events
        from ttakkari.runs.manager import _interrupted_handoff
        run.status = RunStatus.interrupted
        run.pid = None
        run.heartbeat_at = utcnow()
        run.finished_at = utcnow()
        run.error = "executor orphan reconciled after recorded process groups exited"
        run.resume_handoff = await _interrupted_handoff(db, run)
        events.append(db, run, "run.finished", {"status": RunStatus.interrupted, "error": run.error,
                                                  "result_text": None, "cost_usd": None, "exit_code": None})
        await db.execute(delete(RuntimeProcessGroup).where(
            RuntimeProcessGroup.run_id == run_id, RuntimeProcessGroup.lease_token == token))
        await db.delete(lease)
        await db.commit()
    return True


def _process_dead_for_entry(entry: dict) -> bool | None:
    if entry["owner_dead"] is not True:
        return entry["owner_dead"]
    return True


async def reconcile_orphans(*, apply: bool = False) -> dict:
    entries = await _read_orphans()
    results = []
    for entry in entries:
        changed = await _apply_one(entry) if apply else False
        results.append({"run_id": entry["run_id"], "owner_id": entry["owner_id"],
            "disposition": entry["disposition"], "groups": entry["groups"], "reconciled": changed,
            "action": "interrupted_and_released" if changed else ("would_reconcile" if
                entry["disposition"] in {"safe_to_release", "process_group_live"} else "blocked")})
    return {"mode": "apply" if apply else "dry-run", "leases": results,
            "reconciled": sum(row["reconciled"] for row in results)}
