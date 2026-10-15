import uuid

import pytest
from test_local_stt import audio_meeting, local, submit  # noqa: F401

from ttakkari.db import sessionmaker
from ttakkari.meetings.local_stt import jobs
from ttakkari.models import LocalSttJob, Meeting


@pytest.mark.usefixtures("local")
async def test_utterances_contract_auth_pagination_and_job_isolation(
    client, auth, new_session, monkeypatch
):
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    path = f"/api/meetings/{m['id']}/stt/{jid}/utterances"
    assert (await client.get(path)).status_code == 401
    r = await client.get(path, headers=auth, params={"offset": 0, "limit": 1})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["total"] == 1 and d["has_more"] is False
    assert {
        "start",
        "end",
        "speaker_id",
        "speaker_label",
        "text",
        "verification_status",
        "overlap",
    } <= d["utterances"][0].keys()
    assert d["utterances"][0]["speaker_id"] is None
    assert d["utterances"][0]["overlap"] is None
    assert (
        await client.get(path, headers=auth, params={"limit": 501})
    ).status_code == 422
    other = await audio_meeting(client, auth, new_session)
    assert (
        await client.get(
            f"/api/meetings/{other['id']}/stt/{jid}/utterances", headers=auth
        )
    ).status_code == 404


@pytest.mark.usefixtures("local")
async def test_retranscription_creates_new_meeting_and_preserves_old_analysis(
    client, auth, new_session
):
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    before = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    r = await client.post(
        f"/api/meetings/{m['id']}/retranscribe",
        headers=auth,
        json={"auto_analyze": False},
    )
    assert r.status_code == 202, r.text
    new = r.json()
    assert new["id"] != m["id"] and new["status"] == "transcribing"
    after = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert (before["transcript"], before["analysis_run_id"], before["status"]) == (
        after["transcript"],
        after["analysis_run_id"],
        after["status"],
    )
    async with sessionmaker()() as db:
        meeting = await db.get(Meeting, uuid.UUID(new["id"]))
        job = await db.get(LocalSttJob, uuid.UUID(meeting.provider_job_id))
        assert job.params["source_meeting_id"] == m["id"]
        assert job.params["profile"] == "robust"
    await jobs.execute_job(job.id)
    result = (await client.get(f"/api/meetings/{new['id']}", headers=auth)).json()
    assert (
        result["status"] == "transcribed"
        and result["audio"]["artifact_id"] == before["audio"]["artifact_id"]
    )


@pytest.mark.usefixtures("local")
async def test_quality_presentation_survives_persistence_without_replacing_raw(
    client, auth, new_session, monkeypatch
):
    run = jobs.run_worker

    async def quality_result(*args):
        result = await run(*args)
        result.update(
            original_text=result["text"],
            text="[0–1] 화자 1 [검증 필요]: 짧은 후보",
            speaker_count=2,
            diarization_status="succeeded",
            overlap_detection_status="unsupported",
            utterances=[
                {
                    "id": "0",
                    "start": 0,
                    "end": 1,
                    "speaker_id": "SPEAKER_00",
                    "speaker_label": "화자 1",
                    "text": "짧은 후보",
                    "verification_status": "needs_review",
                    "overlap": None,
                    "flags": ["repetition_folded"],
                    "segment_positions": [0],
                }
            ],
        )
        return result

    monkeypatch.setattr(jobs, "run_worker", quality_result)
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    path = f"/api/meetings/{m['id']}/stt/{jid}"
    d = (await client.get(path + "/utterances", headers=auth)).json()
    assert d["speaker_count"] == 2 and d["utterances"][0]["text"] == "짧은 후보"
    raw = (await client.get(path + "/segments", headers=auth)).json()["segments"][0]
    assert raw["original_text"] == "원본 발언입니다. FAKE_MEETING"
    assert raw["raw"]["tokens"] == [1, 2]
    m = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert "짧은 후보" in m["transcript"] and "FAKE_MEETING" not in m["transcript"]
