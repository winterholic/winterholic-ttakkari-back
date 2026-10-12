"""회의록 API(ADR 0010). 사용자 화면용 /api/meetings 와 에이전트 읽기 전용 /api/agent/meetings.

제안의 승인·거절과 항목 수정은 사용자 API 에만 있다. 에이전트 쪽에는 쓰기 경로가 없다.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.context import store
from ttakkari.meetings import pipeline, providers, read
from ttakkari.models import ChatSession, Meeting, MeetingItem, MeetingStatus
from ttakkari.orchestrator.runtime import OrchestratorSession as AgentSession
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api/meetings", tags=["meetings"])
agent_router = APIRouter(prefix="/api/agent/meetings", tags=["agent"])
Scope = Literal["all", "personal", "work"]
ItemStatus = Literal["open", "done", "cancelled"]


class MeetingIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    scope: Literal["personal", "work"] = "personal"
    project_id: uuid.UUID | None = None
    held_at: datetime | None = None
    participants: list[str] = Field(default_factory=list, max_length=50)
    # 먼저 /api/uploads 로 올린 녹음 파일(보관함 Artifact). 전사 텍스트와 같이 줘도 된다.
    audio_artifact_id: uuid.UUID | None = None
    transcript: str | None = Field(default=None, max_length=2_000_000)
    transcript_source: Literal["paste", "file"] = "paste"


class MeetingPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    project_id: uuid.UUID | None = None
    held_at: datetime | None = None
    participants: list[str] | None = Field(default=None, max_length=50)


class TranscriptIn(BaseModel):
    text: str = Field(min_length=1, max_length=2_000_000)
    source: Literal["paste", "file"] = "paste"


class ItemPatch(BaseModel):
    status: ItemStatus | None = None
    text: str | None = Field(default=None, min_length=1, max_length=1500)
    owner: str | None = Field(default=None, max_length=100)
    due: str | None = Field(default=None, max_length=40)


def _http(e: pipeline.PipelineError) -> HTTPException:
    return HTTPException(e.status, {"code": e.code, "message": str(e)})


async def _get(db: AsyncSession, mid: uuid.UUID) -> Meeting:
    m = await db.get(Meeting, mid)
    if m is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "회의록이 없어요.")
    return m


async def _fresh(db: AsyncSession, m: Meeting) -> Meeting:
    """진행 중이던 단계가 끝났으면 먼저 반영한다(서버가 재시작해도 조회가 이어받는다)."""
    if m.status in (MeetingStatus.analyzing, MeetingStatus.transcribing):
        await pipeline.reconcile(db, m)
    return m


@router.get("/providers")
async def list_providers(_: User) -> dict[str, Any]:
    """전사 Provider 상태. 경로·인증 정보는 담지 않는다."""
    return {"providers": providers.describe_providers()}


@router.get("")
async def list_all(_: User, db: Db, scope: Scope = "all", q: Annotated[str | None, Query(max_length=200)] = None,
                   status_: Annotated[str | None, Query(alias="status")] = None, project_id: uuid.UUID | None = None,
                   limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    rows = await read.list_meetings(db, scope=scope, q=q, status=status_, entity_id=project_id, limit=limit, offset=offset)
    for m in rows:
        await _fresh(db, m)
    return {"meetings": await read.meeting_rows(db, rows)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create(body: MeetingIn, _: User, db: Db, request: Request) -> dict[str, Any]:
    try:
        m = await pipeline.create_meeting(
            db, title=body.title, scope=body.scope, entity_id=body.project_id, held_at=body.held_at,
            participants=body.participants, audio_artifact_id=body.audio_artifact_id, transcript=body.transcript,
            transcript_source=body.transcript_source)
    except pipeline.PipelineError as e:
        raise _http(e) from e
    record(db, action="meeting.create", target_type="meeting", target_id=m.id, ip=client_ip(request),
           detail={"scope": m.scope, "source": m.source, "status": m.status})
    await db.commit()
    return await read.meeting_detail(db, m)


@router.get("/{mid}")
async def get(mid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    return await read.meeting_detail(db, await _fresh(db, await _get(db, mid)))


@router.patch("/{mid}")
async def patch(mid: uuid.UUID, body: MeetingPatch, _: User, db: Db) -> dict[str, Any]:
    m = await _get(db, mid)
    data = body.model_dump(exclude_unset=True)
    try:
        if "project_id" in data:
            await pipeline.check_project(db, data["project_id"], m.scope)
            m.entity_id = data["project_id"]
    except pipeline.PipelineError as e:
        raise _http(e) from e
    if data.get("title"):
        m.title = data["title"].strip()[:200]
    if "held_at" in data:
        m.held_at = data["held_at"]
    if data.get("participants") is not None:
        m.participants = [p.strip() for p in data["participants"] if p.strip()][:50]
    await db.commit()
    return await read.meeting_detail(db, m)


@router.delete("/{mid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(mid: uuid.UUID, _: User, db: Db, request: Request) -> None:
    """회의록과 항목·제안을 지운다. 이미 승인해 만든 기억과 보관함의 녹음 파일은 남는다."""
    m = await _get(db, mid)
    if m.status == MeetingStatus.analyzing:
        await pipeline.cancel_analysis(db, m)
    record(db, action="meeting.delete", target_type="meeting", target_id=m.id, ip=client_ip(request))
    await db.delete(m)
    await db.commit()


@router.post("/{mid}/transcript")
async def put_transcript(mid: uuid.UUID, body: TranscriptIn, _: User, db: Db) -> dict[str, Any]:
    m = await _get(db, mid)
    try:
        await pipeline.set_transcript(db, m, body.text, body.source)
    except pipeline.PipelineError as e:
        raise _http(e) from e
    await db.commit()
    return await read.meeting_detail(db, m)


@router.post("/{mid}/transcribe", status_code=status.HTTP_202_ACCEPTED)
async def transcribe(mid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    """녹음을 전사 Provider 에 맡긴다. 클로바노트가 준비되기 전에는 409 provider_unavailable 과 사유를 돌려준다."""
    m = await _get(db, mid)
    try:
        await pipeline.request_transcription(db, m)
    except pipeline.PipelineError as e:
        raise _http(e) from e
    await db.commit()
    return await read.meeting_detail(db, m)


@router.post("/{mid}/analyze", status_code=status.HTTP_202_ACCEPTED)
async def analyze(mid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    m = await _get(db, mid)
    try:
        await pipeline.start_analysis(db, m)
    except pipeline.PipelineError as e:
        raise _http(e) from e
    return await read.meeting_detail(db, m)


@router.post("/{mid}/analyze/cancel")
async def cancel_analyze(mid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    m = await _get(db, mid)
    try:
        await pipeline.cancel_analysis(db, m)
    except pipeline.PipelineError as e:
        raise _http(e) from e
    return await read.meeting_detail(db, m)


@router.patch("/{mid}/items/{item_id}")
async def patch_item(mid: uuid.UUID, item_id: uuid.UUID, body: ItemPatch, _: User, db: Db) -> dict[str, Any]:
    await _get(db, mid)
    it = await db.get(MeetingItem, item_id)
    if it is None or it.meeting_id != mid:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "항목이 없어요.")
    data = body.model_dump(exclude_unset=True)
    if data.get("status") and it.kind not in ("action", "open_issue"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, {"code": "bad_input", "message": "액션과 미해결 쟁점만 상태를 바꿀 수 있어요."})
    for k in ("status", "text", "owner", "due"):
        if k in data and (k != "text" or data[k]):
            setattr(it, k, data[k].strip() if isinstance(data[k], str) else data[k])
    await db.commit()
    return read.item_out(it)


async def _resolve(db: AsyncSession, mid: uuid.UUID, pid: uuid.UUID, fn) -> dict[str, Any]:
    m = await _get(db, mid)
    try:
        await fn(db, m, pid)
    except pipeline.PipelineError as e:
        await db.rollback()
        raise _http(e) from e
    await db.commit()
    return await read.meeting_detail(db, m)


@router.post("/{mid}/proposals/{pid}/approve")
async def approve(mid: uuid.UUID, pid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    """맥락 도서관·이전 액션에 실제로 반영하는 유일한 경로(사용자 승인)."""
    return await _resolve(db, mid, pid, pipeline.approve)


@router.post("/{mid}/proposals/{pid}/reject")
async def reject(mid: uuid.UUID, pid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    return await _resolve(db, mid, pid, pipeline.reject)


# ---- 에이전트 읽기 전용(MCP list_meetings / get_meeting / search_meeting_items)
# 회사 세션은 회사 회의만 본다(맥락 도서관과 같은 범위 규칙, ADR 0009).

async def _agent_scope(db: AsyncSession, sid: uuid.UUID) -> str:
    s = await db.get(ChatSession, sid)
    if s is None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "세션이 없습니다.")
    return "work" if s.pool == "work" else "all"


@agent_router.get("")
async def agent_list(sid: AgentSession, db: Db, q: str | None = None, project: str | None = None, status_: Annotated[str | None, Query(alias="status")] = None,
                     limit: int = 20) -> dict[str, Any]:
    scope = await _agent_scope(db, sid)
    entity_id = None
    if project:
        e = await store.find_entity(db, project, "project", scope=scope)
        if e is None:
            return {"meetings": [], "note": f"'{project}' 프로젝트를 찾지 못했어요. library_catalog 로 이름을 확인하세요."}
        entity_id = e.id
    rows = await read.list_meetings(db, scope=scope, q=q, status=status_, entity_id=entity_id, limit=limit)
    keep = ("id", "title", "scope", "status", "project", "held_at", "summary")
    return {"meetings": [{**{k: r[k] for k in keep}, "counts": r["counts"]} for r in await read.meeting_rows(db, rows)]}


@agent_router.get("/search")
async def agent_search(sid: AgentSession, db: Db, q: str | None = None, kind: Literal["discussion", "decision", "action", "open_issue"] | None = None,
                       status_: Annotated[ItemStatus | None, Query(alias="status")] = None, project: str | None = None,
                       limit: int = 20) -> dict[str, Any]:
    scope = await _agent_scope(db, sid)
    return await read.search_items(db, scope=scope, q=q, kind=kind, status=status_, project=project, limit=limit)


@agent_router.get("/{mid}")
async def agent_get(mid: uuid.UUID, sid: AgentSession, db: Db) -> dict[str, Any]:
    scope = await _agent_scope(db, sid)
    m = await _get(db, mid)
    if scope == "work" and m.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "회의록이 없어요.")
    items = await db.scalars(select(MeetingItem).where(MeetingItem.meeting_id == m.id).order_by(MeetingItem.kind, MeetingItem.position))
    return read.agent_meeting((await read.meeting_rows(db, [m]))[0], list(items))
