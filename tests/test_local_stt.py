"""Local provider integration uses a deterministic worker; no model/network in pytest."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from test_meetings import _create

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.meetings.local_stt import jobs
from ttakkari.models import LocalSttJob, LocalSttSegment, Meeting


@pytest.fixture
def local(monkeypatch, tmp_path):
    s = get_settings()
    monkeypatch.setattr(
        s, "local_stt_python", __import__("pathlib").Path(__import__("sys").executable)
    )
    model = tmp_path / "model-revision"
    model.mkdir()
    (model / "model.bin").write_bytes(b"test")
    (model / "config.json").write_text("{}")
    monkeypatch.setattr(s, "local_stt_enabled", True)
    monkeypatch.setattr(s, "local_stt_model_path", model)
    monkeypatch.setattr(jobs, "kick", lambda: None)
    calls = []

    async def run(path, params, settings):
        calls.append((path, params))
        offset = params.get("start", 0)
        return {
            "engine": "faster-whisper",
            "engine_version": "test",
            "model": "large-v3",
            "model_version": "revision",
            "duration": 10,
            "start": offset,
            "end": params.get("end", 10),
            "audio_sha256": "a" * 64,
            "text": "원본 발언입니다. FAKE_MEETING",
            "metrics": {"load_seconds": 0.1, "process_exited": True},
            "segments": [
                {
                    "start": offset,
                    "end": offset + 1,
                    "original_text": "원본 발언입니다. FAKE_MEETING",
                    "corrected_text": None,
                    "speaker_id": None,
                    "overlap": None,
                    "words": [],
                    "avg_logprob": -0.8,
                    "no_speech_prob": 0.02,
                    "compression_ratio": 1.1,
                    "flags": [],
                    "verification_status": "unverified",
                }
            ],
            "raw_segments": [
                {"text": "원본 발언입니다. FAKE_MEETING", "tokens": [1, 2]}
            ],
        }

    monkeypatch.setattr(jobs, "run_worker", run)
    return calls


async def audio_meeting(client, auth, new_session, **kw):
    ws, _ = await new_session()
    art = (
        await client.post(
            "/api/uploads",
            headers=auth,
            data={"workspace_id": ws["id"]},
            files={"file": ("a.wav", b"RIFFtest", "audio/wav")},
        )
    ).json()
    return await _create(client, auth, audio_artifact_id=art["id"], **kw)


async def submit(client, auth, mid):
    r = await client.post(
        f"/api/meetings/{mid}/transcribe",
        headers=auth,
        json={"provider_id": "local_whisper", "auto_analyze": False},
    )
    assert r.status_code == 202, r.text
    async with sessionmaker()() as db:
        job = await db.scalar(
            select(LocalSttJob).where(LocalSttJob.meeting_id == uuid.UUID(mid))
        )
        return job.id


async def test_local_provider_is_visible_but_disabled_by_default(client, auth):
    r = await client.get("/api/meetings/providers", headers=auth)
    p = next(p for p in r.json()["providers"] if p["id"] == "local_whisper")
    assert p["state"] == "unconfigured"


async def test_local_transcription_preserves_raw_and_pms_and_vectors(
    local, client, auth, new_session
):
    project = (
        await client.post(
            "/api/pms/projects",
            headers=auth,
            json={"name": "Local", "scope": "personal"},
        )
    ).json()
    m = await audio_meeting(
        client, auth, new_session, project_id=project["id"], participants=["민수"]
    )
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    r = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert (
        r["status"] == "transcribed"
        and r["transcript_source"] == "provider:local_whisper"
    )
    assert r["project"]["id"] == project["id"] and r["participants"] == ["민수"]
    assert r["transcript"] == "원본 발언입니다. FAKE_MEETING"
    for _ in range(100):
        if r["vector"]["status"] == "ready":
            break
        await asyncio.sleep(0.03)
        r = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert r["vector"]["status"] == "ready"
    result = (
        await client.get(f"/api/meetings/{m['id']}/stt/{jid}/segments", headers=auth)
    ).json()
    assert result["segments"][0]["original_text"] == r["transcript"]
    assert result["segments"][0]["corrected_text"] is None
    assert result["segments"][0]["audio_artifact_id"] == m["audio"]["artifact_id"]
    assert result["segments"][0]["raw"]["tokens"] == [1, 2]
    assert "민수" in local[0][1]["terms"] and "Local" in local[0][1]["terms"]
    await jobs.execute_job(jid)  # idempotent, no second inference
    assert len(local) == 1


async def test_reprocessing_is_candidate_and_does_not_overwrite(
    local, client, auth, new_session
):
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    r = await client.post(
        f"/api/meetings/{m['id']}/stt/{jid}/reprocess",
        headers=auth,
        json={"start": 2, "end": 4, "terms": ["새 용어"]},
    )
    assert r.status_code == 202, r.text
    candidate = r.json()
    await jobs.execute_job(uuid.UUID(candidate["id"]))
    assert local[-1][1]["start"] == 2
    result = (
        await client.get(
            f"/api/meetings/{m['id']}/stt/{candidate['id']}/segments", headers=auth
        )
    ).json()
    assert result["segments"][0]["start"] == 2
    async with sessionmaker()() as db:
        meeting = await db.get(Meeting, uuid.UUID(m["id"]))
        assert meeting.provider_job_id == str(jid)
        assert meeting.transcript == "원본 발언입니다. FAKE_MEETING"


async def test_reprocess_wrong_meeting_or_out_of_bounds_is_rejected(
    local, client, auth, new_session
):
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    other = await audio_meeting(client, auth, new_session)
    r = await client.get(
        f"/api/meetings/{other['id']}/stt/{jid}/segments", headers=auth
    )
    assert r.status_code == 404
    for start, end in [(5, 2), (-1, 2), (9, 11), (0, 1000)]:
        r = await client.post(
            f"/api/meetings/{m['id']}/stt/{jid}/reprocess",
            headers=auth,
            json={"start": start, "end": end},
        )
        assert r.status_code == 422


async def test_worker_failure_updates_meeting_and_is_not_empty_success(
    local, client, auth, new_session, monkeypatch
):
    async def failed(*_):
        raise RuntimeError("private detail")

    monkeypatch.setattr(jobs, "run_worker", failed)
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    result = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert result["status"] == "failed" and result["failed_stage"] == "transcription"
    assert "private detail" not in result["error"]
    async with sessionmaker()() as db:
        assert not list(await db.scalars(select(LocalSttSegment)))


async def test_create_auto_process_can_explicitly_select_local(
    local, client, auth, new_session
):
    m = await audio_meeting(
        client, auth, new_session, auto_process=True, provider_id="local_whisper"
    )
    assert m["status"] == "transcribing" and m["provider"] == "local_whisper"


async def test_provider_selection_rejects_unknown_id(client, auth, new_session):
    m = await audio_meeting(client, auth, new_session)
    r = await client.post(
        f"/api/meetings/{m['id']}/transcribe",
        headers=auth,
        json={"provider_id": "missing"},
    )
    assert r.status_code == 409


async def test_local_auto_analysis_uses_existing_pipeline(
    local, client, auth, new_session
):
    m = await audio_meeting(
        client, auth, new_session, auto_process=True, provider_id="local_whisper"
    )
    async with sessionmaker()() as db:
        meeting = await db.get(Meeting, uuid.UUID(m["id"]))
        jid = uuid.UUID(meeting.provider_job_id)
    await jobs.execute_job(jid)
    for _ in range(100):
        result = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
        if result["status"] in ("review", "analyzed", "failed"):
            break
        await asyncio.sleep(0.05)
    assert result["status"] in ("review", "analyzed")
    assert result["items"] and result["analysis_run_id"]


async def test_restart_reconciles_finished_job_without_reinference(
    local, client, auth, new_session, monkeypatch
):
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    original = jobs._reconcile

    async def interrupted(_):
        return

    monkeypatch.setattr(jobs, "_reconcile", interrupted)
    await jobs.execute_job(jid)
    async with sessionmaker()() as db:
        assert (await db.get(Meeting, uuid.UUID(m["id"]))).status == "transcribing"
    monkeypatch.setattr(jobs, "_reconcile", original)
    await jobs.start()
    async with sessionmaker()() as db:
        assert (await db.get(Meeting, uuid.UUID(m["id"]))).status == "transcribed"
    assert len(local) == 1


async def test_restart_resumes_auto_analysis_after_poll_ingested_transcript(
    local, client, auth, new_session, monkeypatch
):
    m = await audio_meeting(
        client, auth, new_session, auto_process=True, provider_id="local_whisper"
    )
    async with sessionmaker()() as db:
        jid = uuid.UUID((await db.get(Meeting, uuid.UUID(m["id"]))).provider_job_id)
    reconcile = jobs._reconcile

    async def interrupted(_):
        return

    monkeypatch.setattr(jobs, "_reconcile", interrupted)
    await jobs.execute_job(jid)
    polled = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert polled["status"] == "transcribed" and polled["analysis_run_id"] is None
    monkeypatch.setattr(jobs, "_reconcile", reconcile)
    await jobs.start()
    async with sessionmaker()() as db:
        assert (await db.get(Meeting, uuid.UUID(m["id"]))).analysis_run_id is not None
    assert len(local) == 1


async def test_term_correction_preview_keeps_persisted_original(
    local, client, auth, new_session
):
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    path = f"/api/meetings/{m['id']}/stt/{jid}/corrections"
    r = await client.post(path, headers=auth, json={"replacements": {"원본": "교정"}})
    assert r.status_code == 200, r.text
    assert r.json()["candidate_only"] is True
    assert r.json()["segments"][0]["corrected_text"].startswith("교정")
    raw = (
        await client.get(f"/api/meetings/{m['id']}/stt/{jid}/segments", headers=auth)
    ).json()["segments"][0]
    assert raw["corrected_text"] is None and raw["original_text"].startswith("원본")
    assert (
        await client.post(path, json={"replacements": {"원본": "교정"}})
    ).status_code == 401
    assert (
        await client.post(path, headers=auth, json={"replacements": {"": "bad"}})
    ).status_code == 422
    assert (
        await client.post(
            path,
            headers=auth,
            json={"replacements": {"원본": "교정"}, "surprise": True},
        )
    ).status_code == 422


async def test_overlap_reference_comparison_is_explicit_and_preserves_meeting(
    local, client, auth, new_session, monkeypatch
):
    run = jobs.run_worker

    async def with_candidate(*args):
        result = await run(*args)
        result["overlap_candidates"] = [
            {
                "status": "succeeded",
                "start": 0,
                "end": 1,
                "original_text": "오늘",
                "tracks": [{"text": "오늘 회의"}, {"text": "시작합니다"}],
                "decision": {"adopt": False, "reason": "no_reference"},
            }
        ]
        return result

    monkeypatch.setattr(jobs, "run_worker", with_candidate)
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    path = f"/api/meetings/{m['id']}/stt/{jid}/overlap/0/compare"
    body = {"references": ["오늘 회의", "시작합니다"], "human_verified": True}
    r = await client.post(path, headers=auth, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["decision"]["adopt"] is True
    assert r.json()["candidate_only"] is True
    assert (
        await client.post(
            path,
            headers=auth,
            json={"references": body["references"], "human_verified": False},
        )
    ).status_code == 422
    assert (
        await client.post(path.replace("/0/", "/9/"), headers=auth, json=body)
    ).status_code == 404
    async with sessionmaker()() as db:
        job = await db.get(LocalSttJob, jid)
        meeting = await db.get(Meeting, uuid.UUID(m["id"]))
        assert (
            job.result_metadata["overlap_candidates"][0]["decision"]["adopt"] is False
        )
        assert meeting.transcript == "원본 발언입니다. FAKE_MEETING"


async def test_unrecovered_loop_is_saved_as_failed_and_never_sent_to_analysis(
    local, client, auth, new_session, monkeypatch
):
    from ttakkari.meetings import pipeline
    from ttakkari.meetings.local_stt.core import normalize_segments
    from ttakkari.meetings.local_stt.quality import recover_quality

    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    async with sessionmaker()() as db:
        job = await db.get(LocalSttJob, jid)
        job.params = {**job.params, "auto_analyze": True}
        await db.commit()
    raw = [
        {"start": i * 2, "end": i * 2 + 2, "text": "계속 같은 문장이 나옵니다."}
        for i in range(12)
    ]
    result = recover_quality(
        normalize_segments(raw, duration=24), audio=[], offset=0, recognize=None
    )

    async def run(*args):
        return {
            **result,
            "duration": 24,
            "raw_segments": raw,
            "text": "계속 같은 문장이 나옵니다.",
        }

    async def forbidden_analysis(*args, **kwargs):
        pytest.fail("Unrecovered severe repetition reached automatic analysis")

    monkeypatch.setattr(jobs, "run_worker", run)
    monkeypatch.setattr(pipeline, "start_analysis", forbidden_analysis)
    await jobs.execute_job(jid)
    async with sessionmaker()() as db:
        job = await db.get(LocalSttJob, jid)
        meeting = await db.get(Meeting, uuid.UUID(m["id"]))
        rows = list(
            await db.scalars(
                select(LocalSttSegment).where(LocalSttSegment.job_id == jid)
            )
        )
        assert job.status == "failed"
        assert "검증" in job.error
        assert job.result_metadata["quality"]["status"] == "needs_review"
        assert job.result_metadata["quality"]["after"]["repeated_segments"] == 0
        assert len(job.result_metadata["post_retry_segments"]) == 12
        assert len(rows) == 12 and job.transcript
        assert meeting.status == "failed" and meeting.failed_stage == "transcription"
        assert meeting.transcript is None and meeting.analysis_run_id is None
    polled = await jobs.LocalWhisperProvider().poll(str(jid))
    assert polled.state == "failed"
