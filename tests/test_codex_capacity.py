"""Model saturation must recover with progress, without exhausting an account's quota."""
import time
from uuid import UUID

import pytest
from conftest import agent_calls
from helpers import run_events, run_to_end, start_run, wait_event, wait_run
from test_orchestrator import (  # noqa: F401 -- shared pytest fixture
    _session,
    push_calls,
)

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.runs import profiles
from ttakkari.runs.manager import manager


@pytest.mark.parametrize("suffix", ["", " CAPACITY_STDERR"])
async def test_capacity_falls_back_with_progress_and_skips_same_model_accounts(client, auth, make_dir, suffix):
    accounts = (await client.get("/api/accounts", headers=auth)).json()
    codex = next(a for a in accounts if a["engine"] == "codex")
    claude = next(a for a in accounts if a["engine"] == "claude")
    second = (await client.post("/api/accounts", headers=auth, json={
        "name": "codex-2", "engine": "codex", "config_dir": str(make_dir()),
    })).json()
    await client.post("/api/accounts/reorder", headers=auth,
                      json={"ids": [codex["id"], second["id"], claude["id"]]})
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    await run_to_end(client, auth, sess["id"], "project alpha previous turn")
    before = len(agent_calls())
    run = await run_to_end(client, auth, sess["id"], "continue alpha CODEX_CAPACITY" + suffix)
    assert run["status"] == "succeeded", run
    calls = agent_calls()[before:]
    assert [c["engine"] for c in calls] == ["codex", "claude"]
    prompt = calls[-1]["argv"][calls[-1]["argv"].index("-p") + 1]
    assert "project alpha previous turn" in prompt
    assert "Existing worker was already created; checkpoint alpha saved." in prompt
    assert "Avoid re-executing non-idempotent operations" in prompt
    assert "--resume" not in calls[-1]["argv"]
    events = await run_events(client, auth, run["id"])
    fallback = [e["payload"] for e in events if e["type"] == "engine.fallback"]
    assert len(fallback) == 1
    assert fallback[0]["reason"] == "model_capacity"
    assert fallback[0]["mode"] == "handoff"
    assert not any(e["type"] == "account.limited" for e in events)
    assert profiles.limited_until(UUID(codex["id"])) is None
    # A new request also avoids the saturated model while its short cooldown lasts.
    before = len(agent_calls())
    assert (await run_to_end(client, auth, sess["id"], "next alpha"))["status"] == "succeeded"
    assert [c["engine"] for c in agent_calls()[before:]] == ["claude"]


async def test_capacity_without_alternative_waits_and_preserves_progress(client, auth, make_dir, request):
    notifications = request.getfixturevalue("push_calls")
    sess = await _session(client, auth, make_dir, engine="codex", model="gpt-6.1-sol", strict_engine=True)
    run = await start_run(client, auth, sess["id"], "CODEX_CAPACITY")
    waiting = await wait_event(client, auth, run["id"], "run.waiting")
    assert "checkpoint alpha saved" in waiting["payload"]["handoff"]
    assert waiting["payload"]["reason"] == "model_capacity"
    assert "모델 혼잡" in notifications[0][1]
    assert len(agent_calls()) == 1
    assert profiles.cooldown_until(profiles.model_capacity_key("codex", "gpt-6.1-sol")) > time.time()
    await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)


async def test_capacity_cooldown_is_model_scoped_and_persisted(client, auth):
    key = profiles.model_capacity_key("codex", "gpt-6.1-sol")
    profiles.cool_down(key, 120)
    await profiles.flush_persist()
    profiles.reset_cooldowns()
    async with sessionmaker()() as db:
        await profiles.load_cooldowns(db)
    assert profiles.cooldown_until(key) > time.time()
    assert profiles.cooldown_until(profiles.model_capacity_key("codex", "gpt-6-luna")) is None
    assert profiles.cooldown_until(profiles.model_capacity_key("claude", "gpt-6.1-sol")) is None


async def test_capacity_wait_automatically_resumes_same_run(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(profiles, "MODEL_CAPACITY_COOLDOWN_SECONDS", 1)
    sess = await _session(client, auth, make_dir, engine="codex", model="gpt-6.1-sol", strict_engine=True)
    run = await start_run(client, auth, sess["id"], "CODEX_CAPACITY CAPACITY_ONCE")
    await wait_event(client, auth, run["id"], "run.waiting")
    done = await wait_run(client, auth, run["id"])
    assert done["status"] == "succeeded", done
    assert len(agent_calls()) == 2
    call = agent_calls()[-1]["argv"]
    assert "resume" in call
    assert "checkpoint alpha saved" in call[call.index("--") + 1]
    events = await run_events(client, auth, run["id"])
    assert sum(e["type"] == "run.resumed" for e in events) == 1


async def test_capacity_cooldown_does_not_disable_another_codex_model(client, auth, make_dir):
    profiles.cool_down(profiles.model_capacity_key("codex", "gpt-6.1-sol"), 120)
    sess = await _session(client, auth, make_dir, engine="codex", model="gpt-6-luna", strict_engine=True)
    done = await run_to_end(client, auth, sess["id"], "use the available model")
    assert done["status"] == "succeeded", done
    assert [c["engine"] for c in agent_calls()] == ["codex"]


async def test_capacity_cooldown_is_not_all_cloud_quota_exhaustion(client, auth, make_dir, monkeypatch):
    profiles.cool_down(profiles.model_capacity_key("codex", "gpt-6.1-sol"), 120)
    emergency_calls = []

    async def unexpected_qwen(*args, **kwargs):
        emergency_calls.append(True)
        return None, "ineligible"

    monkeypatch.setattr(manager, "_emergency_qwen", unexpected_qwen)
    sess = await _session(client, auth, make_dir, engine="codex", model="gpt-6.1-sol", strict_engine=True)
    run = await start_run(client, auth, sess["id"], "wait for the model")
    await wait_event(client, auth, run["id"], "run.waiting")
    assert not agent_calls()
    await client.post(f"/api/runs/{run['id']}/cancel", headers=auth)
    assert not emergency_calls


async def test_persistent_capacity_stops_at_wait_limit(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(profiles, "MODEL_CAPACITY_COOLDOWN_SECONDS", 1)
    monkeypatch.setattr(get_settings(), "max_limit_waits", 1)
    sess = await _session(client, auth, make_dir, engine="codex", model="gpt-6.1-sol", strict_engine=True)
    run = await start_run(client, auth, sess["id"], "CODEX_CAPACITY")
    done = await wait_run(client, auth, run["id"])
    assert done["status"] == "failed", done
    assert "모델 혼잡" in done["error"]
    assert len(agent_calls()) == 2
