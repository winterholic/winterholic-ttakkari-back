"""Frontend contract additions, tested using real DB/HTTP and a fake STT worker."""

import uuid
from copy import deepcopy

import pytest
from sqlalchemy import select
from test_local_stt import audio_meeting, local, submit  # noqa: F401
from test_meetings import _create

from ttakkari.db import sessionmaker
from ttakkari.meetings.local_stt import jobs
from ttakkari.meetings.local_stt.utterances import render_transcript
from ttakkari.models import LocalSttJob, LocalSttSegment, Meeting, MeetingItem, Run


async def prepared(client, auth, new_session):
    m = await audio_meeting(client, auth, new_session)
    jid = await submit(client, auth, m["id"])
    await jobs.execute_job(jid)
    texts = [
        "배포 일정은 목요일로 확정합니다.",
        "민수가 배포 체크리스트를 작성합니다.",
        "모바일 화면 간격을 먼저 조정합시다.",
    ]
    utterances = [
        {
            "id": str(i),
            "start": 1 + i * 3,
            "end": 3 + i * 3,
            "speaker_id": f"SPEAKER_{i % 2:02d}",
            "speaker_label": f"화자 {i % 2 + 1}",
            "text": text,
            "verification_status": "unverified",
            "overlap": None,
            "flags": [],
            "segment_positions": [0],
        }
        for i, text in enumerate(texts)
    ]
    async with sessionmaker()() as db:
        j = await db.get(LocalSttJob, jid)
        j.transcript = render_transcript(utterances)
        meeting = await db.get(Meeting, uuid.UUID(m["id"]))
        meeting.transcript = j.transcript
        j.result_metadata = {
            **j.result_metadata,
            "utterances": utterances,
            "speaker_count": 2,
            "diarization_status": "succeeded",
            "diarization": {
                "turns": [
                    {k: u[k] for k in ("start", "end", "speaker_id")}
                    for u in utterances
                ]
            },
            "quality_retries": [{"raw_segments": [{"tokens": list(range(1000))}]}],
        }
        for kind, quote in zip(
            ("decision", "action", "discussion"), texts, strict=True
        ):
            db.add(
                MeetingItem(
                    meeting_id=uuid.UUID(m["id"]),
                    kind=kind,
                    position=0,
                    text="분석된 내용",
                    evidence=quote,
                    conflicts=[],
                    status="open",
                )
            )
        db.add(
            MeetingItem(
                meeting_id=uuid.UUID(m["id"]),
                kind="open_issue",
                position=0,
                text="미해결",
                evidence="전사에 없는 인용입니다.",
                conflicts=[],
                status="open",
            )
        )
        await db.commit()
    return m, jid, utterances


@pytest.mark.usefixtures("local")
async def test_speaker_names_persist_separately_and_appear_on_every_page(
    client, auth, new_session
):
    m, jid, utterances = await prepared(client, auth, new_session)
    base = f"/api/meetings/{m['id']}/stt/{jid}"
    async with sessionmaker()() as db:
        j = await db.get(LocalSttJob, jid)
        original = deepcopy(j.result_metadata)
    assert (
        await client.put(base + "/speakers", json={"names": {"SPEAKER_00": "민수"}})
    ).status_code == 401
    r = await client.put(
        base + "/speakers",
        headers=auth,
        json={"names": {"SPEAKER_00": "  민수  ", "SPEAKER_01": "지영"}},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "job_id": str(jid),
        "names": {"SPEAKER_00": "민수", "SPEAKER_01": "지영"},
    }
    page = (
        await client.get(
            base + "/utterances", headers=auth, params={"offset": 1, "limit": 1}
        )
    ).json()
    assert page["utterances"][0]["speaker_name"] == "지영"
    assert page["speaker_names"] == {"SPEAKER_00": "민수", "SPEAKER_01": "지영"}
    async with sessionmaker()() as db:
        j = await db.get(LocalSttJob, jid)
        assert j.result_metadata["utterances"] == original["utterances"] == utterances
        assert j.result_metadata["quality_retries"] == original["quality_retries"]
        raw = await db.scalar(
            select(LocalSttSegment).where(LocalSttSegment.job_id == jid)
        )
        assert raw.identified_person is None and raw.raw["tokens"] == [1, 2]
    assert (
        await client.put(base + "/speakers", headers=auth, json={"names": {}})
    ).status_code == 200
    assert (await client.get(base + "/utterances", headers=auth)).json()["utterances"][
        0
    ]["speaker_name"] is None


@pytest.mark.usefixtures("local")
async def test_speaker_names_reject_unknown_ids_invalid_names_and_other_meeting(
    client, auth, new_session
):
    m, jid, _ = await prepared(client, auth, new_session)
    base = f"/api/meetings/{m['id']}/stt/{jid}"
    for body in (
        {"names": {"UNKNOWN": "민수"}},
        {"names": {"SPEAKER_00": " "}},
        {"names": {"SPEAKER_00": "a" * 41}},
        {"names": {"SPEAKER_00": "민\u0000수"}},
        {"names": {"SPEAKER_00": "민\u0085수"}},
        {"names": {"SPEAKER_00": 123}},
        {"names": {}, "extra": True},
    ):
        assert (
            await client.put(base + "/speakers", headers=auth, json=body)
        ).status_code == 422
    other = await audio_meeting(client, auth, new_session)
    assert (
        await client.put(
            f"/api/meetings/{other['id']}/stt/{jid}/speakers",
            headers=auth,
            json={"names": {}},
        )
    ).status_code == 404
    pending = await submit(client, auth, other["id"])
    assert (
        await client.put(
            f"/api/meetings/{other['id']}/stt/{pending}/speakers",
            headers=auth,
            json={"names": {}},
        )
    ).status_code == 409


@pytest.mark.usefixtures("local")
async def test_detail_locations_use_adopted_job_and_no_extra_analysis(
    client, auth, new_session
):
    m, jid, _ = await prepared(client, auth, new_session)
    async with sessionmaker()() as db:
        candidate = LocalSttJob(
            meeting_id=uuid.UUID(m["id"]),
            audio_artifact_id=uuid.UUID(m["audio"]["artifact_id"]),
            parent_job_id=jid,
            status="succeeded",
            params={},
            result_metadata={"utterances": []},
        )
        db.add(candidate)
        await db.commit()
    detail = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert detail["stt_job_id"] == str(jid)
    assert detail["analysis_run_id"] is None
    async with sessionmaker()() as db:
        assert list(await db.scalars(select(Run.id))) == []
    items = {i["kind"]: i for i in detail["items"]}
    for kind, start, uid in [
        ("decision", 1, "0"),
        ("action", 4, "1"),
        ("discussion", 7, "2"),
    ]:
        assert (
            items[kind]["evidence_start"],
            items[kind]["evidence_utterance_id"],
        ) == (start, uid)
    assert items["open_issue"]["evidence_start"] is None
    r = await client.patch(
        f"/api/meetings/{m['id']}/items/{items['action']['id']}",
        headers=auth,
        json={"status": "done"},
    )
    assert r.status_code == 200 and r.json()["evidence_start"] == 4
    plain = await _create(client, auth, transcript="붙여넣은 전사입니다.")
    assert plain["stt_job_id"] is None
    async with sessionmaker()() as db:
        meeting = await db.get(Meeting, uuid.UUID(m["id"]))
        meeting.transcript_source = "paste"
        await db.commit()
    detail = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert detail["stt_job_id"] is None and all(
        i["evidence_start"] is None for i in detail["items"]
    )


@pytest.mark.usefixtures("local")
async def test_queued_replacement_does_not_hide_the_adopted_transcript(
    client, auth, new_session
):
    m, jid, _ = await prepared(client, auth, new_session)
    async with sessionmaker()() as db:
        queued = LocalSttJob(
            meeting_id=uuid.UUID(m["id"]),
            status="queued",
            params={},
            result_metadata={},
        )
        db.add(queued)
        await db.flush()
        meeting = await db.get(Meeting, uuid.UUID(m["id"]))
        meeting.provider_job_id = str(queued.id)
        await db.commit()
    detail = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
    assert detail["stt_job_id"] == str(jid)
    assert (
        next(i for i in detail["items"] if i["kind"] == "decision")["evidence_start"]
        == 1
    )


@pytest.mark.usefixtures("local")
async def test_names_do_not_leak_to_another_job_and_legacy_utterances_are_supported(
    client, auth, new_session
):
    m, jid, _ = await prepared(client, auth, new_session)
    base = f"/api/meetings/{m['id']}/stt/{jid}"
    async with sessionmaker()() as db:
        j = await db.get(LocalSttJob, jid)
        j.result_metadata = {
            k: v for k, v in j.result_metadata.items() if k != "utterances"
        }
        s = await db.scalar(
            select(LocalSttSegment).where(LocalSttSegment.job_id == jid)
        )
        s.speaker_id = "SPEAKER_00"
        j.result_metadata = {
            **j.result_metadata,
            "diarization": {
                "turns": [{"start": 0, "end": 1, "speaker_id": "SPEAKER_00"}]
            },
        }
        second = LocalSttJob(
            meeting_id=uuid.UUID(m["id"]),
            status="succeeded",
            params={},
            result_metadata={
                "utterances": [
                    {
                        "id": "0",
                        "start": 0,
                        "end": 1,
                        "speaker_id": "SPEAKER_00",
                        "speaker_label": "화자 1",
                        "text": "별도 시도",
                        "verification_status": "unverified",
                        "overlap": None,
                        "flags": [],
                        "segment_positions": [],
                    }
                ]
            },
        )
        db.add(second)
        await db.commit()
        second_id = second.id
    r = await client.put(
        base + "/speakers", headers=auth, json={"names": {"SPEAKER_00": "민수"}}
    )
    assert r.status_code == 200, r.text
    legacy = (await client.get(base + "/utterances", headers=auth)).json()
    assert legacy["utterances"][0]["speaker_name"] == "민수"
    other = (
        await client.get(
            f"/api/meetings/{m['id']}/stt/{second_id}/utterances", headers=auth
        )
    ).json()
    assert (
        other["speaker_names"] == {} and other["utterances"][0]["speaker_name"] is None
    )


@pytest.mark.usefixtures("local")
async def test_light_segments_omit_large_columns_and_job_metadata(
    client, auth, new_session
):
    m, jid, _ = await prepared(client, auth, new_session)
    path = f"/api/meetings/{m['id']}/stt/{jid}/segments"
    full = (await client.get(path, headers=auth)).json()
    assert "raw" in full["segments"][0] and "words" in full["segments"][0]
    light = (await client.get(path, headers=auth, params={"fields": "light"})).json()
    assert "raw" not in light["segments"][0] and "words" not in light["segments"][0]
    assert light["segments"][0]["original_text"] == full["segments"][0]["original_text"]
    assert (
        not {"quality_retries", "utterances", "diarization"}
        & light["job"]["metadata"].keys()
    )
    selected = (
        await client.get(
            path, headers=auth, params={"fields": "start,end,original_text"}
        )
    ).json()
    assert set(selected["segments"][0]) == {
        "id",
        "job_id",
        "audio_artifact_id",
        "start",
        "end",
        "original_text",
    }
    assert len(str(light)) < len(str(full)) / 2
    for invalid in ("rawx", "", "start;drop table meetings"):
        assert (
            await client.get(path, headers=auth, params={"fields": invalid})
        ).status_code == 422
