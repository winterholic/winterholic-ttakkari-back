"""Model family, independent quota pools and current worker routing decisions."""
import json
import textwrap
import time
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from ttakkari.api.orchestrator import WorkerIn
from ttakkari.config import get_settings
from ttakkari.runs import profiles, usage


@pytest.mark.parametrize("model", [
    "claude-sonnet-5-5-high", "claude-opus-5-5-medium", "gemini-4-argon-high",
    "gemini-4-argon-medium", "gemini-3.8-flash-high", "gpt-oss-120b-medium",
])
def test_observed_cli_models_can_be_assigned_to_the_correct_engine(model):
    assert profiles.model_engine(model) == "antigravity"
    worker = WorkerIn(title="task", prompt="work", engine="antigravity", model=model)
    assert worker.model == model
    assert model in get_settings().engine_models["antigravity"]


@pytest.mark.parametrize("model,effort", [
    ("gemini-3.8-flash-made-up", None),
    ("gemini-4-argon-max", "max"),
    ("claude-sonnet-5-5-high", "low"),
    ("gpt-oss-120b-medium", "high"),
])
def test_rejects_unavailable_ids_and_conflicting_thinking_levels(model, effort):
    with pytest.raises(ValidationError):
        WorkerIn(title="task", prompt="work", engine="antigravity", model=model, effort=effort)


def cache_quota(account_id, *, third_party=0, gemini=80, argon=70, age=0):
    now = time.time()
    windows = [
        usage.Window("agy_claude_and_gpt_models_5h", "third party", round(100-third_party), int(now+300), third_party),
        usage.Window("agy_claude_and_gpt_models_weekly", "third party weekly", 0, int(now+600), 100),
        usage.Window("agy_gemini_models_5h", "gemini", round(100-gemini), int(now+400), gemini),
        usage.Window("agy_gemini_4_argon_weekly", "argon", round(100-argon), int(now+500), argon),
    ]
    usage._cache[account_id] = (now-age, usage.Usage(windows=windows, updated_at=now-age,
                                                 source="antigravity_cli", credits_balance=0))


def test_exhausted_claude_pool_does_not_block_flash_or_argon():
    aid = uuid.uuid4()
    cache_quota(aid)
    try:
        claude = profiles.Profile(aid, "agy", "antigravity", model="claude-sonnet-5-5-high")
        flash = profiles.Profile(aid, "agy", "antigravity", model="gemini-3.8-flash-high")
        argon = profiles.Profile(aid, "agy", "antigravity", model="gemini-4-argon-high")
        assert profiles.profile_limited_until(claude) > time.time()
        assert profiles.profile_limited_until(flash) is None
        assert profiles.profile_limited_until(argon) is None
    finally:
        usage._cache.pop(aid, None)


def test_argon_pool_is_separate_from_flash_for_paid_pro_quota():
    aid = uuid.uuid4()
    cache_quota(aid, third_party=80, gemini=0, argon=70)
    try:
        assert profiles.profile_limited_until(profiles.Profile(aid, "agy", "antigravity", model="gemini-4-argon-high")) is None
        assert profiles.profile_limited_until(profiles.Profile(aid, "agy", "antigravity", model="gemini-3.8-flash-high"))
    finally:
        usage._cache.pop(aid, None)


def test_stale_exhaustion_still_blocks_but_a_fraction_remaining_is_usable():
    aid = uuid.uuid4()
    p = profiles.Profile(aid, "agy", "antigravity", model="claude-sonnet-5-5-high")
    cache_quota(aid, age=3600)
    try:
        assert profiles.profile_limited_until(p)
        cache_quota(aid, third_party=.1)
        assert profiles.profile_limited_until(p) is None
    finally:
        usage._cache.pop(aid, None)


def test_runtime_quota_cooldown_is_shared_by_claude_and_gpt_only(monkeypatch):
    aid = uuid.uuid4()
    sonnet = profiles.Profile(aid, "agy", "antigravity", model="claude-sonnet-5-5-high")
    opus = profiles.Profile(aid, "agy", "antigravity", model="claude-opus-5-5-medium")
    gpt = profiles.Profile(aid, "agy", "antigravity", model="gpt-oss-120b-medium")
    flash = profiles.Profile(aid, "agy", "antigravity", model="gemini-3.8-flash-high")
    monkeypatch.setattr(profiles, "_persist", lambda: None)
    profiles.cool_down(sonnet.limit_key, 60)
    try:
        assert sonnet.limit_key == opus.limit_key == gpt.limit_key
        assert profiles.profile_limited_until(opus)
        assert profiles.profile_limited_until(flash) is None
    finally:
        profiles.reset_cooldowns()


async def test_availability_uses_requested_model_pool(client, auth):
    from ttakkari.db import sessionmaker
    result = await client.post("/api/accounts", json={"name": "agy", "engine": "antigravity"}, headers=auth)
    aid = uuid.UUID(result.json()["id"])
    cache_quota(aid)
    try:
        async with sessionmaker()() as db:
            free, blocked = await profiles.availability(db, "antigravity", model="claude-sonnet-5-5-high")
            assert free == [] and blocked[0][0].model == "claude-sonnet-5-5-high"
            free, blocked = await profiles.availability(db, "antigravity", model="gemini-4-argon-high")
            assert len(free) == 1 and not blocked
    finally:
        usage._cache.pop(aid, None)


async def test_latest_policy_follows_role_instructions_without_discarding_them(monkeypatch):
    from importlib import import_module
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ttakkari.models import SessionKind
    m = import_module("ttakkari.runs.manager")
    monkeypatch.setattr(m.instructions_mod, "read", lambda role: "LEGACY_FLASH_ONLY_" + role)
    monkeypatch.setattr(m, "pinned_brief", AsyncMock(return_value=""))
    note = await m._system_note(None, Path("/tmp/outbox"), SimpleNamespace(kind=SessionKind.orchestrator, parent_id="desk"))
    assert "LEGACY_FLASH_ONLY_lead" in note
    assert note.rfind("Antigravity routing policy (2026-10-16)") > note.index("LEGACY_FLASH_ONLY_lead")
    assert "claude-sonnet-5-5-high" in note and "gemini-4-argon-high" in note


@pytest.mark.parametrize("model,level,reason", [
    ("claude-sonnet-5-5-medium", "medium", None),
    ("gemini-4-argon-high", "high", "Compare difficult architecture alternatives"),
    ("claude-opus-5-5-medium", "medium", "Resolve a difficult design decision"),
    (None, "high", None),
    ("gemini-4-argon-high", "high", None),
    ("claude-opus-5-5-medium", "medium", None),
])
async def test_worker_api_launches_the_selected_cli_model_and_level(client, auth, make_dir, monkeypatch, model, level, reason):
    from types import SimpleNamespace

    from helpers import wait_run

    from ttakkari.agents import antigravity
    from ttakkari.orchestrator.runtime import issue_token

    root = make_dir()
    cli = root / "fake_agy.py"
    cli.write_text(textwrap.dedent('''\
        #!/usr/bin/env python3
        import json, sys
        args=sys.argv
        output={"model":args[args.index('--model')+1], "effort":args[args.index('--effort')+1] if '--effort' in args else None}
        print(json.dumps({"event":"init", "conversation_id":"fixture-model-selection"}), flush=True)
        print(json.dumps({"event":"result", "result":{"status":"SUCCESS", "response":json.dumps(output)}}), flush=True)
    '''))
    cli.chmod(0o700)
    monkeypatch.setattr(antigravity, "get_settings", lambda: SimpleNamespace(antigravity_bin=str(cli), run_timeout_seconds=10))
    account = await client.post("/api/accounts", json={"name":"agy", "engine":"antigravity"}, headers=auth)
    assert account.status_code == 201
    ws = (await client.post("/api/workspaces", json={"name":root.name,"root_path":str(root)}, headers=auth)).json()
    orch = (await client.post("/api/sessions", json={"workspace_id":ws['id'],"kind":"orchestrator"}, headers=auth)).json()
    body = {"title":"model selection", "prompt":"fixture", "engine":"antigravity"}
    if model:
        body['model'] = model
    if reason:
        body['escalation_reason'] = reason
    response = await client.post("/api/orchestrator/workers", json=body,
                                 headers={"Authorization":f"Bearer {issue_token(uuid.UUID(orch['id']))}"})
    if model and model.startswith(("gemini-4-argon-", "claude-opus-")) and not reason:
        assert response.status_code == 400 and "escalation_reason" in response.text
        return
    assert response.status_code == 201, response.text
    runs = (await client.get(f"/api/sessions/{response.json()['worker_id']}/runs", headers=auth)).json()
    run = await wait_run(client, auth, runs[0]['id'])
    assert run['status'] == 'succeeded', run
    assert run['effort'] is None
    assert json.loads(run['result_text']) == {'model':model or 'claude-sonnet-5-5-high', 'effort':None}


def test_auto_accepts_an_antigravity_model_for_task_aware_routing():
    worker = WorkerIn(title="task", prompt="work", model="claude-sonnet-5-5-high", engine="auto")
    assert worker.model == "claude-sonnet-5-5-high"


def test_default_sonnet_high_cannot_be_combined_with_a_conflicting_effort():
    with pytest.raises(ValidationError, match="thinking level"):
        WorkerIn(title="task", prompt="work", engine="antigravity", effort="low")


async def test_group_cooldown_survives_reload_and_account_reset_clears_it():
    from ttakkari.db import sessionmaker

    aid = uuid.uuid4()
    sonnet = profiles.Profile(aid, "agy", "antigravity", model="claude-sonnet-5-5-high")
    flash = profiles.Profile(aid, "agy", "antigravity", model="gemini-3.8-flash-high")
    profiles.cool_down(sonnet.limit_key, 600)
    await profiles.flush_persist()
    profiles.reset_cooldowns()
    async with sessionmaker()() as db:
        assert await profiles.load_cooldowns(db) == 1
    assert profiles.profile_limited_until(sonnet)
    assert profiles.profile_limited_until(flash) is None
    profiles.clear_cooldown(aid)
    assert profiles.profile_limited_until(sonnet) is None
    await profiles.flush_persist()


def test_missing_argon_quota_is_unknown_and_does_not_inherit_flash_exhaustion():
    aid = uuid.uuid4()
    now = time.time()
    usage._cache[aid] = (now, usage.Usage(windows=[usage.Window("agy_gemini_models_weekly", "Gemini", 100, int(now+900), 0)],
                                        updated_at=now, source="antigravity_cli", credits_balance=0))
    try:
        assert profiles.usage_limit_until(profiles.Profile(aid, "agy", "antigravity", model="gemini-4-argon-high")) is None
    finally:
        usage._cache.pop(aid, None)
