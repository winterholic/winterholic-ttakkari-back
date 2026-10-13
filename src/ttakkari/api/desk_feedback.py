"""접수 분류 판정(ADR 0013). 사용자가 접수의 answer/delegate 가 맞았는지 남기고, 그 모음을 평가 문항으로 꺼낸다.

합성 문항만으로는 지침·모델을 바꿀 근거가 약해 실제 메시지에 대한 판정을 모은다."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.models import (
    TERMINAL_RUN_STATUSES,
    ChatSession,
    DeskFeedback,
    Run,
    RunEvent,
    RunOrigin,
    SessionKind,
    utcnow,
)
from ttakkari.runs import desk, desk_eval, events
from ttakkari.schemas import (
    DeskEvalItem,
    DeskFeedbackBrief,
    DeskFeedbackIn,
    DeskFeedbackOut,
    RunOut,
)
from ttakkari.security.auth import User

router = APIRouter(prefix="/api", tags=["desk"])


def verdict_of(decided: str, expected: str) -> str:
    return "ok" if decided == expected else f"should_{expected}"


def brief(fb: DeskFeedback) -> DeskFeedbackBrief:
    return DeskFeedbackBrief(verdict=verdict_of(fb.decided, fb.expected), decided=fb.decided, expected=fb.expected,
                             note=fb.note, updated_at=fb.updated_at)


async def runs_out(db: AsyncSession, runs: list[Run]) -> list[RunOut]:
    """RunOut 에 그 Run 의 판정을 붙인다. 판정은 접수 Run 에만 생기므로 run_id 로 한 번에 찾는다."""
    found = {fb.run_id: fb for fb in await db.scalars(
        select(DeskFeedback).where(DeskFeedback.run_id.in_([r.id for r in runs])))} if runs else {}
    out = []
    for r in runs:
        o = RunOut.model_validate(r)
        if r.id in found:
            o.desk_feedback = brief(found[r.id])
        out.append(o)
    return out


async def _last_event(db: AsyncSession, run_id: uuid.UUID, type_: str) -> RunEvent | None:
    return await db.scalar(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.type == type_)
                           .order_by(RunEvent.seq.desc()).limit(1))


@router.post("/runs/{run_id}/desk-feedback", response_model=DeskFeedbackOut)
async def put_feedback(run_id: uuid.UUID, body: DeskFeedbackIn, _: User, db: Db) -> DeskFeedbackOut:
    run = await db.get(Run, run_id)
    if run is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Run 이 없습니다.")
    sess = await db.get(ChatSession, run.session_id)
    if sess is None or sess.kind != SessionKind.desk or run.origin != RunOrigin.user:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "접수 세션의 사용자 메시지에만 판정할 수 있습니다.")
    route = await _last_event(db, run.id, "desk.route")
    # 끝나기 전 Run 에 이벤트를 붙이면 실행 태스크와 seq 가 겹친다.
    if route is None or run.status not in TERMINAL_RUN_STATUSES:
        raise HTTPException(status.HTTP_409_CONFLICT, "접수가 아직 이 메시지를 처리하지 않았습니다.")
    decided = route.payload.get("action")
    expected = decided if body.verdict == "ok" else body.verdict.removeprefix("should_")
    if body.verdict != "ok" and expected == decided:
        raise HTTPException(status.HTTP_409_CONFLICT, f"접수가 이미 {decided} 로 처리했습니다. '맞아'로 보내 주세요.")

    now = utcnow()
    stmt = insert(DeskFeedback).values(id=uuid.uuid4(), run_id=run.id, session_id=sess.id, message=run.prompt,
                                       decided=decided, expected=expected, note=body.note, created_at=now,
                                       updated_at=now)
    await db.execute(stmt.on_conflict_do_update(
        index_elements=[DeskFeedback.run_id],
        set_={"decided": decided, "expected": expected, "note": body.note, "updated_at": now}))

    lead_id: uuid.UUID | None = None
    if body.redo and body.verdict == "should_delegate":
        done = await _last_event(db, run.id, "desk.delegated")
        if done is not None:
            # 다시 눌러도 기획 세션을 또 만들지 않는다.
            lead_id = uuid.UUID(done.payload["lead_id"])
        else:
            # 작업판을 비워 넘긴다. 바로 답했던 메시지라 이어 맡을 기획 세션을 고른 판단이 없다.
            lead, _dedup = await desk._delegate(db, run, sess, desk.Decision("delegate", ""))
            events.append(db, run, "desk.delegated", {"lead_id": str(lead.id), "title": lead.title, "redo": True})
            lead_id = lead.id
    record(db, action="desk.feedback", target_type="run", target_id=run.id,
           detail={"decided": decided, "expected": expected, "redo": lead_id is not None})
    await db.commit()
    events.notify(run.id)

    fb = await db.scalar(select(DeskFeedback).where(DeskFeedback.run_id == run.id)
                         .execution_options(populate_existing=True))
    return _out(fb, lead_id)


def _out(fb: DeskFeedback, lead_id: uuid.UUID | None = None) -> DeskFeedbackOut:
    return DeskFeedbackOut(**brief(fb).model_dump(), run_id=fb.run_id, session_id=fb.session_id, message=fb.message,
                           created_at=fb.created_at, lead_id=lead_id)


@router.get("/desk/feedback", response_model=list[DeskFeedbackOut])
async def list_feedback(_: User, db: Db, limit: int = Query(50, ge=1, le=500)) -> list[DeskFeedbackOut]:
    rows = await db.scalars(select(DeskFeedback).order_by(DeskFeedback.updated_at.desc()).limit(limit))
    return [_out(fb) for fb in rows]


@router.get("/desk/eval-set", response_model=list[DeskEvalItem])
async def get_eval_set(_: User, db: Db, format: str = Query("json", pattern="^(json|jsonl)$"),
                       limit: int | None = Query(None, ge=1, le=5000)) -> list[DeskEvalItem] | Response:
    items = [DeskEvalItem(message=m, expected=e) for m, e in await desk_eval.eval_set(db, limit)]
    if format == "jsonl":
        body = "".join(json.dumps(i.model_dump(), ensure_ascii=False) + "\n" for i in items)
        return Response(body, media_type="application/x-ndjson")
    return items
