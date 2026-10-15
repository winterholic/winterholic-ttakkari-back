"""클로바노트 처리 작업 큐(ADR 0021). 목록 동기화(sync)·노트 가져오기(import)·녹음 업로드(upload)를 한 줄로 차례차례 처리한다.

브라우저(프로필) 하나를 같이 쓰므로 동시에 하나만 돈다. 작업은 clovanote_jobs 행에 단계·진행률·기록을 남기고 화면이 그것을 그대로 보여 준다.
서버가 재시작하면 실행 중이던 작업은 대기열로 돌아가 이어서 한다(업로드는 이미 올린 노트가 있으면 전사 대기부터).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.artifacts.service import content_path
from ttakkari.audit import record
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.meetings import clovanote as cn
from ttakkari.meetings import vectors
from ttakkari.models import (
    Artifact,
    ClovaJob,
    ClovaNote,
    Meeting,
    MeetingStatus,
    utcnow,
)

log = logging.getLogger(__name__)

# 화면의 단계 표시. (단계, 이름) 순서가 곧 진행 순서다.
STAGES: dict[str, list[tuple[str, str]]] = {
    "sync": [("connect", "연결"), ("list", "목록"), ("fetch", "전사 받기"), ("done", "완료")],
    "import": [("connect", "연결"), ("fetch", "전사 받기"), ("meeting", "회의록"), ("analyze", "분석 요청"), ("done", "완료")],
    "upload": [("connect", "연결"), ("create", "노트 만들기"), ("upload", "업로드"), ("recognize", "전사"), ("fetch", "전사 받기"),
               ("analyze", "분석 요청"), ("done", "완료")],
}
ACTIVE = ("queued", "running", "waiting")
POLL_SECONDS = 3.0
MAX_STEPS = 60

_wake = asyncio.Event()
_worker: asyncio.Task | None = None
_sync_loop: asyncio.Task | None = None


class JobError(Exception):
    pass


# ---- 등록

async def enqueue(db: AsyncSession, kind: str, *, meeting_id: uuid.UUID | None = None, note_id: str | None = None,
                  params: dict[str, Any] | None = None) -> ClovaJob:
    """같은 대상의 작업이 이미 대기·진행 중이면 그것을 돌려준다(버튼을 여러 번 눌러도 한 번만)."""
    q = select(ClovaJob).where(ClovaJob.kind == kind, ClovaJob.status.in_(ACTIVE))
    if kind == "import":
        q = q.where(ClovaJob.note_id == note_id)
    elif kind == "upload":
        q = q.where(ClovaJob.meeting_id == meeting_id)
    existing = await db.scalar(q.limit(1))
    if existing is not None:
        return existing
    job = ClovaJob(kind=kind, meeting_id=meeting_id, note_id=note_id, params=params or {}, status="queued", stage="queued",
                   message="차례를 기다리는 중", steps=[], result={})
    db.add(job)
    await db.flush()
    return job


def kick() -> None:
    """커밋한 뒤에 부른다. 워커가 없으면 띄우고, 있으면 깨운다."""
    global _worker
    _wake.set()
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_run())


async def start() -> None:
    """서버 시작: 끊긴 작업을 대기열로 되돌리고 워커·자동 동기화를 띄운다."""
    global _sync_loop
    if not get_settings().clovanote_enabled:
        return
    async with sessionmaker()() as db:
        await db.execute(update(ClovaJob).where(ClovaJob.status.in_(("running", "waiting")))
                         .values(status="queued", message="서버 재시작 후 이어서 처리해요"))
        await db.commit()
    kick()
    secs = get_settings().clovanote_poll_seconds
    if secs > 0 and get_settings().clovanote_enabled:
        _sync_loop = asyncio.create_task(_auto_sync(secs))


async def stop() -> None:
    for t in (_worker, _sync_loop):
        if t is not None:
            t.cancel()
    with contextlib.suppress(Exception):
        await cn.client.close()


async def _auto_sync(secs: int) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            st = await cn.client.check()
            if st.state == "ready":
                async with sessionmaker()() as db:
                    await enqueue(db, "sync", params={"auto": True})
                    await db.commit()
                kick()
        except Exception:
            log.exception("clovanote auto sync failed")
        await asyncio.sleep(secs)


# ---- 워커

async def _run() -> None:
    while True:
        _wake.clear()
        async with sessionmaker()() as db:
            job = await db.scalar(select(ClovaJob).where(ClovaJob.status == "queued").order_by(ClovaJob.created_at)
                                  .with_for_update(skip_locked=True).limit(1))
            if job is None:
                await db.commit()
                job_id = None
            else:
                job.status, job.started_at = "running", job.started_at or utcnow()
                job_id, kind = job.id, job.kind
                await db.commit()
        if job_id is None:
            try:
                await asyncio.wait_for(_wake.wait(), 600)
            except TimeoutError:
                return
            continue
        try:
            await {"sync": _do_sync, "import": _do_import, "upload": _do_upload}[kind](job_id)
            await _finish(job_id, "succeeded", "끝났어요")
        except cn.Busy as e:
            await _step(job_id, "connect", str(e), status="queued")
            await asyncio.sleep(10)  # 로그인 창이 닫히길 기다렸다 다시
        except (cn.ClovaError, JobError) as e:
            await _finish(job_id, "failed", str(e))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("clovanote job %s crashed", job_id)
            await _finish(job_id, "failed", f"처리 중 오류: {type(e).__name__}: {str(e)[:300]}")


async def _step(job_id: uuid.UUID, stage: str, message: str, *, progress: int | None = None, status: str | None = None,
                **fields: Any) -> None:
    async with sessionmaker()() as db:
        job = await db.get(ClovaJob, job_id)
        if job is None:
            return
        changed = job.stage != stage or job.message != message
        job.stage, job.message = stage, message
        if progress is not None:
            job.progress = progress
        if status:
            job.status = status
        for k, v in fields.items():
            setattr(job, k, v)
        if changed:
            job.steps = [*job.steps, {"at": utcnow().isoformat(), "stage": stage, "message": message}][-MAX_STEPS:]
        job.updated_at = utcnow()
        await db.commit()


async def _finish(job_id: uuid.UUID, status: str, message: str) -> None:
    async with sessionmaker()() as db:
        job = await db.get(ClovaJob, job_id)
        if job is None:
            return
        job.status, job.finished_at, job.updated_at = status, utcnow(), utcnow()
        if status == "failed":
            job.error = message[:2000]
            job.message = message[:500]
        else:
            if job.stage != "done":
                job.message = message
            job.stage, job.progress = "done", 100
        job.steps = [*job.steps, {"at": utcnow().isoformat(), "stage": job.stage, "message": message[:500]}][-MAX_STEPS:]
        record(db, actor="system", action=f"clovanote.{job.kind}", target_type="clovanote_job", target_id=job.id,
               outcome="ok" if status == "succeeded" else "error", detail={"note": job.note_id, "meeting": str(job.meeting_id or "")})
        meeting_id = job.meeting_id if job.kind == "upload" else None
        await db.commit()
    if meeting_id is not None:
        await _after_upload(meeting_id, job_id)


# ---- 노트 저장

async def upsert_note(db: AsyncSession, info: dict[str, Any], detail: dict[str, Any] | None = None, ws: str | None = None) -> ClovaNote:
    nid = info["noteId"]
    row = await db.scalar(select(ClovaNote).where(ClovaNote.note_id == nid))
    if row is None:
        row = ClovaNote(note_id=nid, speakers=[], segments=[], keywords=[], meta={})
        db.add(row)
    row.workspace_id = ws or row.workspace_id
    row.title = (info.get("noteName") or "")[:500]
    row.remote_status = (info.get("noteStatus") or "")[:40]
    row.state = cn.note_state(info)
    row.duration_ms = int(info.get("audioDuration") or 0) or None
    row.recorded_at = cn.parse_time(info.get("audioStartDate")) or cn.parse_time(info.get("createdDate"))
    row.remote_updated_at = cn.parse_time(info.get("updatedDate"))
    row.speakers = sorted(set(cn.speakers_of(info).values()))
    row.remote_summary = cn.summary_of(info) or row.remote_summary
    row.meta = {**(row.meta or {}), "missing": False, "folder": info.get("folderName"), "upload_type": info.get("uploadType"),
                "progress": (info.get("recognitionStatus") or {}).get("recognitionProgress"),
                "language": info.get("recognitionLanguage"), "sample": info.get("conversationType") == "SAMPLE"}
    if detail is not None and row.state == "ready":
        segs = cn.segments_of(detail)
        row.segments = segs
        row.transcript = cn.transcript_of(segs) or None
        row.speakers = sorted({s["speaker"] for s in segs} | set(row.speakers))
        row.transcript_fetched_at = utcnow()
    row.synced_at = utcnow()
    await db.flush()
    return row


def _stale(row: ClovaNote | None, info: dict[str, Any]) -> bool:
    if row is None or row.transcript_fetched_at is None:
        return True
    upd = cn.parse_time(info.get("updatedDate"))
    return upd is not None and upd > row.transcript_fetched_at


# ---- sync

async def _do_sync(job_id: uuid.UUID) -> None:
    await _step(job_id, "connect", "클로바노트에 연결하는 중")
    async with cn.client.lock:
        await _step(job_id, "list", "노트 목록을 받는 중")
        notes = await cn.client.list_notes()
        ws = cn.client.workspace_id
    async with sessionmaker()() as db:
        existing = {r.note_id: r for r in await db.scalars(select(ClovaNote))}
        for info in notes:
            await upsert_note(db, info, ws=ws)
        seen = {n["noteId"] for n in notes}
        for nid, r in existing.items():
            if nid not in seen:
                r.meta = {**(r.meta or {}), "missing": True}
        await db.commit()
    todo = [n for n in notes if cn.note_state(n) == "ready" and _stale(existing.get(n["noteId"]), n)]
    for i, info in enumerate(todo, 1):
        await _step(job_id, "fetch", f"전사 받는 중 ({i}/{len(todo)}) {info.get('noteName', '')[:40]}", progress=int(i * 100 / max(1, len(todo))))
        async with cn.client.lock:
            detail = await cn.client.get_note(info["noteId"])
        async with sessionmaker()() as db:
            await upsert_note(db, detail.get("noteInfo") or info, detail, ws=ws)
            await db.commit()
    await _step(job_id, "done", f"노트 {len(notes)}개 확인, 전사 {len(todo)}개 새로 받음", progress=100,
                result={"notes": len(notes), "fetched": len(todo)})


# ---- import

async def _do_import(job_id: uuid.UUID) -> None:
    from ttakkari.meetings import pipeline

    async with sessionmaker()() as db:
        job = await db.get(ClovaJob, job_id)
        note_id, params = job.note_id, dict(job.params or {})
        linked = await db.scalar(select(ClovaNote.meeting_id).where(ClovaNote.note_id == note_id))
        if linked and await db.get(Meeting, linked) is not None:
            raise JobError("이미 회의록으로 가져온 노트예요.")
    await _step(job_id, "connect", "클로바노트에 연결하는 중")
    async with cn.client.lock:
        await _step(job_id, "fetch", "전사를 받는 중")
        detail = await cn.client.get_note(note_id)
        ws = cn.client.workspace_id
    info = detail.get("noteInfo") or {}
    async with sessionmaker()() as db:
        note = await upsert_note(db, info, detail, ws=ws)
        await db.commit()
        if note.state != "ready":
            raise JobError("클로바노트에서 아직 전사가 끝나지 않았어요. 끝난 뒤에 다시 가져와 주세요.")
        if not note.transcript:
            raise JobError("이 노트에는 전사된 문장이 없어요.")
        await _step(job_id, "meeting", "회의록을 만드는 중")
        try:
            m = await pipeline.create_meeting(
                db, title=note.title or "클로바노트 회의", scope=params.get("scope") or "personal",
                entity_id=uuid.UUID(params["project_id"]) if params.get("project_id") else None,
                held_at=note.recorded_at, participants=[s for s in note.speakers if s and s != "참석자"],
                transcript=note.transcript, transcript_source="provider:clovanote")
        except pipeline.PipelineError as e:
            raise JobError(str(e)) from e
        m.provider = "clovanote"  # 노트 연결은 clovanote_notes.meeting_id 가 정본이다
        note.meeting_id = m.id
        await db.commit()
        meeting_id = m.id
    await _step(job_id, "meeting", "회의록을 만들었어요", meeting_id=meeting_id)
    vectors.schedule(meeting_id)
    if params.get("analyze", True):
        await _step(job_id, "analyze", "분석을 요청하는 중")
        await _analyze(meeting_id)
    await _step(job_id, "done", "회의록으로 가져왔어요", progress=100, result={"meeting_id": str(meeting_id)})


async def _analyze(meeting_id: uuid.UUID) -> None:
    from ttakkari.meetings import pipeline

    async with sessionmaker()() as db:
        m = await db.get(Meeting, meeting_id)
        if m is not None and m.status in (MeetingStatus.transcribed, MeetingStatus.failed) and m.transcript:
            try:
                await pipeline.start_analysis(db, m)
            except pipeline.PipelineError as e:
                log.warning("meeting %s auto analysis not started: %s", meeting_id, e)


# ---- upload

async def _do_upload(job_id: uuid.UUID) -> None:
    async with sessionmaker()() as db:
        job = await db.get(ClovaJob, job_id)
        m = await db.get(Meeting, job.meeting_id) if job.meeting_id else None
        if m is None:
            raise JobError("회의록이 사라졌어요.")
        art = await db.get(Artifact, m.audio_artifact_id) if m.audio_artifact_id else None
        if art is None or art.deleted_at is not None:
            raise JobError("녹음 파일이 사라졌어요.")
        try:
            path = content_path(art)
        except Exception as e:
            raise JobError(str(e)) from e
        title, filename, mime, meeting_id = m.title, art.filename, art.mime_type, m.id
        note_id, uploaded = job.note_id, bool((job.result or {}).get("uploaded"))
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in cn.AUDIO_FORMATS:
        raise JobError(f"클로바노트는 {', '.join(cn.AUDIO_FORMATS)} 파일만 받아요(지금 파일: .{ext or '?'}).")
    if path.stat().st_size > cn.MAX_AUDIO_BYTES:
        raise JobError("클로바노트는 300MB 까지 올릴 수 있어요.")
    if not (note_id and uploaded):
        await _step(job_id, "connect", "클로바노트에 연결하는 중")
        async with cn.client.lock:
            await _step(job_id, "create", "클로바노트에 노트를 만드는 중")
            note_id = await cn.client.create_note(title)
            await _step(job_id, "upload", f"녹음 올리는 중 ({path.stat().st_size / 1_048_576:.1f}MB)", note_id=note_id)
            audio = await cn.client.upload_audio(note_id, path, filename, mime)
            await cn.client.start_recognition(note_id)
        dur = int(audio.get("audioDuration") or 0) // 1000 if isinstance(audio, dict) else 0
        await _step(job_id, "recognize", f"올렸어요({dur // 60}분 {dur % 60}초). 클로바노트가 전사하는 중", progress=0,
                    result={"uploaded": True, "note_id": note_id, "duration_s": dur})
        async with sessionmaker()() as db:
            await upsert_note(db, {"noteId": note_id, "noteName": title, "noteStatus": "RECOGNITION"}, ws=cn.client.workspace_id)
            await db.execute(update(ClovaNote).where(ClovaNote.note_id == note_id).values(meeting_id=meeting_id))
            await db.commit()
    deadline = time.monotonic() + get_settings().clovanote_recognition_timeout_seconds
    last = -1
    while True:
        async with cn.client.lock:
            detail = await cn.client.get_note(note_id)
        info = detail.get("noteInfo") or {}
        state = cn.note_state(info)
        prog = int((info.get("recognitionStatus") or {}).get("recognitionProgress") or 0)
        if state == "ready":
            break
        if state == "failed":
            err = (info.get("recognitionStatus") or {}).get("errorMessage") or info.get("noteStatus")
            raise JobError(f"클로바노트 전사에 실패했어요: {err}")
        if prog != last:
            await _step(job_id, "recognize", f"클로바노트가 전사하는 중 {prog}%", progress=prog)
            last = prog
        if time.monotonic() > deadline:
            raise JobError("클로바노트 전사가 너무 오래 걸려요. 클로바노트에서 확인한 뒤 노트 가져오기로 받아 주세요.")
        await asyncio.sleep(POLL_SECONDS)
    await _step(job_id, "fetch", "전사를 받는 중", progress=100)
    async with sessionmaker()() as db:
        note = await upsert_note(db, info, detail, ws=cn.client.workspace_id)
        note.meeting_id = meeting_id
        await db.commit()
        if not note.transcript:
            raise JobError("전사된 문장이 없어요. 녹음에 말소리가 있는지 확인해 주세요.")
    await _step(job_id, "done", "전사를 받았어요" + (". 이어서 회의록을 분석해요" if await _auto(job_id) else ""), progress=100)


async def _auto(job_id: uuid.UUID) -> bool:
    async with sessionmaker()() as db:
        job = await db.get(ClovaJob, job_id)
        return bool(job and (job.params or {}).get("auto_analyze", True))


async def _after_upload(meeting_id: uuid.UUID, job_id: uuid.UUID) -> None:
    """업로드 작업이 끝나면(성공·실패) 회의 상태를 바로 맞추고, 성공했으면 분석을 이어서 건다."""
    from ttakkari.meetings import pipeline

    for _ in range(10):  # 전사 요청 트랜잭션이 아직 커밋 전일 수 있다
        async with sessionmaker()() as db:
            m = await db.get(Meeting, meeting_id)
            if m is None:
                return
            if m.status == MeetingStatus.transcribing:
                await pipeline.reconcile(db, m)
                break
        await asyncio.sleep(1)
    async with sessionmaker()() as db:
        m = await db.get(Meeting, meeting_id)
        if m is None or m.status != MeetingStatus.transcribed:
            return
    vectors.schedule(meeting_id)
    if await _auto(job_id):
        await _analyze(meeting_id)


# ---- 읽기

def job_out(j: ClovaJob) -> dict[str, Any]:
    stages = STAGES.get(j.kind, [])
    return {"id": str(j.id), "kind": j.kind, "status": j.status, "stage": j.stage, "progress": j.progress, "message": j.message,
            "error": j.error, "note_id": j.note_id, "meeting_id": str(j.meeting_id) if j.meeting_id else None,
            "params": {k: v for k, v in (j.params or {}).items() if k in ("scope", "project_id", "analyze", "auto", "auto_analyze")},
            "result": j.result or {}, "steps": j.steps or [], "stages": [{"id": s, "label": label} for s, label in stages],
            "created_at": _iso(j.created_at), "started_at": _iso(j.started_at), "finished_at": _iso(j.finished_at),
            "updated_at": _iso(j.updated_at)}


def note_out(n: ClovaNote, *, full: bool = False) -> dict[str, Any]:
    d = {"note_id": n.note_id, "title": n.title, "state": n.state, "remote_status": n.remote_status, "duration_ms": n.duration_ms,
         "recorded_at": _iso(n.recorded_at), "remote_updated_at": _iso(n.remote_updated_at), "speakers": n.speakers,
         "has_transcript": bool(n.transcript), "transcript_chars": len(n.transcript or ""), "summary": n.remote_summary,
         "meeting_id": str(n.meeting_id) if n.meeting_id else None, "missing": bool((n.meta or {}).get("missing")),
         "sample": bool((n.meta or {}).get("sample")), "progress": (n.meta or {}).get("progress"),
         "synced_at": _iso(n.synced_at), "transcript_fetched_at": _iso(n.transcript_fetched_at)}
    if full:
        d["segments"] = n.segments
        d["transcript"] = n.transcript
    return d


def _iso(v: datetime | None) -> str | None:
    return v.isoformat() if v else None
