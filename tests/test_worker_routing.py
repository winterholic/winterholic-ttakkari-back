"""Task fit, quota isolation, runtime fallbacks and safe worker continuation."""
import json
import time
import uuid
from dataclasses import replace
from importlib import import_module

import pytest
from conftest import agent_calls
from helpers import run_events, wait_run
from test_orchestrator import _orch, _session

from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles, usage

SONNET = "claude-sonnet-5-5-high"
FLASH = "gemini-3.8-flash-high"
ARGON = "gemini-4-argon-high"


def quota(aid, *, claude=80, gemini=90, argon=70, age=0):
    now = time.time()
    windows = [usage.Window(f"{group}_{span}", group, round(100-left), int(now+1000), left)
               for group, left in [("agy_claude_and_gpt_models", claude), ("agy_gemini_models", gemini),
                                   ("agy_gemini_4_argon", argon)] for span in ("5h", "weekly")]
    usage.seed(aid, usage.Usage(windows=windows, updated_at=now-age, source="antigravity_cli", credits_balance=0), refresh=True)


def test_model_quota_headroom_preserves_fractions_and_shared_group():
    aid = uuid.uuid4()
    quota(aid, claude=12.25, gemini=89.75)
    assert profiles._usage_headroom(profiles.Profile(aid, "agy", "antigravity", model=SONNET))[0] == 12.25
    assert profiles._usage_headroom(profiles.Profile(aid, "agy", "antigravity", model=FLASH))[0] == 89.75


@pytest.fixture
async def accounts(client, auth):
    await client.get("/api/accounts", headers=auth)
    agy = (await client.post("/api/accounts", json={"name":"agy", "engine":"antigravity"}, headers=auth)).json()
    quota(uuid.UUID(agy["id"]))
    rows = (await client.get("/api/accounts", headers=auth)).json()
    for a in rows:
        if a["engine"] != "antigravity":
            usage.seed(uuid.UUID(a["id"]), usage.Usage(windows=[
                usage.Window("five_hour", "5h", 40, int(time.time()+1000))], updated_at=time.time()))
    return rows


@pytest.fixture
def synthetic(monkeypatch):
    class SyntheticAdapter(ClaudeAdapter):
        def parse_line(self, obj, state, cwd):
            # The shared CLI fixture emits hard-coded expired windows. These tests supply
            # current per-model quota snapshots; do not replace them with that unrelated fixture.
            if obj.get("type") == "rate_limit_event" and obj.get("rate_limit_info", {}).get("status") == "allowed":
                return []
            return super().parse_line(obj, state, cwd)

        def build_command(self, spec, env):
            limit_model = env.get("WORKER_TEST_LIMIT_MODEL")
            if limit_model == spec.model:
                spec = replace(spec, prompt=spec.prompt + "\nCLAUDE_LIMIT")
            return super().build_command(spec, env)
    m = import_module("ttakkari.runs.manager")
    for engine in ("claude", "codex", "antigravity"):
        monkeypatch.setitem(m.ADAPTERS, engine, SyntheticAdapter())


async def spawn(client, auth, make_dir, **body):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    headers = _orch(issue_token(orch["id"]))
    response = await client.post("/api/orchestrator/workers", json={"title":"routing fixture", "prompt":"fixture", **body}, headers=headers)
    return response, headers


async def result(client, auth, response):
    assert response.status_code == 201, response.text
    runs = (await client.get(f"/api/sessions/{response.json()['worker_id']}/runs", headers=auth)).json()
    return await wait_run(client, auth, runs[0]["id"])


@pytest.mark.parametrize("task_type,expected_engine,expected_model", [
    ("general", "antigravity", SONNET), ("routine", "antigravity", FLASH),
    ("design", "antigravity", SONNET), ("verification", "codex", "gpt-6-luna"),
    ("reasoning", "antigravity", ARGON),
])
async def test_auto_worker_uses_task_policy_and_reports_actual_model(task_type, expected_engine, expected_model,
                                                                    client, auth, make_dir, accounts, synthetic):
    response, h = await spawn(client, auth, make_dir, task_type=task_type,
                              **({"escalation_reason":"Compare difficult architecture alternatives"} if task_type=="reasoning" else {}))
    run = await result(client, auth, response)
    assert run["status"] == "succeeded", run
    assert (run["engine"], run["model"]) == (expected_engine, expected_model)
    wid = response.json()["worker_id"]
    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert detail["task_type"] == task_type
    assert detail["routing"]["selected"]["model"] == expected_model
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(wid))
        assert sess.worker_task_type == task_type and sess.engine == "auto"


async def test_default_auto_worker_uses_antigravity_for_normal_work(client, auth, make_dir, accounts, synthetic):
    response, _ = await spawn(client, auth, make_dir)
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", SONNET)


@pytest.mark.parametrize("task_type,empty,expected", [
    ("general", "claude", FLASH), ("routine", "gemini", "gpt-6-luna"),
    ("reasoning", "argon", "claude-opus-5-5-medium"), ("design", "claude", "sonnet"),
])
async def test_exhausted_model_pool_uses_suitable_fallback(task_type, empty, expected,
                                                         client, auth, make_dir, accounts, synthetic):
    aid = uuid.UUID(next(a["id"] for a in accounts if a["engine"]=="antigravity"))
    quota(aid, **{empty:0})
    response, _ = await spawn(client, auth, make_dir, task_type=task_type,
                              **({"escalation_reason":"Difficult design alternatives"} if task_type=="reasoning" else {}))
    run = await result(client, auth, response)
    assert run["model"] == expected and run["status"] == "succeeded", run
    ev = await run_events(client, auth, run["id"])
    receipt = next(e["payload"] for e in ev if e["type"]=="worker.routing")
    assert receipt["blocked"] and receipt["selected"]["model"] == expected


async def test_runtime_limit_switches_groups_on_same_account_and_keeps_conversations_separate(
    client, auth, make_dir, accounts, synthetic, monkeypatch,
):
    monkeypatch.setenv("WORKER_TEST_LIMIT_MODEL", SONNET)
    response, h = await spawn(client, auth, make_dir)
    run = await result(client, auth, response)
    assert run["model"] == FLASH and run["status"] == "succeeded", run
    ev = await run_events(client, auth, run["id"])
    selected = [e["payload"]["selected"]["model"] for e in ev if e["type"]=="worker.routing"]
    assert selected == [SONNET, FLASH]
    calls = [c for c in agent_calls() if "--model" in c["argv"] and c["argv"][c["argv"].index("--model")+1] in (SONNET, FLASH)]
    assert len(calls)==2 and "--resume" not in calls[1]["argv"]
    wid = response.json()["worker_id"]
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(wid))
        assert len(sess.engine_sessions)==2
    follow = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt":"continue fixture"}, headers=h)
    assert follow.status_code == 202
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    second = await wait_run(client, auth, runs[-1]["id"])
    assert second["model"] == FLASH and second["status"] == "succeeded"
    flash_calls = [c for c in agent_calls() if "--model" in c["argv"] and c["argv"][c["argv"].index("--model")+1] == FLASH]
    assert "--resume" in flash_calls[-1]["argv"]


async def test_explicit_engine_remains_locked(client, auth, make_dir, accounts, synthetic):
    response, _ = await spawn(client, auth, make_dir, engine="codex", task_type="general")
    run = await result(client, auth, response)
    assert run["engine"] == "codex" and run["model"] == "gpt-6-luna"


async def test_reasoning_needs_justification_even_without_an_explicit_model(client, auth, make_dir):
    response, _ = await spawn(client, auth, make_dir, task_type="reasoning")
    assert response.status_code == 400 and "escalation_reason" in response.text


async def test_invalid_task_type_is_rejected(client, auth, make_dir):
    response, _ = await spawn(client, auth, make_dir, task_type="made-up")
    assert response.status_code == 422


async def test_company_worker_does_not_borrow_personal_antigravity(client, auth, make_dir, accounts, synthetic):
    codex = next(a for a in accounts if a["engine"]=="codex")
    response = await client.patch(f"/api/accounts/{codex['id']}", json={"pool":"work", "work_enabled":True}, headers=auth)
    assert response.status_code == 200, response.text
    orch = await _session(client, auth, make_dir, kind="orchestrator", pool="work")
    h = _orch(issue_token(orch["id"]))
    response = await client.post("/api/orchestrator/workers", json={"title":"work", "prompt":"fixture"}, headers=h)
    run = await result(client, auth, response)
    assert run["engine"] == "codex", run


async def test_account_usage_publishes_one_task_policy_and_per_model_headroom(client, auth, make_dir, accounts):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    body = (await client.get("/api/orchestrator/accounts/usage", headers=h)).json()
    assert body["worker_routing"]["general"][0] == [{"engine":"antigravity", "model":SONNET}]
    assert "native auto" not in body["note"]
    agy = next(a for a in body["accounts"] if a["engine"]=="antigravity")
    assert agy["model_capacity"][SONNET]["remaining_to_limit_percent"] == 80
    assert agy["model_capacity"][FLASH]["remaining_to_limit_percent"] == 90


def test_missing_stale_and_reset_model_quota_stay_unknown():
    aid = uuid.uuid4()
    p = profiles.Profile(aid, "agy", "antigravity", model=SONNET)
    assert profiles._usage_headroom(p) is None
    quota(aid, age=3600)
    assert profiles._usage_headroom(p) is None
    now = time.time()
    usage._cache[aid] = (now, usage.Usage(windows=[usage.Window("agy_claude_and_gpt_models_5h", "pool", 0, int(now-1), 100)], updated_at=now))
    assert profiles._usage_headroom(p) is None
    usage._cache[aid] = (now, usage.Usage(windows=[usage.Window("agy_gemini_models_weekly", "other pool", 0, int(now+100), 100)], updated_at=now))
    assert profiles._usage_headroom(p) is None


def test_stale_exhausted_pool_blocks_but_does_not_hide_other_pool():
    aid = uuid.uuid4()
    quota(aid, claude=0, age=3600)
    assert profiles.profile_limited_until(profiles.Profile(aid, "agy", "antigravity", model=SONNET))
    assert profiles.profile_limited_until(profiles.Profile(aid, "agy", "antigravity", model=FLASH)) is None


def test_known_capacity_precedes_unknown_but_not_task_eligibility():
    from ttakkari.runs import worker_routing as routing
    aid = uuid.uuid4()
    agy = profiles.Profile(aid, "agy", "antigravity", model=SONNET)
    native = profiles.Profile(uuid.uuid4(), "native", "codex", model="gpt-6-luna")
    usage.seed(native.id, usage.Usage(windows=[usage.Window("five_hour", "5h", 50, int(time.time()+100))], updated_at=time.time()))
    assert routing.first_stage([agy, native], "general") == [native]
    quota(aid)
    assert routing.first_stage([agy, native], "general") == [agy]
    assert routing.first_stage([agy, native], "verification") == [native]


async def test_native_fallback_balances_by_per_run_headroom(client, auth, make_dir, accounts, synthetic):
    for a in accounts:
        if a["engine"] == "antigravity":
            quota(uuid.UUID(a["id"]), claude=0, gemini=0)
        else:
            usage.seed(uuid.UUID(a["id"]), usage.Usage(windows=[usage.Window("five_hour", "5h", 10 if a["engine"]=="codex" else 80,
                       int(time.time()+1000))], updated_at=time.time()), refresh=True)
    response, _ = await spawn(client, auth, make_dir)
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("codex", "gpt-6-luna")


async def test_all_eligible_models_capped_wait_without_engine_launch(client, auth, make_dir, accounts, synthetic):
    from helpers import wait_event
    for a in accounts:
        if a["engine"] == "antigravity":
            quota(uuid.UUID(a["id"]), claude=0, gemini=0)
        else:
            usage.seed(uuid.UUID(a["id"]), usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(time.time()+1000))],
                                                     updated_at=time.time()), refresh=True)
    response, _ = await spawn(client, auth, make_dir)
    assert response.status_code == 201, response.text
    runs = (await client.get(f"/api/sessions/{response.json()['worker_id']}/runs", headers=auth)).json()
    waiting = await wait_event(client, auth, runs[0]["id"], "run.waiting")
    assert waiting["payload"] and not agent_calls()
    await client.post(f"/api/runs/{runs[0]['id']}/cancel", headers=auth)


async def test_design_never_falls_back_to_gpt_or_flash(client, auth, make_dir, accounts):
    from ttakkari.runs import worker_routing as routing
    agy = next(a for a in accounts if a["engine"]=="antigravity")
    quota(uuid.UUID(agy["id"]), claude=0)
    async with sessionmaker()() as db:
        ready, _ = await routing.availability(db, "design", "personal")
    assert ready and {(p.engine, p.model) for p in ready} == {("claude", "sonnet")}


async def test_unsupported_configured_model_is_not_launched(client, auth, accounts, monkeypatch):
    from ttakkari.config import get_settings
    from ttakkari.runs import worker_routing as routing
    monkeypatch.setattr(get_settings(), "engine_models", {"claude":["sonnet"], "codex":["gpt-6-luna"], "antigravity":[FLASH]})
    async with sessionmaker()() as db:
        ready, _ = await routing.availability(db, "general", "personal")
    assert all(p.model != SONNET for p in ready)
    assert routing.first_stage(ready, "general")[0].model == FLASH


async def test_idle_legacy_worker_can_reroute_without_losing_its_conversation(client, auth, make_dir, accounts, synthetic):
    response, h = await spawn(client, auth, make_dir, engine="codex", model="gpt-6-luna")
    first = await result(client, auth, response)
    wid = response.json()["worker_id"]
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(wid))
        prior = dict(sess.engine_sessions)
    native_account = next(a for a in accounts if a["engine"]=="codex")
    native_id = uuid.UUID(native_account["id"])
    cached = usage.cached(native_id)
    assert profiles._usage_headroom(profiles.Profile(native_id, "codex", "codex", model="gpt-6-luna")) is not None, repr(cached)
    # Keep Flash blocked so routine continues the exact native conversation it used previously.
    quota(uuid.UUID(next(a["id"] for a in accounts if a["engine"]=="antigravity")), gemini=0)
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt":"continue fixture", "reroute":True, "task_type":"routine"}, headers=h)
    assert r.status_code == 202, r.text
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    second = await wait_run(client, auth, runs[-1]["id"])
    assert second["status"] == "succeeded" and second["model"] == "gpt-6-luna", json.dumps([e for e in await run_events(client, auth, second["id"]) if e["type"]=="worker.routing"])
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(wid))
        assert sess.engine == "auto" and sess.worker_task_type == "routine"
        assert all(sess.engine_sessions[k] == v for k,v in prior.items())
    native_calls = [c for c in agent_calls() if "--model" in c["argv"] and c["argv"][c["argv"].index("--model")+1] == "gpt-6-luna"]
    assert "--resume" in native_calls[-1]["argv"]
    assert first["status"] == "succeeded"


async def test_active_reroute_is_rejected_without_reset_or_duplicate_run(client, auth, make_dir, accounts, synthetic):
    response, h = await spawn(client, auth, make_dir, engine="codex", prompt="SLEEP")
    wid = response.json()["worker_id"]
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt":"change", "reroute":True, "fresh_context":True}, headers=h)
    assert r.status_code == 409, r.text
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(wid))
        assert sess.engine == "codex" and "_reset_at" not in sess.engine_sessions
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    assert len(runs)==1
    await client.post(f"/api/runs/{runs[0]['id']}/cancel", headers=auth)


async def test_reroute_cannot_escalate_without_new_reason(client, auth, make_dir, accounts, synthetic):
    response, h = await spawn(client, auth, make_dir)
    await result(client, auth, response)
    wid = response.json()["worker_id"]
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt":"change", "reroute":True, "task_type":"reasoning"}, headers=h)
    assert r.status_code == 400 and "escalation_reason" in r.text


async def test_non_quota_error_does_not_replay_work_on_another_engine(client, auth, make_dir, accounts, synthetic):
    response, _ = await spawn(client, auth, make_dir, prompt="FAIL")
    run = await result(client, auth, response)
    assert run["status"] == "failed"
    ev = await run_events(client, auth, run["id"])
    assert not any(e["type"]=="engine.fallback" for e in ev)


async def test_incompatible_auto_model_preference_is_rejected_instead_of_ignored(client, auth, make_dir):
    response, _ = await spawn(client, auth, make_dir, task_type="design", model=FLASH)
    assert response.status_code == 400 and "task_type" in response.text


def test_reasoning_fallback_uses_native_peer_effort_instead_of_carrying_argon_level():
    from ttakkari.runs import worker_routing as routing
    native = profiles.Profile(uuid.uuid4(), "native", "codex", model="gpt-6.1-sol")
    assert routing.effort(native, "medium") == "high"
    native = replace(native, engine="claude", model="opus")
    assert routing.effort(native, "high") == "medium"


async def test_generation_backend_runs_task_policy_and_group_fallback_with_owned_lease(
    client, auth, make_dir, accounts, synthetic, monkeypatch, tmp_path,
):
    import asyncio

    from test_generations import generations

    from ttakkari import generation
    from ttakkari.generation import activate
    from ttakkari.runs.manager import manager

    async with generations(tmp_path) as (a, _b):
        await activate(a.id, expected=None)
        monkeypatch.setattr(generation, "current", a)
        monkeypatch.setattr(manager, "_runtime", a)
        monkeypatch.setenv("WORKER_TEST_LIMIT_MODEL", SONNET)
        response, _h = await spawn(client, auth, make_dir)
        run = await result(client, auth, response)
        assert run["status"] == "succeeded" and run["model"] == FLASH, run
        ev = await run_events(client, auth, run["id"])
        receipts = [e["payload"] for e in ev if e["type"] == "worker.routing"]
        assert [r["selected"]["model"] for r in receipts] == [SONNET, FLASH]
        assert receipts[1]["selected"]["headroom_per_run_percent"] == 90  # exclude own lease
        while manager._handles:
            await asyncio.gather(*(v.task for v in list(manager._handles.values())), return_exceptions=True)


async def test_resuming_sonnet_receives_flash_progress_from_the_same_account(client, auth, make_dir, accounts, synthetic):
    response, h = await spawn(client, auth, make_dir, prompt="sonnet-anchor")
    await result(client, auth, response)
    wid = response.json()["worker_id"]
    aid = uuid.UUID(next(a["id"] for a in accounts if a["engine"]=="antigravity"))
    quota(aid, claude=0)
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt":"flash-progress-marker"}, headers=h)
    assert r.status_code == 202
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    flash = await wait_run(client, auth, runs[-1]["id"])
    assert flash["model"] == FLASH
    quota(aid, claude=80)
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt":"return-to-sonnet"}, headers=h)
    assert r.status_code == 202
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    third = await wait_run(client, auth, runs[-1]["id"])
    assert third["model"] == SONNET and third["status"] == "succeeded"
    calls = [c for c in agent_calls() if "--model" in c["argv"] and c["argv"][c["argv"].index("--model")+1] == SONNET]
    argv = calls[-1]["argv"]
    assert "--resume" in argv
    assert "flash-progress-marker" in argv[argv.index("-p")+1]


async def test_legacy_luna_preference_does_not_label_normal_work_as_verification(client, auth, make_dir, accounts, synthetic):
    response, _ = await spawn(client, auth, make_dir, model="gpt-6-luna")
    run = await result(client, auth, response)
    assert run["engine"] == "antigravity" and run["model"] == SONNET
    assert response.json()["task_type"] == "general"


async def test_missing_antigravity_binary_is_not_retried_for_each_model(client, auth, make_dir, accounts, synthetic, monkeypatch):
    attempted = []
    class MissingAdapter(ClaudeAdapter):
        def build_command(self, spec, env):
            attempted.append(spec.model)
            return ["/nonexistent/ttakkari-routing-fixture-agy"]
    monkeypatch.setitem(import_module("ttakkari.runs.manager").ADAPTERS, "antigravity", MissingAdapter())
    response, _ = await spawn(client, auth, make_dir)
    run = await result(client, auth, response)
    assert run["status"] == "succeeded" and run["engine"] in {"claude", "codex"}, run
    assert attempted == [SONNET]


async def test_reroute_maps_legacy_cross_engine_model_to_its_actual_counterpart(client, auth, make_dir, accounts, synthetic):
    from ttakkari.models import Run

    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    claude = next(a for a in accounts if a["engine"]=="claude")
    quota(uuid.UUID(next(a["id"] for a in accounts if a["engine"]=="antigravity")), claude=0)
    async with sessionmaker()() as db:
        legacy = ChatSession(workspace_id=uuid.UUID(orch["workspace_id"]), title="legacy auto counterpart", kind="worker",
                             parent_id=uuid.UUID(orch["id"]), engine="auto", model="gpt-6-luna", effort="high",
                             engine_sessions={claude["id"]:"legacy-sonnet-conversation"})
        db.add(legacy)
        await db.flush()
        db.add(Run(workspace_id=legacy.workspace_id, session_id=legacy.id, prompt="legacy fixture", engine="claude",
                   model="gpt-6-luna", profile=claude["name"], status="succeeded", result_text="previous progress"))
        await db.commit()
        wid = str(legacy.id)
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", json={"prompt":"continue legacy", "reroute":True, "task_type":"design"}, headers=h)
    assert r.status_code == 202, r.text
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    run = await wait_run(client, auth, runs[-1]["id"])
    assert run["status"] == "succeeded" and run["model"] == "sonnet", run
    calls = [c for c in agent_calls() if "--model" in c["argv"] and c["argv"][c["argv"].index("--model")+1] == "sonnet"]
    assert "--resume" in calls[-1]["argv"]
    assert calls[-1]["argv"][calls[-1]["argv"].index("--resume")+1] == "legacy-sonnet-conversation"
