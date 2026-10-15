"""회의록 API(ADR 0010). 사용자 화면용 /api/meetings 와 에이전트 읽기 전용 /api/agent/meetings.

제안의 승인·거절과 항목 수정은 사용자 API 에만 있다. 에이전트 쪽에는 쓰기 경로가 없다.
"""

from __future__ import annotations

import unicodedata
import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, StringConstraints, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.api.deps import Db
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.meetings import pipeline, providers, read, vectors
from ttakkari.meetings.projects import find_project
from ttakkari.models import (
    ChatSession,
    LocalSttJob,
    LocalSttSegment,
    Meeting,
    MeetingItem,
    MeetingStatus,
    PmsProject,
)
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
    # 만들자마자 처리까지: 녹음이면 로컬 전사 → 분석, 전사 텍스트면 바로 분석(ADR 0021).
    auto_process: bool = False
    provider_id: str | None = Field(default=None, max_length=30)


class TranscribeIn(BaseModel):
    auto_analyze: bool = True
    provider_id: str | None = Field(default=None, max_length=30)


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
    return {"default_provider": providers.DEFAULT_PROVIDER, "providers": providers.describe_providers()}


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
           detail={"scope": m.scope, "source": m.source, "status": m.status, "auto": body.auto_process})
    await db.commit()
    if m.transcript:
        vectors.schedule(m.id)
    if body.auto_process:
        # 자동 처리 실패는 회의 생성을 되돌리지 않는다. 사유는 상세 화면의 버튼·오류로 다시 보인다.
        try:
            if m.status == MeetingStatus.uploaded:
                await pipeline.request_transcription(db, m, body.provider_id, auto_analyze=True)
                await db.commit()
                if m.provider == "local_whisper":
                    from ttakkari.meetings.local_stt import jobs
                    jobs.kick()
            elif m.status == MeetingStatus.transcribed:
                await pipeline.start_analysis(db, m)
        except pipeline.PipelineError as e:
            await db.rollback()
            m = await _get(db, m.id)
            out = await read.meeting_detail(db, m)
            return {**out, "auto_error": {"code": e.code, "message": str(e)}}
    return await read.meeting_detail(db, m)


@router.get("/{mid}")
async def get(mid: uuid.UUID, _: User, db: Db) -> dict[str, Any]:
    return await read.meeting_detail(db, await _fresh(db, await _get(db, mid)))


class ReprocessIn(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    terms: list[str] = Field(default_factory=list, max_length=50)


@router.get("/{mid}/stt")
async def stt_jobs(mid: uuid.UUID, _: User, db: Db) -> dict:
    from ttakkari.meetings.local_stt.jobs import job_out
    await _get(db, mid)
    rows = await db.scalars(select(LocalSttJob).where(LocalSttJob.meeting_id == mid).order_by(LocalSttJob.created_at.desc()).limit(100))
    return {"jobs": [job_out(j) for j in rows]}


async def _stt_job(db: AsyncSession, mid: uuid.UUID, jid: uuid.UUID, *, lock: bool = False) -> LocalSttJob:
    if lock:
        job = await db.scalar(
            select(LocalSttJob).where(LocalSttJob.id == jid, LocalSttJob.meeting_id == mid)
            .with_for_update().execution_options(populate_existing=True))
    else:
        job = await db.get(LocalSttJob, jid)
    if job is None or job.meeting_id != mid:
        raise HTTPException(404, "로컬 전사 작업이 없어요.")
    return job


@router.get("/{mid}/stt/{jid}/segments")
async def stt_segments(mid: uuid.UUID, jid: uuid.UUID, _: User, db: Db,
                       offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500),
                       fields: str | None = Query(None, max_length=500)) -> dict:
    from ttakkari.meetings.local_stt.jobs import job_out
    from ttakkari.meetings.local_stt.read_model import segment_fields
    job = await _stt_job(db, mid, jid)
    try:
        selected = segment_fields(fields)
    except ValueError as exc:
        raise HTTPException(422, "조회할 구간 필드를 확인해 주세요.") from exc
    columns = [LocalSttSegment.id, LocalSttSegment.job_id] + [getattr(LocalSttSegment, name) for name in selected]
    rows = (await db.execute(select(*columns).where(LocalSttSegment.job_id == jid)
                            .order_by(LocalSttSegment.position).offset(offset).limit(limit))).mappings()
    segments = [{**s, "id": str(s.id), "job_id": str(s.job_id),
                 "audio_artifact_id": str(job.audio_artifact_id) if job.audio_artifact_id else None} for s in rows]
    info = job_out(job)
    if fields is not None:
        # Dropping raw/words alone would still return the whole transcript and
        # retry token arrays through job metadata.
        info["metadata"] = {key: value for key, value in info["metadata"].items()
                            if key in ("duration", "start", "end", "speaker_count", "diarization_status", "overlap_detection_status")}
    return {"job": info, "segments": segments, "offset": offset}


class UtteranceOut(BaseModel):
    id: str
    start: float
    end: float
    speaker_id: str | None
    speaker_label: str | None
    speaker_name: str | None
    text: str
    verification_status: str
    overlap: bool | None
    flags: list[str]
    segment_positions: list[int]


class UtterancesOut(BaseModel):
    job_id: str
    utterances: list[UtteranceOut]
    offset: int
    limit: int
    total: int
    has_more: bool
    speaker_count: int | None
    speaker_names: dict[str, str]
    diarization_status: str
    overlap_detection_status: str
    audio_artifact_id: str | None


@router.get("/{mid}/stt/{jid}/utterances", response_model=UtterancesOut)
async def stt_utterances(mid: uuid.UUID, jid: uuid.UUID, _: User, db: Db,
                         offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    from ttakkari.meetings.local_stt.read_model import job_utterances, named_utterances
    job = await _stt_job(db, mid, jid)
    if job.status != "succeeded":
        raise HTTPException(409, "전사가 끝난 뒤 발화 목록을 볼 수 있어요.")
    meta = job.result_metadata
    utterances = await job_utterances(db, job)
    names = meta.get("speaker_names", {})
    return {"job_id": str(jid), "utterances": named_utterances(utterances[offset:offset+limit], names),
            "offset": offset, "limit": limit, "total": len(utterances),
            "has_more": offset+limit < len(utterances), "speaker_count": meta.get("speaker_count"),
            "speaker_names": names,
            "diarization_status": meta.get("diarization_status", "not_run"),
            "overlap_detection_status": meta.get("overlap_detection_status", "not_run"),
            "audio_artifact_id": str(job.audio_artifact_id) if job.audio_artifact_id else None}


class SpeakerNamesIn(BaseModel):
    model_config = {"extra": "forbid"}
    names: dict[
        Annotated[str, Field(min_length=1, max_length=100)],
        Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=40)],
    ] = Field(max_length=100)

    @field_validator("names")
    @classmethod
    def no_control_characters(cls, names: dict[str, str]) -> dict[str, str]:
        if any(unicodedata.category(char) == "Cc" for name in names.values() for char in name):
            raise ValueError("Names cannot contain control characters")
        return names


class SpeakerNamesOut(BaseModel):
    job_id: str
    names: dict[str, str]


@router.put("/{mid}/stt/{jid}/speakers", response_model=SpeakerNamesOut)
async def stt_speaker_names(mid: uuid.UUID, jid: uuid.UUID, body: SpeakerNamesIn,
                            _: User, db: Db, request: Request):
    from ttakkari.meetings.local_stt.read_model import job_utterances
    job = await _stt_job(db, mid, jid, lock=True)
    if job.status != "succeeded":
        raise HTTPException(409, "전사가 끝난 뒤 화자 이름을 저장할 수 있어요.")
    known = {row["speaker_id"] for row in await job_utterances(db, job) if row.get("speaker_id")}
    if set(body.names) - known:
        raise HTTPException(422, "이 전사 작업에 없는 화자 ID가 있어요.")
    job.result_metadata = {**job.result_metadata, "speaker_names": body.names}
    record(db, action="meeting.speakers.update", target_type="local_stt_job", target_id=jid,
           ip=client_ip(request), detail={"speaker_count": len(body.names)})
    await db.commit()
    return {"job_id": str(jid), "names": body.names}


class RetranscribeIn(BaseModel):
    model_config = {"extra": "forbid"}
    # Capability probes must fail in request validation, before creating any rows.
    auto_analyze: bool = Field(default=False, strict=True)


@router.post("/{mid}/retranscribe", status_code=202)
async def retranscribe(mid: uuid.UUID, body: RetranscribeIn, _: User, db: Db) -> dict:
    """A new meeting preserves the old transcript, analysis, items and approved memories."""
    from ttakkari.meetings.local_stt import jobs
    original = await _get(db, mid)
    if original.audio_artifact_id is None:
        raise HTTPException(409, "재전사할 원본 녹음이 없어요.")
    if jobs.LocalWhisperProvider().availability().state != providers.ProviderState.ready:
        raise HTTPException(409, "로컬 음성 인식 설정이 필요해요.")
    try:
        meeting = await pipeline.create_meeting(db, title=f"{original.title[:190]} (재처리)",
            scope=original.scope, entity_id=original.pms_project_id or original.entity_id,
            held_at=original.held_at, participants=original.participants,
            audio_artifact_id=original.audio_artifact_id)
    except pipeline.PipelineError as exc:
        raise _http(exc) from exc
    meeting.source_meeting_id = original.id
    job = LocalSttJob(meeting_id=meeting.id, audio_artifact_id=meeting.audio_artifact_id,
        params={"profile": "robust", "terms": [], "auto_analyze": body.auto_analyze,
                "source_meeting_id": str(mid), "source_job_id": original.provider_job_id})
    db.add(job)
    await db.flush()
    meeting.provider, meeting.provider_job_id = "local_whisper", str(job.id)
    pipeline.transition(meeting, MeetingStatus.transcribing)
    await db.commit()
    out = await read.meeting_detail(db, meeting)
    # stt_job_id remains reserved for the adopted, successful transcript.
    out["stt_job"] = {"id": str(job.id), "status": job.status}
    jobs.kick()
    return out


class CorrectionsIn(BaseModel):
    model_config = {"extra": "forbid"}
    replacements: dict[
        Annotated[str, Field(min_length=1, max_length=80)],
        Annotated[str, Field(min_length=1, max_length=80)],
    ] = Field(min_length=1, max_length=100)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


@router.post("/{mid}/stt/{jid}/corrections")
async def stt_corrections(
    mid: uuid.UUID, jid: uuid.UUID, body: CorrectionsIn, _: User, db: Db
) -> dict:
    """Preview literal corrections; original DB rows and meeting analysis are immutable."""
    from ttakkari.meetings.local_stt.core import correct_terms

    job = await _stt_job(db, mid, jid)
    if job.status != "succeeded":
        raise HTTPException(409, "전사 결과가 나온 뒤 교정 후보를 만들 수 있어요.")
    rows = await db.scalars(
        select(LocalSttSegment)
        .where(LocalSttSegment.job_id == jid)
        .order_by(LocalSttSegment.position)
        .offset(body.offset)
        .limit(body.limit)
    )
    segments = [
        {
            c.name: getattr(s, c.name)
            for c in LocalSttSegment.__table__.columns
            if c.name not in ("id", "job_id", "raw")
        }
        for s in rows
    ]
    return {
        "candidate_only": True,
        "segments": correct_terms(segments, body.replacements),
        "offset": body.offset,
    }


class OverlapComparisonIn(BaseModel):
    model_config = {"extra": "forbid"}
    references: list[Annotated[str, Field(min_length=1, max_length=20000)]] = Field(
        min_length=2, max_length=2
    )
    human_verified: Literal[True]


@router.post("/{mid}/stt/{jid}/overlap/{candidate_index}/compare")
async def stt_compare_overlap(
    mid: uuid.UUID,
    jid: uuid.UUID,
    candidate_index: int,
    body: OverlapComparisonIn,
    _: User,
    db: Db,
) -> dict:
    """Compare a saved candidate against explicit human references; never overwrite a meeting."""
    from ttakkari.meetings.local_stt.core import compare_overlap_candidate

    job = await _stt_job(db, mid, jid)
    candidates = job.result_metadata.get("overlap_candidates", [])
    if not 0 <= candidate_index < len(candidates):
        raise HTTPException(404, "중첩 재처리 후보가 없어요.")
    candidate = candidates[candidate_index]
    if candidate.get("status") != "succeeded":
        raise HTTPException(409, "분리와 전사가 나온 후보만 비교할 수 있어요.")
    decision = compare_overlap_candidate(
        candidate["original_text"],
        [t["text"] for t in candidate["tracks"]],
        references=body.references,
    )
    return {
        "candidate_only": True,
        "decision": decision,
        "start": candidate["start"],
        "end": candidate["end"],
    }

@router.post("/{mid}/stt/{jid}/reprocess", status_code=202)
async def stt_reprocess(mid: uuid.UUID, jid: uuid.UUID, body: ReprocessIn, _: User, db: Db) -> dict:
    from ttakkari.meetings.local_stt import jobs
    original = await _stt_job(db, mid, jid)
    if original.status != "succeeded":
        raise HTTPException(409, "전사 결과가 나온 뒤 구간을 다시 처리할 수 있어요.")
    if jobs.LocalWhisperProvider().availability().state != providers.ProviderState.ready:
        raise HTTPException(409, "로컬 음성 인식 설정이 필요해요.")
    if (not body.start < body.end <= original.result_metadata.get("duration", 0)
            or body.end-body.start > get_settings().local_stt_max_reprocess_seconds
            or any(len(term) > 100 for term in body.terms)):
        raise HTTPException(422, "재처리 구간·용어 길이를 확인해 주세요.")
    # Preserve both attempts. This endpoint cannot change the meeting transcript or analysis.
    candidate = LocalSttJob(meeting_id=mid, audio_artifact_id=original.audio_artifact_id, parent_job_id=jid,
        params={"profile": "improved", "start": body.start, "end": body.end, "terms": body.terms, "auto_analyze": False})
    db.add(candidate)
    await db.commit()
    jobs.kick()
    return jobs.job_out(candidate)


@router.patch("/{mid}")
async def patch(mid: uuid.UUID, body: MeetingPatch, _: User, db: Db) -> dict[str, Any]:
    m = await _get(db, mid)
    data = body.model_dump(exclude_unset=True)
    try:
        if "project_id" in data:
            project = await pipeline.check_project(db, data["project_id"], m.scope)
            m.pms_project_id = data["project_id"] if isinstance(project, PmsProject) else None
            m.entity_id = data["project_id"] if project and not isinstance(project, PmsProject) else None
    except pipeline.PipelineError as e:
        raise _http(e) from e
    if data.get("title"):
        m.title = data["title"].strip()[:200]
    if "held_at" in data:
        m.held_at = data["held_at"]
    if data.get("participants") is not None:
        m.participants = [p.strip() for p in data["participants"] if p.strip()][:50]
    await db.commit()
    vectors.schedule(m.id)
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
    vectors.schedule(m.id)
    return await read.meeting_detail(db, m)


@router.post("/{mid}/transcribe", status_code=status.HTTP_202_ACCEPTED)
async def transcribe(mid: uuid.UUID, _: User, db: Db, body: TranscribeIn | None = None) -> dict[str, Any]:
    """녹음을 선택 Provider에 맡긴다. 미설정이면 409 provider_unavailable과 사유를 돌려준다."""
    m = await _get(db, mid)
    try:
        await pipeline.request_transcription(db, m, body.provider_id if body else None, auto_analyze=body.auto_analyze if body else True)
    except pipeline.PipelineError as e:
        raise _http(e) from e
    await db.commit()
    if m.provider == "local_whisper":
        from ttakkari.meetings.local_stt import jobs
        jobs.kick()
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
    m = await _get(db, mid)
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
    vectors.schedule(mid, delay=3.0)
    return await read.item_with_evidence(db, m, it)


async def _resolve(db: AsyncSession, mid: uuid.UUID, pid: uuid.UUID, fn) -> dict[str, Any]:
    m = await _get(db, mid)
    try:
        p = await fn(db, m, pid)
    except pipeline.PipelineError as e:
        await db.rollback()
        raise _http(e) from e
    await db.commit()
    if p.kind == "update_action_item" and p.status == "approved" and p.payload.get("target_meeting_id"):
        vectors.schedule(uuid.UUID(p.payload["target_meeting_id"]))
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
        e = await find_project(db, project, scope=scope)
        if e is None:
            return {"meetings": [], "note": f"'{project}' 프로젝트를 찾지 못했어요. PMS에서 이름을 확인하세요."}
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


@agent_router.get("/semantic")
async def agent_semantic(sid: AgentSession, db: Db, q: Annotated[str, Query(min_length=1, max_length=500)],
                         project: str | None = None, kind: str | None = None, limit: int = Query(8, ge=1, le=30)) -> dict[str, Any]:
    """회의록 조각 의미 검색(어휘 + 로컬 벡터, ADR 0021). 회사 세션은 회사 회의만."""
    scope = await _agent_scope(db, sid)
    entity_id = None
    if project:
        e = await find_project(db, project, scope=scope)
        if e is None:
            return {"results": [], "note": f"'{project}' 프로젝트를 찾지 못했어요. PMS에서 이름을 확인하세요."}
        entity_id = e.id
    kinds = tuple(k for k in (kind or "").split(",") if k) or None
    hits = await vectors.search(db, q, limit, scope=scope, entity_id=entity_id, kinds=kinds)
    return {"results": [vectors.hit_out(h) for h in hits]}


@agent_router.get("/{mid}")
async def agent_get(mid: uuid.UUID, sid: AgentSession, db: Db) -> dict[str, Any]:
    scope = await _agent_scope(db, sid)
    m = await _get(db, mid)
    if scope == "work" and m.scope != "work":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "회의록이 없어요.")
    items = await db.scalars(select(MeetingItem).where(MeetingItem.meeting_id == m.id).order_by(MeetingItem.kind, MeetingItem.position))
    return read.agent_meeting((await read.meeting_rows(db, [m]))[0], list(items))
