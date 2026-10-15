"""Removal leaves historic meeting data usable and never rediscovers credentials."""

import sys
from pathlib import Path

import pytest
from test_local_stt import local  # noqa: F401

from ttakkari.config import Settings
from ttakkari.db import sessionmaker
from ttakkari.meetings import providers
from ttakkari.models import Meeting, MeetingItem


def test_only_local_provider_is_registered():
    assert {p["id"] for p in providers.describe_providers()} == {"local_whisper"}
    with pytest.raises(providers.ProviderNotAvailable):
        providers.get_provider("clovanote")


def test_old_config_keys_are_ignored_but_current_keys_take_effect(
    tmp_path, monkeypatch
):
    path = tmp_path / "synthetic-config.env"
    path.write_text(
        "TTAKKARI_CLOVANOTE_ENABLED=true\nTTAKKARI_CLOVANOTE_POLL_SECONDS=10\nTTAKKARI_MEETING_DEFAULT_PROVIDER=clovanote\nTTAKKARI_LOCAL_STT_CPU_THREADS=6\n"
    )
    monkeypatch.delenv("TTAKKARI_LOCAL_STT_CPU_THREADS", raising=False)
    s = Settings(_env_file=path)
    assert not hasattr(s, "clovanote_enabled")
    assert not hasattr(s, "meeting_default_provider")
    assert s.local_stt_cpu_threads == 6


def test_unconfigured_runtime_paths_have_stable_defaults(monkeypatch, tmp_path):
    monkeypatch.delenv("TTAKKARI_LOCAL_STT_MODEL_PATH", raising=False)
    s = Settings(_env_file=None, home=tmp_path / "app")
    assert (
        s.local_stt_python
        == Path.home()
        / "development/projects/winterholic-ttakkari/back/.stt-runtime/venv/bin/python"
    )
    assert (
        s.local_stt_model_path
        == Path.home()
        / "development/projects/winterholic-ttakkari/back/.stt-runtime/faster-whisper-large-v3"
    )
    override = Settings(
        _env_file=None,
        local_stt_python=tmp_path / "python",
        local_stt_model_path=tmp_path / "model",
    )
    assert override.local_stt_python == tmp_path / "python"
    assert override.local_stt_model_path == tmp_path / "model"
    assert "torch" not in sys.modules and "faster_whisper" not in sys.modules


async def test_removed_api_and_agent_tools_are_absent(client, auth):
    for path in [
        "/api/clovanote/status",
        "/api/clovanote/jobs",
        "/api/clovanote/notes",
    ]:
        assert (await client.get(path, headers=auth)).status_code == 404
    for path in ["/api/clovanote/sync", "/api/clovanote/login"]:
        assert (await client.post(path, headers=auth)).status_code == 404
    from ttakkari.orchestrator.mcp_server import server

    tools = await server.list_tools()
    assert not any("clovanote" in t.name for t in tools)


async def test_historic_meetings_remain_readable_searchable_analyzable(client, auth):
    async with sessionmaker()() as db:
        m = Meeting(
            title="예전 회의",
            scope="personal",
            status="transcribed",
            source="text",
            provider="clovanote",
            provider_job_id="old-job",
            transcript="검수된 과거 전사",
            transcript_source="provider:clovanote",
        )
        db.add(m)
        await db.flush()
        db.add(
            MeetingItem(
                meeting_id=m.id,
                kind="decision",
                position=0,
                text="이전 결정 보존",
                status="open",
            )
        )
        await db.commit()
        mid = str(m.id)
    detail = await client.get(f"/api/meetings/{mid}", headers=auth)
    assert detail.status_code == 200, detail.text
    assert detail.json()["transcript"] == "검수된 과거 전사"
    assert detail.json()["provider"] == "clovanote"
    assert "clovanote" not in detail.json()
    assert detail.json()["items"][0]["text"] == "이전 결정 보존"
    listing = await client.get("/api/meetings", params={"q": "이전 결정"}, headers=auth)
    assert listing.json()["meetings"][0]["id"] == mid
    # The normal analysis path accepts the preserved transcript independently of its old provider.
    result = await client.post(f"/api/meetings/{mid}/analyze", headers=auth)
    assert result.status_code == 202, result.text
