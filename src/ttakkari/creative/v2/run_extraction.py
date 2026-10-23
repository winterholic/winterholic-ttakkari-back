"""Creative intent extraction through Ttakkari's persisted Run lifecycle."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.api.sessions import queue_run
from ttakkari.api.workspaces import ensure_default_workspace
from ttakkari.config import get_settings
from ttakkari.creative.v2.catalog_data import CATALOG_NAMES, ENTRIES
from ttakkari.creative.v2.extraction import (
    IntentExtractionRejected,
    build_intent_prompt,
    parse_intent_response,
)
from ttakkari.models import ChatSession, Run, RunStatus, SessionKind
from ttakkari.runs import qwen_activation, worker_routing

TASK_TYPE = "creative_intent"
TASK_MARKER = "creative_intent"
TERMINAL = {RunStatus.succeeded, RunStatus.failed, RunStatus.cancelled, RunStatus.interrupted}


def _assert_intent_run(session: ChatSession) -> None:
    if not (
        session.worker_task_type == TASK_TYPE
        and session.engine == "qwen-local"
        and session.kind == SessionKind.worker
        and session.pool == "personal"
    ):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "의도 분석 Run을 찾을 수 없습니다.")


def _intent_metadata(brief: str) -> dict[str, Any]:
    return {TASK_MARKER: {"schema_version": 1, "brief": brief}}


async def _queue_intent(db: AsyncSession, session: ChatSession, brief: str, *, request_key: str | None = None) -> Run:
    prompt = build_intent_prompt(brief)

    def mark(run: Run) -> None:
        run.usage = _intent_metadata(brief)

    return await queue_run(
        db,
        session,
        prompt,
        model=worker_routing.qwen_local_model(),
        request_key=request_key,
        on_queued=mark,
    )


async def start_intent_run(db: AsyncSession, brief: str, *, request_key: str | None = None) -> dict[str, Any]:
    if request_key:
        existing = await db.scalar(select(Run).where(Run.request_key == request_key))
        if existing is not None:
            existing_session = await db.get(ChatSession, existing.session_id)
            if existing_session is None:
                raise HTTPException(status.HTTP_409_CONFLICT, "요청 키가 다른 작업에 사용됐습니다.")
            _assert_intent_run(existing_session)
            metadata = (existing.usage or {}).get(TASK_MARKER)
            if not isinstance(metadata, dict) or metadata.get("brief") != brief:
                raise HTTPException(status.HTTP_409_CONFLICT, "같은 요청 키가 다른 브리프에 사용됐습니다.")
            return {
                "session_id": str(existing_session.id), "run_id": str(existing.id),
                "status": str(existing.status), "engine": existing.engine, "model": existing.model,
            }
    engine_status = await qwen_activation.status(db)
    if not engine_status["ready"]:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "intent_engine_unavailable", "message": f"기존 Qwen Run 엔진을 사용할 수 없습니다: {engine_status['reason']}"},
        )
    model = worker_routing.qwen_local_model()
    if model not in get_settings().engine_models.get("qwen-local", []):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "intent_engine_unavailable", "message": "Qwen 모델이 Run 허용 목록에 없습니다."},
        )
    workspace = await ensure_default_workspace(db)
    session = ChatSession(
        workspace_id=workspace.id,
        title="Creative Studio 의도 검토",
        engine="qwen-local",
        model=model,
        kind=SessionKind.worker,
        pool="personal",
        worker_task_type=TASK_TYPE,
        strict_engine=True,
    )
    db.add(session)
    await db.flush()
    run = await _queue_intent(db, session, brief, request_key=request_key)
    return {
        "session_id": str(session.id),
        "run_id": str(run.id),
        "status": str(run.status),
        "engine": "qwen-local",
        "model": model,
    }


async def resume_intent_run(db: AsyncSession, run_id: uuid.UUID, *, request_key: str | None = None) -> dict[str, Any]:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "의도 분석 Run을 찾을 수 없습니다.")
    session = await db.get(ChatSession, run.session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "의도 분석 Run을 찾을 수 없습니다.")
    _assert_intent_run(session)
    if run.status not in TERMINAL:
        raise HTTPException(status.HTTP_409_CONFLICT, "실행 중인 Run은 다시 시작할 수 없습니다.")
    metadata = (run.usage or {}).get(TASK_MARKER)
    brief = metadata.get("brief") if isinstance(metadata, dict) else None
    if not isinstance(brief, str) or not brief.strip():
        raise HTTPException(status.HTTP_409_CONFLICT, "재개할 브리프 기록이 없습니다.")
    new_run = await _queue_intent(db, session, brief, request_key=request_key)
    return {
        "session_id": str(session.id),
        "run_id": str(new_run.id),
        "status": str(new_run.status),
        "engine": "qwen-local",
        "model": worker_routing.qwen_local_model(),
        "resumed_from": str(run.id),
    }


async def read_intent_run(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "의도 분석 Run을 찾을 수 없습니다.")
    session = await db.get(ChatSession, run.session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "의도 분석 Run을 찾을 수 없습니다.")
    _assert_intent_run(session)
    result: dict[str, Any] = {
        "session_id": str(session.id),
        "run_id": str(run.id),
        "status": str(run.status),
        "engine": run.engine,
        "model": run.model,
        "profile": run.profile,
        "error": run.error,
        "intent": None,
    }
    if run.status != RunStatus.succeeded:
        return result
    metadata = (run.usage or {}).get(TASK_MARKER)
    brief = metadata.get("brief") if isinstance(metadata, dict) else None
    if not isinstance(brief, str) or not run.result_text:
        result["error"] = "invalid_structured_output: Run 결과나 원문 브리프가 없습니다."
        return result
    try:
        intent = parse_intent_response(brief, run.result_text, model=run.model)
    except IntentExtractionRejected as exc:
        result["error"] = f"invalid_structured_output: {exc}"
        return result
    from ttakkari.creative.v2.intent import validate_intent

    result.update(
        {
            "intent": intent.model_dump(mode="json"),
            "validation": validate_intent(brief, intent).model_dump(mode="json"),
            "brief": brief,
            "selection_labels": [
                {
                    "entry_id": selection.entry_id,
                    "catalog": CATALOG_NAMES[ENTRIES[selection.entry_id].catalog],
                    "name": ENTRIES[selection.entry_id].name,
                }
                for selection in intent.selections
                if selection.entry_id in ENTRIES
            ],
            "review_required": True,
        }
    )
    return result
