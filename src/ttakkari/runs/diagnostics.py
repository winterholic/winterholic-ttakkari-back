"""Read-only dispatch observations; unavailable local state is never reported as absence."""

from datetime import UTC, datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from ttakkari import generation
from ttakkari.config import get_settings
from ttakkari.models import (
    Run,
    RunEvent,
    RuntimeControl,
    RuntimeLease,
    SessionKind,
    utcnow,
)
from ttakkari.oplog.scrub import scrub


def _stored_provenance(value):
    """Echo stored args shape; rows written before the key stay None."""
    if not isinstance(value, dict):
        return None
    return {'args_present': bool(value.get('args_present')),
            'args_type': scrub(str(value.get('args_type', '')))[:32],
            'args_parsed': bool(value.get('args_parsed'))}


async def mcp_tool_errors(db, runs) -> dict:
    """Three latest correlated failures per authorized Run; argument values stay private."""
    if not runs:
        return {}
    call, result, intervening = aliased(RunEvent), aliased(RunEvent), aliased(RunEvent)
    name = call.payload['name'].as_string()
    output = result.payload['output'].as_string()
    # Some MCP validation failures were stored as successful tool executions.
    # Match the dispatch envelope and empty arguments, never quoted prose.
    leaf = func.regexp_replace(name, r'^.*(__|\.)', '')
    validation_prefix = 'Error executing tool ' + leaf + ':'
    embedded_validation = (
        (call.payload['input'] == {})
        & (func.left(output, func.length(validation_prefix)) == validation_prefix)
        & output.contains('Field required') & output.contains('input_value={}')
    )
    ranked = select(
        result.run_id, result.seq.label('result_seq'), result.payload.label('result_payload'),
        call.seq.label('call_seq'), call.payload.label('call_payload'),
        func.row_number().over(partition_by=result.run_id, order_by=result.seq.desc()).label('rank'),
    ).join(call, (call.run_id == result.run_id)
           & (call.payload['call_id'].as_string() == result.payload['call_id'].as_string())
           & (call.seq < result.seq)).where(
        result.run_id.in_([run.id for run in runs]),
        call.type == 'tool.call', result.type == 'tool.result',
        ~select(intervening.id).where(
            intervening.run_id == result.run_id, intervening.type == 'tool.call',
            intervening.payload['call_id'].as_string() == result.payload['call_id'].as_string(),
            intervening.seq > call.seq, intervening.seq < result.seq,
        ).exists(),
        or_(name.startswith('mcp__', autoescape=True), name.startswith('mcp.', autoescape=True)),
        or_(result.payload['is_error'].as_boolean().is_(True), embedded_validation),
    ).subquery()
    rows = (await db.execute(select(ranked).where(ranked.c.rank <= 3)
                            .order_by(ranked.c.run_id, ranked.c.result_seq))).mappings()
    observations = {}
    for row in rows:
        called, returned = row['call_payload'], row['result_payload']
        args = called.get('input')
        observations.setdefault(row['run_id'], []).append({
            'call_id': scrub(str(called.get('call_id', '')))[:120],
            'name': scrub(str(called.get('name', '')))[:200],
            'call_seq': row['call_seq'], 'result_seq': row['result_seq'],
            'input_keys': sorted(scrub(str(key))[:80] for key in args)[:20] if isinstance(args, dict) else None,
            'input_empty': args == {}, 'recorded_is_error': returned.get('is_error'),
            'input_provenance': _stored_provenance(called.get('input_provenance')),
            'error_excerpt': scrub(str(returned.get('output') or ''))[:1000],
        })
    return observations


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
