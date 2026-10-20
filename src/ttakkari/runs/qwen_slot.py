"""Local Qwen single-slot state: busy/queue visibility plus routing helpers.

One Mac runs at most one Qwen worker job at a time (user decision 2026-10-19).
"Busy" is read from the DB so every backend generation sees the same slot:
a running Run whose engine is qwen-local occupies it, plus a same-process
claim recorded by profiles.choose (chosen but not running yet). Two claims in
the same instant on different processes can still overlap; callers check at
decision time and again right before launching, and this module makes the
state visible instead of pretending the race is impossible.
"""
from __future__ import annotations

import fcntl
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ChatSession,
    Run,
    RunStatus,
    SessionKind,
    utcnow,
)
from ttakkari.runs import profiles as profiles_mod
from ttakkari.runs import worker_routing

RECENT_RUNS = 200
DURATION_SAMPLES = 20


def process_lock_path() -> Path:
    """Stable path shared by every backend generation on this Mac."""
    return get_settings().home / "run" / "qwen-local-slot.lock"


def acquire_process_lock(path: Path | None = None) -> int | None:
    """Try to claim the machine-wide Qwen slot; None means another process owns it."""
    lock_path = path or process_lock_path()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # If the backend dies while qwen-worker continues, its inherited handle
        # keeps the machine-wide slot occupied until that child exits too.
        os.set_inheritable(fd, True)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def release_process_lock(fd: int) -> None:
    """Release a slot claim. Closing the descriptor also releases it after crashes."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def is_process_lock_busy(path: Path | None = None) -> bool:
    """Probe the machine lock, including an orphaned worker absent from the DB."""
    fd = acquire_process_lock(path)
    if fd is None:
        return True
    release_process_lock(fd)
    return False


def _qwen_profile_ids() -> set[UUID]:
    # Only the personal pool ever routes to Qwen; the id is deterministic per pool.
    return {worker_routing.emergency_qwen_profile("personal").id}


def claimed_elsewhere(exclude_run_id: UUID | None = None) -> UUID | None:
    """A same-process Run (other than exclude) already holding the Qwen profile, if any."""
    wanted = _qwen_profile_ids()
    for rid, aid in profiles_mod._in_use.items():
        if rid != exclude_run_id and aid in wanted:
            return rid
    return None


async def _running_qwen(db: AsyncSession, *, exclude_run_id: UUID | None = None) -> Run | None:
    stmt = (select(Run).where(Run.status == RunStatus.running, Run.engine == "qwen-local")
            .order_by(Run.started_at).limit(2))
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    rows = list(await db.scalars(stmt))
    return rows[0] if rows else None


async def slot_status(db: AsyncSession, *, exclude_run_id: UUID | None = None) -> dict:
    """Busy flag, current holder and explicit-waiter queue for the single Qwen slot."""
    running = await _running_qwen(db, exclude_run_id=exclude_run_id)
    claim = None if running is not None else claimed_elsewhere(exclude_run_id)
    process_busy = is_process_lock_busy()
    queued = list(await db.scalars(
        select(Run).join(ChatSession, ChatSession.id == Run.session_id)
        .where(Run.status == RunStatus.queued, ChatSession.engine == "qwen-local",
               ChatSession.kind == SessionKind.worker)
        .order_by(Run.created_at).limit(20)))
    if exclude_run_id is not None:
        queued = [r for r in queued if r.id != exclude_run_id]
    now = utcnow()
    current = None
    if running is not None:
        started = running.started_at or running.created_at
        current = {"run_id": str(running.id), "session_id": str(running.session_id),
                   "started_at": started.isoformat() if started else None,
                   "elapsed_s": max(0, round((now - started).total_seconds())) if started else None}
    return {"slots": worker_routing.QWEN_SLOTS,
            "busy": running is not None or claim is not None or process_busy,
            "process_lock_busy": process_busy,
            "current": current, "claimed_run_id": str(claim) if claim else None,
            "queue_length": len(queued),
            "queued": [{"run_id": str(r.id), "session_id": str(r.session_id),
                        "waiting_s": max(0, round((now - r.created_at).total_seconds()))}
                       for r in queued]}


async def active_sibling_workers(db: AsyncSession, sess: ChatSession) -> int:
    """Living worker sessions under the same orchestrator, excluding this session."""
    if sess.parent_id is None:
        return 0
    return await db.scalar(select(func.count(func.distinct(ChatSession.id)))
                           .join(Run, Run.session_id == ChatSession.id)
                           .where(ChatSession.parent_id == sess.parent_id, ChatSession.id != sess.id,
                                  ChatSession.kind == SessionKind.worker,
                                  ChatSession.archived_at.is_(None),
                                  Run.status.in_([RunStatus.queued, RunStatus.running]))) or 0


async def median_qwen_run_s(db: AsyncSession) -> float | None:
    rows = list(await db.scalars(
        select(Run).where(Run.status.in_(TERMINAL_RUN_STATUSES),
                          Run.started_at.is_not(None), Run.finished_at.is_not(None))
        .order_by(Run.created_at.desc()).limit(RECENT_RUNS)))
    durations = sorted(
        (r.finished_at - r.started_at).total_seconds()
        for r in rows if (r.usage or {}).get("qwen_attempted") and r.finished_at > r.started_at)
    if not durations:
        return None
    return round(median(durations[-DURATION_SAMPLES:]), 1)


async def estimate_wait_s(db: AsyncSession, slot: dict, *, ahead: int) -> int | None:
    """Honest wait estimate for a newcomer appended behind `ahead` waiters.

    Median of recent Qwen attempt durations minus the current holder's elapsed
    time, plus one median per waiter ahead. None when Qwen never ran here yet
    (no history to extrapolate from) or the slot is free.
    """
    if not slot["busy"]:
        return 0
    typical = await median_qwen_run_s(db)
    if typical is None:
        return None
    elapsed = (slot["current"] or {}).get("elapsed_s") or 0
    return max(0, round(typical - elapsed + typical * max(0, ahead)))


async def recent_stats(db: AsyncSession) -> dict:
    """Qwen selection, verification and cloud-handoff counts over recent worker runs."""
    rows = list(await db.execute(
        select(Run, ChatSession.engine)
        .join(ChatSession, ChatSession.id == Run.session_id)
        .where(ChatSession.kind == SessionKind.worker, ChatSession.pool == "personal",
               Run.status.in_(TERMINAL_RUN_STATUSES))
        .order_by(Run.created_at.desc()).limit(RECENT_RUNS)))
    out = {"window_runs": len(rows), "qwen_runs": 0, "qwen_parallel": 0, "qwen_emergency": 0,
           "qwen_explicit": 0, "cloud_handoffs": 0,
           "verification_required": 0, "verified_passed": 0, "verification_failed": 0,
           "unverified": 0}
    for run, session_engine in rows:
        u = run.usage or {}
        if not u.get("qwen_attempted"):
            continue
        out["qwen_runs"] += 1
        trigger = u.get("routing_trigger")
        if trigger == worker_routing.QWEN_PARALLEL_TRIGGER:
            out["qwen_parallel"] += 1
        elif trigger == "emergency":
            out["qwen_emergency"] += 1
        if session_engine == "qwen-local":
            out["qwen_explicit"] += 1
        if u.get("qwen_cloud_handoff"):
            out["cloud_handoffs"] += 1
        if u.get("verification_required"):
            out["verification_required"] += 1
        status = u.get("verification_status")
        if status == "passed":
            out["verified_passed"] += 1
        elif status == "failed" or u.get("false_completion") is True:
            out["verification_failed"] += 1
        elif u.get("verification_required"):
            out["unverified"] += 1
    return out


async def effective_enabled(db: AsyncSession) -> tuple[bool, bool]:
    """(enabled, config_locked) without running the doctor readiness check."""
    from ttakkari.models import AppSetting
    from ttakkari.runs import qwen_activation
    settings = get_settings()
    locked = qwen_activation.config_locked()
    if locked:
        return settings.qwen_local_enabled, True
    row = await db.get(AppSetting, qwen_activation.KEY)
    enabled = settings.qwen_local_enabled or (row is not None and row.value.get("enabled") is True)
    return enabled, False


async def public_status(db: AsyncSession, pool: str) -> dict:
    """account_usage.qwen_local: slot visibility for the orchestrator. Company scope sees nothing usable."""
    if pool != "personal":
        return {"available": False, "reason": "company_scope_excluded"}
    enabled, locked = await effective_enabled(db)
    slot = await slot_status(db)
    return {
        "available": True, "enabled": enabled, "config_locked": locked,
        "model": worker_routing.qwen_local_model(), "slots": slot["slots"],
        "busy": slot["busy"], "process_lock_busy": slot["process_lock_busy"],
        "current": slot["current"], "queue_length": slot["queue_length"],
        "queued": slot["queued"],
        "estimated_wait_s": await estimate_wait_s(db, slot, ahead=slot["queue_length"]),
        "median_qwen_run_s": await median_qwen_run_s(db),
        "policy": {"tasks": list(worker_routing.QWEN_PARALLEL_TASKS), "kinds": ["worker"],
                   "requires_parallel_siblings": 1,
                   "requires_fit": ["needs_mcp=false", "narrow_scope=true", "verifiable=true"],
                   "orchestrator": "emergency_only_lowest_priority"},
        "recent": await recent_stats(db),
        # doctor readiness is checked at routing time, not by this status read.
        "readiness": "checked_at_routing",
        "measured_at": datetime.fromtimestamp(time.time(), UTC).isoformat(timespec="seconds"),
    }
