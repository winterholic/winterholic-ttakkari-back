"""Storyboard jobs reuse the session Run queue, cancellation and account routing."""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from sqlalchemy import select

from ttakkari.api.sessions import queue_run
from ttakkari.api.workspaces import ensure_default_workspace
from ttakkari.config import get_settings
from ttakkari.creative.v2.storyboard import (
    StoryboardRequest,
    build_prompt,
    materialize,
    output_hash,
    parse_storyboard,
)
from ttakkari.models import ChatSession, Run, RunStatus, SessionKind
from ttakkari.runs import qwen_activation

MARKER = "creative_storyboard"
TERMINAL = {
    RunStatus.succeeded,
    RunStatus.failed,
    RunStatus.cancelled,
    RunStatus.interrupted,
}


def _summary(run, **extra):
    return {
        "run_id": str(run.id),
        "session_id": str(run.session_id),
        "engine": run.engine,
        "model": run.model or (run.usage or {}).get("selected_model"),
        "status": str(run.status),
        **extra,
    }


async def _load(db, run_id):
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "storyboard Run not found")
    session = await db.get(ChatSession, run.session_id)
    meta = (run.usage or {}).get(MARKER)
    if session is None or session.pool != "personal" or not isinstance(meta, dict):
        raise HTTPException(404, "storyboard Run not found")
    request = StoryboardRequest.model_validate(meta["request"])
    if (
        run.engine != request.engine
        or meta.get("prompt_sha256") != output_hash(run.prompt)
        or meta.get("request_sha256") != output_hash(request.model_dump_json())
        or (session.worker_task_type != MARKER and not meta.get("adopted_from_generic"))
    ):
        raise HTTPException(409, "storyboard provenance mismatch")
    return run, session, request


async def _queue(db, session, request, request_key=None, resumed_from=None):
    settings = get_settings()
    models = settings.engine_models.get(request.engine, [])
    model = (
        request.model
        or settings.worker_default_models.get(request.engine, {}).get("model")
        or (models[0] if models else None)
    )
    if model not in models:
        raise HTTPException(503, "storyboard engine/model is not allowed")
    if request.engine == "qwen-local":
        ready = await qwen_activation.status(db)
        if not ready["ready"]:
            raise HTTPException(
                503,
                {"code": "storyboard_engine_unavailable", "reason": ready["reason"]},
            )

    def mark(run):
        run.usage = {
            **(run.usage or {}),
            MARKER: {
                "schema_version": 1,
                "request": request.model_dump(mode="json"),
                "prompt_sha256": output_hash(build_prompt(request)),
                "request_sha256": output_hash(request.model_dump_json()),
                "resumed_from": resumed_from,
            },
        }

    run = await queue_run(
        db,
        session,
        build_prompt(request),
        model=model,
        request_key=request_key,
        on_queued=mark,
    )
    return _summary(run, **({"resumed_from": resumed_from} if resumed_from else {}))


async def start(db, request: StoryboardRequest, *, request_key=None):
    if request_key:
        run = await db.scalar(select(Run).where(Run.request_key == request_key))
        if run:
            old, _, body = await _load(db, run.id)
            if body != request:
                raise HTTPException(
                    409, "request key belongs to another storyboard input"
                )
            return _summary(old)
    workspace = await ensure_default_workspace(db)
    session = ChatSession(
        workspace_id=workspace.id,
        title="Creative storyboard review",
        engine=request.engine,
        kind=SessionKind.worker,
        pool="personal",
        worker_task_type=MARKER,
        strict_engine=True,
    )
    db.add(session)
    await db.flush()
    return await _queue(db, session, request, request_key=request_key)


async def resume(db, run_id, *, request_key=None):
    run, session, request = await _load(db, run_id)
    if run.status not in TERMINAL:
        raise HTTPException(409, "active storyboard Run cannot resume")
    return await _queue(
        db, session, request, request_key=request_key, resumed_from=str(run.id)
    )


async def read(db, run_id):
    run, _, request = await _load(db, run_id)
    result = _summary(
        run,
        phase=str(run.status),
        error=run.error,
        review_required=True,
        storyboard=None,
        sources=[s.model_dump(mode="json") for s in request.sources],
        resumed_from=(run.usage or {})[MARKER].get("resumed_from"),
    )
    if run.status == RunStatus.succeeded:
        try:
            draft = parse_storyboard(request, run.result_text or "")
        except ValueError as exc:
            result.update(phase="invalid_output", error=str(exc))
        else:
            result.update(
                phase="completed_needs_review",
                storyboard=draft.model_dump(mode="json"),
                output_sha256=output_hash(run.result_text),
            )
    return result


async def adopt(db, run_id, request):
    """Owner-only import of an existing normal Run; never accepts result_text from a client."""
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(404, "Run not found")
    session = await db.get(ChatSession, run.session_id)
    if (
        session is None
        or session.pool != "personal"
        or run.engine != request.engine
        or run.prompt != build_prompt(request)
    ):
        raise HTTPException(
            409, "Run engine/prompt does not match the exact storyboard request"
        )
    if MARKER in (run.usage or {}):
        _, _, old = await _load(db, run_id)
        if old != request:
            raise HTTPException(409, "storyboard input already bound")
    else:
        run.usage = {
            **(run.usage or {}),
            MARKER: {
                "schema_version": 1,
                "request": request.model_dump(mode="json"),
                "prompt_sha256": output_hash(run.prompt),
                "request_sha256": output_hash(request.model_dump_json()),
                "adopted_from_generic": True,
            },
        }
        await db.commit()
    return await read(db, run_id)


async def accept(
    db, plans, run_id, *, reviewed_output_sha256, plan_id=None, revision=None
):
    run, _, request = await _load(db, run_id)
    if run.status != RunStatus.succeeded:
        raise HTTPException(409, "only a succeeded storyboard Run can be reviewed")
    return await asyncio.to_thread(
        materialize,
        plans,
        request,
        run.result_text or "",
        run_id=str(run.id),
        model=run.model or (run.usage or {}).get("selected_model") or "unreported",
        reviewed_output_sha256=reviewed_output_sha256,
        plan_id=plan_id,
        revision=revision,
    )


async def assert_owned_run(db, run_id):
    await _load(db, run_id)
