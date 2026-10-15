"""Durable serial queue. Model-free parent; one disposable inference child at a time."""

from __future__ import annotations

import asyncio
import logging
import math
import sys
import uuid
from pathlib import Path

from sqlalchemy import String, select, update

from ttakkari import generation
from ttakkari.artifacts.service import content_path
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.meetings.local_stt.quality import quality_gate, repetition_metrics
from ttakkari.meetings.local_stt.runner import run_worker
from ttakkari.models import (
    Artifact,
    LocalSttJob,
    LocalSttSegment,
    Meeting,
    MeetingStatus,
    PmsProject,
    utcnow,
)

log = logging.getLogger(__name__)
_task: asyncio.Task | None = None
_wake = asyncio.Event()


class LocalWhisperProvider:
    id = "local_whisper"
    label = "로컬 Whisper large-v3"

    def availability(self):
        from ttakkari.meetings.providers import ProviderAvailability, ProviderState

        s = get_settings()
        model = s.local_stt_model_path
        if not s.local_stt_enabled:
            return ProviderAvailability(
                ProviderState.unconfigured, "로컬 음성 인식이 꺼져 있어요."
            )
        if not model or not all(
            (model / f).is_file() for f in ("model.bin", "config.json")
        ):
            return ProviderAvailability(
                ProviderState.unconfigured, "로컬 large-v3 모델 설치가 필요해요."
            )
        if not Path(s.local_stt_python or sys.executable).is_file():
            return ProviderAvailability(
                ProviderState.unconfigured, "로컬 음성 인식 실행 환경이 필요해요."
            )
        return ProviderAvailability(
            ProviderState.ready, "이 Mac에서 Whisper large-v3로 전사해요."
        )

    async def submit(self, audio) -> str:
        from ttakkari.meetings.providers import ProviderNotAvailable

        if audio.meeting_id is None:
            raise ProviderNotAvailable("회의록을 먼저 만들어 주세요.")
        async with sessionmaker()() as db:
            meeting = await db.get(Meeting, audio.meeting_id)
            if meeting is None:
                raise ProviderNotAvailable("회의록을 찾지 못했어요.")
            project = (
                await db.get(PmsProject, meeting.pms_project_id)
                if meeting.pms_project_id
                else None
            )
            terms = ([project.name] if project else []) + list(
                meeting.participants or []
            )
            job = LocalSttJob(
                meeting_id=meeting.id,
                audio_artifact_id=meeting.audio_artifact_id,
                params={
                    "profile": get_settings().local_stt_profile,
                    "terms": terms,
                    "auto_analyze": audio.auto_analyze,
                },
            )
            db.add(job)
            await db.commit()
            jid = str(job.id)
        # The caller wakes the queue after committing the meeting/job link.
        return jid

    async def poll(self, job_id: str):
        from ttakkari.meetings.providers import TranscriptionPoll

        try:
            jid = uuid.UUID(job_id)
        except ValueError:
            return TranscriptionPoll(
                "failed", error="로컬 전사 작업 ID가 올바르지 않아요."
            )
        async with sessionmaker()() as db:
            job = await db.get(LocalSttJob, jid)
            if job is None:
                return TranscriptionPoll(
                    "failed", error="로컬 전사 작업을 찾지 못했어요."
                )
            if job.status == "failed":
                return TranscriptionPoll("failed", error=job.error)
            if job.status == "succeeded":
                if not (job.transcript or "").strip():
                    return TranscriptionPoll(
                        "failed",
                        error="음성을 인식하지 못했어요. 원본 녹음과 구간을 확인해 주세요.",
                    )
                return TranscriptionPoll("done", text=job.transcript)
        return TranscriptionPoll("pending")


def job_out(job: LocalSttJob) -> dict:
    return {
        "id": str(job.id),
        "meeting_id": str(job.meeting_id),
        "status": job.status,
        "parent_job_id": str(job.parent_job_id) if job.parent_job_id else None,
        "candidate_only": job.parent_job_id is not None,
        "params": job.params,
        "metadata": job.result_metadata,
        "error": job.error,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


async def execute_job(job_id: uuid.UUID) -> None:
    # Claim and commit before inference; never hold a DB connection/row lock while loading a model.
    async with sessionmaker()() as db:
        job = await db.scalar(
            select(LocalSttJob).where(LocalSttJob.id == job_id).with_for_update()
        )
        if job is None or job.status != "queued":
            return
        job.status, job.started_at = "running", utcnow()
        params, artifact_id = dict(job.params), job.audio_artifact_id
        await db.commit()
    try:
        async with sessionmaker()() as db:
            artifact = await db.get(Artifact, artifact_id) if artifact_id else None
            if artifact is None or artifact.deleted_at is not None:
                raise ValueError("source_audio_missing")
            path = content_path(artifact)
        result = await run_worker(path, params, get_settings())
        segments, raw = result["segments"], result["raw_segments"]
        if len(segments) != len(raw):
            raise ValueError("worker_segment_mismatch")
        for s, r in zip(segments, raw, strict=True):
            if (
                not all(math.isfinite(s[k]) for k in ("start", "end"))
                or not 0 <= s["start"] <= s["end"] <= result["duration"] + 0.02
                or s["original_text"] != r["text"]
            ):
                raise ValueError("invalid_worker_segment")
        # Recompute from preserved, unfolded output. Display folding is never
        # evidence of recovery; missing retry evidence falls back to originals.
        after_retry = repetition_metrics(result.get("post_retry_segments", segments))
        gate = quality_gate(after_retry)
        result["quality"] = {
            **result.get("quality", {}),
            "after_retry": after_retry,
            **gate,
        }
        async with sessionmaker()() as db:
            job = await db.get(LocalSttJob, job_id)
            if job is None:
                return  # meeting was deleted while inference ran
            for i, (s, r) in enumerate(zip(segments, raw, strict=True)):
                db.add(LocalSttSegment(job_id=job.id, position=i, raw=r, **s))
            job.result_metadata = {
                k: v
                for k, v in result.items()
                if k not in ("text", "segments", "raw_segments")
            }
            job.transcript, job.status, job.finished_at = (
                result["text"],
                "succeeded" if gate["auto_analysis_allowed"] else "failed",
                utcnow(),
            )
            if not gate["auto_analysis_allowed"]:
                job.error = "재시도 뒤에도 심각한 반복이 남아 검증이 필요해요. 원본 구간을 확인해 주세요. 자동분석은 중단했어요."
            await db.commit()
    except asyncio.CancelledError:
        # runner has killed/reaped the child; next start may safely retry this attempt.
        async with sessionmaker()() as db:
            await db.execute(
                update(LocalSttJob)
                .where(LocalSttJob.id == job_id)
                .values(status="queued", started_at=None)
            )
            await db.commit()
        raise
    except Exception as exc:  # noqa: BLE001 -- queue boundary: persist failure, never discard a job
        log.warning("local STT %s failed: %s", job_id, type(exc).__name__)
        async with sessionmaker()() as db:
            job = await db.get(LocalSttJob, job_id)
            if job is None:
                return
            job.status, job.finished_at = "failed", utcnow()
            job.error = (
                "로컬 전사에 실패했어요. 실행 환경·모델·녹음 형식을 확인해 주세요."
            )
            job.result_metadata = {"failure_type": type(exc).__name__}
            await db.commit()
    await _reconcile(job_id)


async def _reconcile(job_id: uuid.UUID) -> None:
    from ttakkari.meetings import pipeline

    async with sessionmaker()() as db:
        job = await db.get(LocalSttJob, job_id)
        if job is None or job.parent_job_id is not None:
            return
        m = await db.get(Meeting, job.meeting_id)
        if (
            m is None
            or m.provider != "local_whisper"
            or m.provider_job_id != str(job_id)
        ):
            return
        await pipeline.reconcile(db, m)
        if (
            job.status == "succeeded"
            and job.params.get("auto_analyze")
            and m.status == MeetingStatus.transcribed
        ):
            # Lock again so a concurrent user analysis cannot create two analysis Runs.
            m = await db.scalar(
                select(Meeting)
                .where(Meeting.id == m.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if m and m.status == MeetingStatus.transcribed:
                try:
                    await pipeline.start_analysis(db, m)
                except pipeline.PipelineError as exc:
                    await db.rollback()
                    log.warning("local STT auto analysis unavailable: %s", exc.code)


async def _generation_next():
    async with sessionmaker()() as db:
        # An orphan inference child may still be alive after its parent dies.
        if await db.scalar(select(LocalSttJob.id).where(LocalSttJob.status == "running").limit(1)):
            return None
        return await db.scalar(select(LocalSttJob.id).join(Meeting, Meeting.id == LocalSttJob.meeting_id)
            .where(LocalSttJob.status == "queued", (LocalSttJob.parent_job_id.is_not(None)) | (
                (Meeting.provider == "local_whisper") & (Meeting.provider_job_id == LocalSttJob.id.cast(String))))
            .order_by(LocalSttJob.created_at).limit(1))


async def _loop() -> None:
    if generation.current:
        from ttakkari.generation_service import consume
        await consume("stt", _generation_next, execute_job)
        return
    while True:
        _wake.clear()
        try:
            async with sessionmaker()() as db:
                # submit commits the job before the caller commits provider_job_id. Wait for that link.
                job_id = await db.scalar(
                    select(LocalSttJob.id)
                    .join(Meeting, Meeting.id == LocalSttJob.meeting_id)
                    .where(
                        LocalSttJob.status == "queued",
                        (LocalSttJob.parent_job_id.is_not(None))
                        | (
                            (Meeting.provider == "local_whisper")
                            & (Meeting.provider_job_id == LocalSttJob.id.cast(String))
                        ),
                    )
                    .order_by(LocalSttJob.created_at)
                    .limit(1)
                )
            if job_id:
                await execute_job(job_id)
            else:
                await _wake.wait()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("local STT queue iteration failed")
            await asyncio.sleep(2)


def kick() -> None:
    global _task
    _wake.set()
    if get_settings().local_stt_enabled and (_task is None or _task.done()):
        _task = asyncio.create_task(_loop())


async def start() -> None:
    if not get_settings().local_stt_enabled:
        return
    if generation.current:
        kick()
        return
    async with sessionmaker()() as db:
        await db.execute(
            update(LocalSttJob)
            .where(LocalSttJob.status == "running")
            .values(status="queued", started_at=None)
        )
        finished = list(
            await db.scalars(
                select(LocalSttJob.id)
                .join(Meeting, Meeting.id == LocalSttJob.meeting_id)
                .where(
                    LocalSttJob.status.in_(("succeeded", "failed")),
                    LocalSttJob.parent_job_id.is_(None),
                    Meeting.provider == "local_whisper",
                    Meeting.provider_job_id == LocalSttJob.id.cast(String),
                    Meeting.status.in_(
                        (MeetingStatus.transcribing, MeetingStatus.transcribed)
                    ),
                )
            )
        )
        await db.commit()
    for jid in finished:
        await _reconcile(jid)
    kick()


async def stop() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None
