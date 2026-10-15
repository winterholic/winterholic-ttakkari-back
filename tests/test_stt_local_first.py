import asyncio

import pytest
from test_local_stt import audio_meeting, local  # noqa: F401

from ttakkari.config import Settings, get_settings
from ttakkari.meetings import clova_jobs, providers
from ttakkari.meetings.local_stt import jobs


def test_local_first_defaults_do_not_start_external_sync():
    s = Settings(_env_file=None)
    assert s.meeting_default_provider == "local_whisper"
    assert s.local_stt_enabled is True
    assert s.clovanote_enabled is False
    assert s.clovanote_poll_seconds == 0


def test_default_provider_is_local_even_if_unconfigured(monkeypatch):
    monkeypatch.setattr(get_settings(), "local_stt_enabled", False)
    assert providers.get_provider().id == "local_whisper"
    assert providers.get_provider("clovanote").id == "clovanote"


async def test_provider_description_marks_default_without_loading_model(client, auth):
    result = (await client.get("/api/meetings/providers", headers=auth)).json()
    assert result["default_provider"] == "local_whisper"
    engine = next(p for p in result["providers"] if p["id"] == "local_whisper")
    assert engine["is_default"] is True


async def test_disabled_clova_start_does_not_touch_queue(monkeypatch):
    def forbidden():
        pytest.fail("disabled Clova must not open DB or start workers")

    monkeypatch.setattr(clova_jobs, "sessionmaker", forbidden)
    await clova_jobs.start()


async def test_queue_sleeps_without_periodic_database_reads(monkeypatch):
    calls = []

    class DB:
        async def __aenter__(self):
            calls.append("open")
            return self

        async def __aexit__(self, *args):
            pass

        async def scalar(self, stmt):
            return None

    monkeypatch.setattr(jobs, "sessionmaker", lambda: DB)
    monkeypatch.setattr(get_settings(), "local_stt_enabled", True)
    await jobs.stop()
    jobs.kick()
    try:
        await asyncio.sleep(0.05)
        assert len(calls) == 1
        await asyncio.sleep(1.1)
        assert len(calls) == 1
        jobs.kick()
        await asyncio.sleep(0.05)
        assert len(calls) == 2
    finally:
        await jobs.stop()


@pytest.mark.parametrize("auto_create", [False, True])
@pytest.mark.usefixtures("local")
async def test_default_submission_wakes_worker_after_meeting_link_commit(
    client, auth, new_session, monkeypatch, auto_create
):
    import uuid

    from ttakkari.db import sessionmaker
    from ttakkari.models import Meeting

    snapshots = []
    tasks = []

    async def inspect_link():
        async with sessionmaker()() as db:
            from sqlalchemy import select

            m = await db.scalar(
                select(Meeting).where(Meeting.provider == "local_whisper")
            )
            snapshots.append(m.provider_job_id if m else None)

    monkeypatch.setattr(
        jobs, "kick", lambda: tasks.append(asyncio.create_task(inspect_link()))
    )
    if auto_create:
        m = await audio_meeting(client, auth, new_session, auto_process=True)
        assert m["provider"] == "local_whisper"
    else:
        m = await audio_meeting(client, auth, new_session)
        r = await client.post(
            f"/api/meetings/{m['id']}/transcribe",
            headers=auth,
            json={"auto_analyze": False},
        )
        assert r.status_code == 202
        assert r.json()["provider"] == "local_whisper"
    await asyncio.gather(*tasks)
    assert snapshots and all(snapshots)
    uuid.UUID(snapshots[-1])


def test_explicit_default_override_is_respected(monkeypatch):
    monkeypatch.setattr(get_settings(), "meeting_default_provider", "clovanote")
    assert providers.get_provider().id == "clovanote"
    flags = {p["id"]: p["is_default"] for p in providers.describe_providers()}
    assert flags == {"clovanote": True, "local_whisper": False}
