"""Retranscription lineage and the frontend's side-effect-free capability probe."""

import uuid

import pytest
from sqlalchemy import func, select
from test_local_stt import audio_meeting, local  # noqa: F401
from test_meetings import _create

from ttakkari.db import sessionmaker
from ttakkari.meetings import pipeline
from ttakkari.meetings.local_stt import jobs
from ttakkari.models import LocalSttJob, Meeting


async def counts():
    async with sessionmaker()() as db:
        return (
            await db.scalar(select(func.count()).select_from(Meeting)),
            await db.scalar(select(func.count()).select_from(LocalSttJob)),
        )


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize("auto_analyze", [False, True])
async def test_retranscribe_returns_queued_job_separate_from_adopted_job(
    client, auth, new_session, auto_analyze
):
    original = await audio_meeting(client, auth, new_session)
    response = await client.post(
        f"/api/meetings/{original['id']}/retranscribe",
        headers=auth,
        json={"auto_analyze": auto_analyze},
    )
    assert response.status_code == 202, response.text
    created = response.json()
    assert created["status"] == "transcribing"
    assert created["stt_job_id"] is None  # No adopted transcript yet.
    assert created["stt_job"]["status"] == "queued"
    async with sessionmaker()() as db:
        job = await db.get(LocalSttJob, uuid.UUID(created["stt_job"]["id"]))
        assert str(job.meeting_id) == created["id"]
        assert job.status == created["stt_job"]["status"]
        assert job.params["auto_analyze"] is auto_analyze


@pytest.mark.usefixtures("local")
async def test_lineage_is_direct_persistent_and_independent_of_list_filters(
    client, auth, new_session
):
    original = await audio_meeting(client, auth, new_session, title="원본 회의")
    unrelated = await _create(
        client, auth, title="관계없는 회의", transcript="회의 전사"
    )

    async def again(mid):
        response = await client.post(
            f"/api/meetings/{mid}/retranscribe", headers=auth, json={}
        )
        assert response.status_code == 202, response.text
        return response.json()

    first = await again(original["id"])
    second = await again(original["id"])
    grandchild = await again(first["id"])
    expected = {
        original["id"]: (None, [first["id"], second["id"]]),
        first["id"]: (original["id"], [grandchild["id"]]),
        second["id"]: (original["id"], []),
        grandchild["id"]: (first["id"], []),
        unrelated["id"]: (None, []),
    }
    # Changing titles and removing attempt records cannot remove meeting lineage.
    async with sessionmaker()() as db:
        child = await db.get(Meeting, uuid.UUID(first["id"]))
        child.title = "다른 이름"
        job = await db.get(LocalSttJob, uuid.UUID(child.provider_job_id))
        await db.delete(job)
        await db.commit()
    listed = (await client.get("/api/meetings", headers=auth)).json()["meetings"]
    assert len(listed) == len(expected)
    for row in listed:
        detail = (await client.get(f"/api/meetings/{row['id']}", headers=auth)).json()
        for data in (row, detail):
            assert (data["source_meeting_id"], data["derived_meeting_ids"]) == expected[
                row["id"]
            ]
    filtered = (
        await client.get(
            "/api/meetings",
            headers=auth,
            params={"q": "원본", "status": "uploaded", "limit": 1},
        )
    ).json()["meetings"]
    assert filtered[0]["id"] == original["id"]
    assert filtered[0]["derived_meeting_ids"] == [first["id"], second["id"]]
    # Deleting the source detaches children, without cascading through their versions.
    assert (
        await client.delete(f"/api/meetings/{original['id']}", headers=auth)
    ).status_code == 204
    first_detail = (
        await client.get(f"/api/meetings/{first['id']}", headers=auth)
    ).json()
    assert first_detail["source_meeting_id"] is None
    assert first_detail["derived_meeting_ids"] == [grandchild["id"]]


@pytest.mark.usefixtures("local")
@pytest.mark.parametrize("invalid", ["probe", "false", "true", 0, 1, None, [], {}])
async def test_capability_probe_validates_before_creation_or_queueing(
    client, auth, new_session, monkeypatch, invalid
):
    original = await audio_meeting(client, auth, new_session)
    before = await counts()

    async def must_not_create(*args, **kwargs):
        pytest.fail("Invalid capability probe reached meeting creation")

    def must_not_kick():
        pytest.fail("Invalid capability probe woke the transcription worker")

    monkeypatch.setattr(pipeline, "create_meeting", must_not_create)
    monkeypatch.setattr(jobs, "kick", must_not_kick)
    for mid in (original["id"], str(uuid.uuid4())):
        response = await client.post(
            f"/api/meetings/{mid}/retranscribe",
            headers=auth,
            json={"auto_analyze": invalid},
        )
        assert response.status_code == 422, response.text
        assert any(
            error["loc"] == ["body", "auto_analyze"]
            for error in response.json()["errors"]
        )
        assert await counts() == before


async def test_unavailable_runtime_does_not_create_retranscription(
    client, auth, new_session
):
    original = await audio_meeting(client, auth, new_session)
    before = await counts()
    path = f"/api/meetings/{original['id']}/retranscribe"
    assert (
        await client.post(path, headers=auth, json={"auto_analyze": "probe"})
    ).status_code == 422
    assert (await client.post(path, headers=auth, json={})).status_code == 409
    assert await counts() == before


async def test_retranscribe_rejections_have_no_side_effects(client, auth):
    text_meeting = await _create(
        client, auth, title="텍스트 회의", transcript="회의 전사"
    )
    before = await counts()
    path = f"/api/meetings/{text_meeting['id']}/retranscribe"
    assert (await client.post(path, json={"auto_analyze": "probe"})).status_code == 401
    assert (await client.post(path, headers=auth, json={})).status_code == 409
    assert (
        await client.post(path, headers=auth, json={"unknown": True})
    ).status_code == 422
    missing = await client.post(
        f"/api/meetings/{uuid.uuid4()}/retranscribe", headers=auth, json={}
    )
    assert missing.status_code == 404
    assert await counts() == before
