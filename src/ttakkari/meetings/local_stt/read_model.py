"""Read projections shared by STT endpoints and meeting evidence links."""

from __future__ import annotations

import uuid

from sqlalchemy import case, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.models import LocalSttJob, LocalSttSegment, Meeting

from .utterances import build_utterances


async def adopted_job(db: AsyncSession, meeting: Meeting) -> LocalSttJob | None:
    if meeting.transcript_source != "provider:local_whisper" or not meeting.transcript:
        return None
    try:
        pointer = uuid.UUID(meeting.provider_job_id or "")
    except ValueError:
        pointer = None
    # The provider pointer can already refer to a queued retry. Keep the prior
    # matching transcript in that case. A successful child is still a candidate.
    return await db.scalar(
        select(LocalSttJob)
        .where(
            LocalSttJob.meeting_id == meeting.id,
            LocalSttJob.status == "succeeded",
            LocalSttJob.parent_job_id.is_(None),
            LocalSttJob.transcript == meeting.transcript,
        )
        .order_by(
            case((LocalSttJob.id == pointer, 0), else_=1),
            LocalSttJob.created_at.desc(),
            LocalSttJob.id.desc(),
        )
        .limit(1)
    )


async def job_utterances(db: AsyncSession, job: LocalSttJob) -> list[dict]:
    meta = job.result_metadata
    if meta.get("utterances") is not None:
        return meta["utterances"]
    columns = [c for c in LocalSttSegment.__table__.columns if c.name != "raw"]
    rows = (
        await db.execute(
            select(*columns)
            .where(LocalSttSegment.job_id == job.id)
            .order_by(LocalSttSegment.position)
        )
    ).mappings()
    return build_utterances(
        [dict(row) for row in rows],
        meta.get("diarization", {}).get("turns", []),
        overlap_known=meta.get("overlap_detection_status") == "succeeded",
    )


def named_utterances(utterances: list[dict], names: dict[str, str]) -> list[dict]:
    # Presentation only. Persisted acoustic labels/raw utterances stay untouched.
    return [
        {**row, "speaker_name": names.get(row.get("speaker_id"))} for row in utterances
    ]


def segment_fields(value: str | None) -> list[str]:
    public = [
        c.name
        for c in LocalSttSegment.__table__.columns
        if c.name not in ("id", "job_id")
    ]
    if value is None:
        return public
    if value == "light":
        return [field for field in public if field not in ("raw", "words")]
    fields = list(dict.fromkeys(part.strip() for part in value.split(",")))
    if not fields or any(field not in public for field in fields):
        raise ValueError("Invalid segment field selection")
    return fields
