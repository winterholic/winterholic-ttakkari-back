"""Read-only dispatch observations; unavailable local state is never reported as absence."""

from datetime import UTC, datetime

from sqlalchemy import func, select

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.models import Run, RuntimeControl, RuntimeLease, SessionKind, utcnow


def _await_chain(task) -> list[str]:
    """Code names only: no source, repr, paths, arguments, or frame locals."""
    cursor = task.get_coro()
    names = []
    for _ in range(8):
        code = getattr(cursor, "cr_code", None) or getattr(cursor, "gi_code", None)
        if code is None:
            break
        names.append(code.co_name[:80])
        cursor = getattr(cursor, "cr_await", None) or getattr(cursor, "gi_yieldfrom", None)
        if cursor is None:
            break
    return names


async def session_execution(db, sess, runs) -> tuple[dict, dict]:
    from ttakkari.runs.manager import manager

    control = await db.scalar(select(RuntimeControl).limit(1))
    owner = generation.claim_owner(control) if control else None
    runtime = manager.runtime
    local = not manager.remote and (
        (runtime is None and owner is None) or (runtime is not None and owner == runtime.id)
    )
    lane = {SessionKind.orchestrator: "leads", SessionKind.desk: "desk"}.get(sess.kind, "workers")
    settings = get_settings()
    limit = {"leads": settings.max_concurrent_leads, "desk": settings.max_concurrent_desk}.get(
        lane, settings.max_concurrent_workers)
    leases = await db.scalar(select(func.count()).select_from(RuntimeLease).where(RuntimeLease.lane == lane))
    head = await db.scalar(select(Run.id).where(
        Run.session_id == sess.id, Run.status.in_(("queued", "running")),
    ).order_by(Run.created_at).limit(1))
    context = {
        "observed_at": utcnow().isoformat(), "local_coordinator": local,
        "observer_generation_id": str(runtime.id) if runtime else None,
        "claim_owner_generation_id": str(owner) if owner else None,
        "remote": manager.remote, "shutting_down": manager._shutting_down,
        "draining": bool(runtime and getattr(runtime, "draining", False)),
        "lane": lane, "lane_leases": leases, "lane_limit": limit,
        "head_run_id": str(head) if head else None,
    }
    observations = {}
    for run in runs:
        obs = None
        if local:
            handle = manager._handles.get(run.id)
            task = handle.task if handle else None
            armed = manager._waiting.get(run.id)
            obs = {
                "task_exists": task is not None, "task_done": task.done() if task else None,
                "armed_until": datetime.fromtimestamp(armed[0], UTC).isoformat() if armed else None,
                "await_chain": _await_chain(task) if task else [],
            }
        observations[run.id] = obs
    return context, observations
