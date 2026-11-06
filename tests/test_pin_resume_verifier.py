"""Verifier-only queued-resume checks for the immutable account-pin implementation."""
from __future__ import annotations

import json
import stat
import uuid

import pytest
from conftest import agent_calls
from helpers import run_events, wait_run

from ttakkari import artcraft_catalog as catalog
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, EngineAccount, Run
from ttakkari.runs import manager as run_manager


@pytest.fixture
def platform(tmp_path, make_dir, monkeypatch):
    base = tmp_path / "artcraft-pin-resume"
    (base / "bin").mkdir(parents=True)
    script = base / "bin" / "artcraft"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    apps = {app: {"title": app, "domain": app, "site_status": "x", "control_port": None,
                  "control_auth": "none-documented", "env": {}, "mcp_args": [], "cli_modes": []}
            for category in catalog.CATEGORIES for app in catalog.app_ids(category)}
    (base / "registry.json").write_text(json.dumps({"apps": apps}))
    root = make_dir()
    settings = get_settings()
    monkeypatch.setattr(settings, "artcraft_root", base)
    monkeypatch.setattr(settings, "artcraft_workspace_root", root)
    return type("Platform", (), {"base": base, "root": root})()


@pytest.mark.parametrize("account_change", ["disabled", "deleted"])
async def test_queued_artcraft_pin_survives_new_session_and_fails_closed_on_resume(
    client, auth, platform, monkeypatch, account_change,
):
    pinned_response = await client.post(
        "/api/accounts", headers=auth,
        json={"name": f"resume-pinned-{account_change}", "engine": "codex"},
    )
    peer_response = await client.post(
        "/api/accounts", headers=auth,
        json={"name": f"resume-peer-{account_change}", "engine": "codex"},
    )
    assert pinned_response.status_code == peer_response.status_code == 201
    pinned_id = uuid.UUID(pinned_response.json()["id"])

    async def do_not_dispatch(_session_id):
        return None

    real_dispatch = run_manager.manager.dispatch
    monkeypatch.setattr(run_manager.manager, "dispatch", do_not_dispatch)
    created = await client.post(
        "/api/artcraft/tasks", headers=auth,
        json={"category": "pdf", "prompt": "ART_OUT resume boundary",
              "account_id": str(pinned_id)},
    )
    assert created.status_code == 201, created.text
    monkeypatch.setattr(run_manager.manager, "dispatch", real_dispatch)

    task = created.json()
    run_id = uuid.UUID(task["rounds"][0]["id"])
    session_id = uuid.UUID(task["session_id"])
    async with sessionmaker()() as db:
        session = await db.get(ChatSession, session_id)
        run = await db.get(Run, run_id)
        assert session.account_id == pinned_id
        assert run.usage["artcraft"]["account_id"] == str(pinned_id)
        assert run.status == "queued"

        pinned = await db.get(EngineAccount, pinned_id)
        if account_change == "disabled":
            pinned.enabled = False
        else:
            await db.delete(pinned)
        await db.commit()

    # Reload through a distinct AsyncSession before restart/resume dispatch.
    async with sessionmaker()() as db:
        session = await db.get(ChatSession, session_id)
        run = await db.get(Run, run_id)
        assert session.account_id == pinned_id
        assert run.usage["artcraft"]["account_id"] == str(pinned_id)
        remaining_account = await db.get(EngineAccount, pinned_id)
        if account_change == "deleted":
            assert remaining_account is None
        else:
            assert remaining_account is not None and not remaining_account.enabled

    calls_before = len(agent_calls())
    resumed_sessions = await run_manager.resume_queued()
    assert resumed_sessions == 1
    result = await wait_run(client, auth, str(run_id))
    assert result["status"] == "failed"
    assert result["profile"] is None
    assert len(agent_calls()) == calls_before
    events = await run_events(client, auth, str(run_id))
    assert not any(event["type"] == "engine.fallback" for event in events)
    assert not any(event["type"] == "worker.routing" and event.get("data", {}).get("trigger") == "emergency"
                   for event in events)
