"""Synthetic engines exercise the production meeting account chain and quota handling."""
import time
import uuid
from dataclasses import replace
from importlib import import_module

import pytest
from conftest import agent_calls
from sqlalchemy import select

from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.db import sessionmaker
from ttakkari.meetings import routing
from ttakkari.models import ChatSession, Run, RunEvent
from ttakkari.runs import usage

MODELS = [model for _, model in routing.STAGES]


async def setup_accounts(client, auth):
    # Seeded native personal accounts plus Antigravity; no company account is required.
    accounts = (await client.get("/api/accounts", headers=auth)).json()
    agy = (await client.post("/api/accounts", json={"name": "agy", "engine": "antigravity"}, headers=auth)).json()
    return accounts + [agy]


@pytest.mark.parametrize("limited_count", [0, 1, 2, 3])
async def test_meeting_uses_ordered_models_and_falls_back_on_runtime_limit(
    limited_count, client, auth, monkeypatch,
):
    from test_meetings import TRANSCRIPT, _analyze, _create

    await setup_accounts(client, auth)
    class SyntheticAdapter(ClaudeAdapter):
        def build_command(self, spec, env):
            if spec.model in MODELS[:limited_count]:
                spec = replace(spec, prompt=spec.prompt + "\nCLAUDE_LIMIT")
            return super().build_command(spec, env)

    manager_module = import_module("ttakkari.runs.manager")
    for engine in ("codex", "antigravity", "claude"):
        monkeypatch.setitem(manager_module.ADAPTERS, engine, SyntheticAdapter())
    m = await _create(client, auth, scope="work", transcript=TRANSCRIPT)
    m = await _analyze(client, auth, m["id"])
    assert m["status"] == "review", m.get("error")
    calls = agent_calls()
    models = [c["argv"][c["argv"].index("--model") + 1] for c in calls]
    assert models == MODELS[:limited_count + 1]
    async with sessionmaker()() as db:
        run = await db.get(Run, uuid.UUID(m["analysis_run_id"]))
        sess = await db.get(ChatSession, uuid.UUID(m["analysis_session_id"]))
        assert run.model == MODELS[limited_count] and run.status == "succeeded"
        assert sess.pool == "work"  # MCP permissions continue to follow meeting data.
        fallback = list(await db.scalars(select(RunEvent).where(
            RunEvent.run_id == run.id, RunEvent.type == "engine.fallback")))
        assert len(fallback) == limited_count
        if limited_count >= 2:
            assert len([key for key in sess.engine_sessions if key.endswith(tuple(MODELS[1:3]))]) == 2


@pytest.mark.parametrize("window", ["five_hour", "seven_day"])
async def test_native_quota_cap_skips_luna_and_keeps_flash_before_more_headroom(
    window, client, auth,
):
    accounts = await setup_accounts(client, auth)
    now = time.time()
    codex = next(a for a in accounts if a["engine"] == "codex")
    aid = uuid.UUID(codex["id"])
    usage.seed(aid, usage.Usage(windows=[usage.Window(window, window, 100, int(now + 1000))], updated_at=now))
    async with sessionmaker()() as db:
        ready, waiting = await routing.availability(db)
    assert [p.model for p in ready] == MODELS[1:]
    assert waiting[0][0].model == MODELS[0]
    assert {p.model for p in routing.first_stage(ready)} == {MODELS[1]}


@pytest.mark.parametrize("empty_group,expected", [
    ("agy_gemini_models_weekly", [MODELS[0], MODELS[2], MODELS[3]]),
    ("agy_claude_and_gpt_models_5h", [MODELS[0], MODELS[1], MODELS[3]]),
])
async def test_antigravity_quota_groups_are_independent_even_with_zero_credits(
    empty_group, expected, client, auth,
):
    accounts = await setup_accounts(client, auth)
    aid = uuid.UUID(accounts[-1]["id"])
    now = time.time()
    usage.seed(aid, usage.Usage(windows=[usage.Window(empty_group, empty_group, 100, int(now + 1000), 0)],
                               updated_at=now, source="antigravity_cli", credits_balance=0))
    async with sessionmaker()() as db:
        ready, waiting = await routing.availability(db)
    assert [p.model for p in ready] == expected
    assert len(waiting) == 1


async def test_all_capped_models_wait_for_reset_instead_of_using_company_accounts(client, auth, monkeypatch):
    from test_meetings import TRANSCRIPT, _create
    accounts = await setup_accounts(client, auth)
    now = time.time()
    for account in accounts:
        aid = uuid.UUID(account["id"])
        for_model = ["agy_gemini_models_5h", "agy_claude_and_gpt_models_weekly"] if account["engine"] == "antigravity" else ["seven_day"]
        usage.seed(aid, usage.Usage(windows=[usage.Window(w, w, 100, int(now + 1000), 0) for w in for_model], updated_at=now))
    m = await _create(client, auth, scope="work", transcript=TRANSCRIPT)
    r = await client.post(f"/api/meetings/{m['id']}/analyze", headers=auth)
    assert r.status_code == 202
    import asyncio
    for _ in range(100):
        async with sessionmaker()() as db:
            current = (await client.get(f"/api/meetings/{m['id']}", headers=auth)).json()
            run = await db.get(Run, uuid.UUID(current["analysis_run_id"]))
            event = await db.scalar(select(RunEvent).where(RunEvent.run_id == run.id, RunEvent.type == "run.waiting"))
            if event:
                assert run.status == "queued" and not agent_calls()
                break
        await asyncio.sleep(.05)
    else:
        pytest.fail("all-capped meeting never entered quota wait")
    r = await client.post(f"/api/runs/{run.id}/cancel", headers=auth)
    assert r.status_code == 200
