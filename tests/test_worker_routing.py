"""Task fit, quota isolation, runtime fallbacks and safe worker continuation."""
import json
import time
import uuid
from dataclasses import replace
from datetime import timedelta
from importlib import import_module

import pytest
from conftest import agent_calls
from helpers import run_events, wait_run
from test_orchestrator import _orch, _session

from ttakkari.agents.claude import ClaudeAdapter
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunEvent, RuntimeLease, utcnow
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
    if expected_engine == "antigravity":
        assert run["effort"] is None
    wid = response.json()["worker_id"]
    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert detail["task_type"] == task_type
    assert detail["routing"]["selected"]["model"] == expected_model
    if task_type == "general":
        assert detail["routing"]["scope"]["account_scope"] == "personal"
        assert run["usage"]["routing_request"]["engine"] == "auto"
        assert run["usage"]["routing_request"]["account_scope"] == "personal"
    async with sessionmaker()() as db:
        sess = await db.get(ChatSession, uuid.UUID(wid))
        assert sess.worker_task_type == task_type and sess.engine == "auto"


async def test_default_auto_worker_uses_antigravity_for_normal_work(client, auth, make_dir, accounts, synthetic):
    response, _ = await spawn(client, auth, make_dir)
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", SONNET)
    assert run["effort"] is None
    agy_calls = [c for c in agent_calls() if "--model" in c["argv"] and c["argv"][c["argv"].index("--model")+1] == SONNET]
    assert agy_calls and all("--effort" not in c["argv"] for c in agy_calls)


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
    assert all("--effort" not in c["argv"] for c in calls)
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
    assert body["muse_auto_routing"] is True
    assert body["worker_routing"]["general"][0] == [
        {"engine":"antigravity", "model":SONNET},
        {"engine":"muse", "model":"muse-spark-1.3"},
    ]
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


@pytest.mark.parametrize("model", [SONNET, FLASH, "claude-opus-5-5-medium", "gemini-4-argon-high"])
def test_antigravity_model_embedded_thinking_level_is_not_cli_effort(model):
    from ttakkari.runs import worker_routing as routing
    antigravity = profiles.Profile(uuid.uuid4(), "agy", "antigravity", model=model)
    assert routing.effort(antigravity, "high") is None


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
    assert all("--effort" not in c["argv"] for c in calls)
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


async def test_pinned_claude_session_falls_back_to_same_family_antigravity_then_other_engine_on_limit(
    client, auth, make_dir, accounts, synthetic, monkeypatch,
):
    # Stage 1: native Claude hits limit -> switches to Antigravity claude-sonnet-5-5-high (same family).
    monkeypatch.setenv("WORKER_TEST_LIMIT_MODEL", "sonnet")
    response, _ = await spawn(client, auth, make_dir, engine="claude", task_type="general")
    run = await result(client, auth, response)
    assert run["status"] == "succeeded"
    assert (run["engine"], run["model"]) == ("antigravity", SONNET)
    ev = await run_events(client, auth, run["id"])
    fb = next(e["payload"] for e in ev if e["type"] == "engine.fallback")
    assert fb["from_engine"] == "claude" and fb["to_engine"] == "antigravity"
    assert fb["to_model"] == SONNET and fb["fallback_tier"] == "same_family"
    assert run["usage"]["fallback"]["to_model"] == SONNET

    # Stage 2: when Antigravity claude_and_gpt pool is also exhausted, falls back to task-fit Flash / Luna.
    agy_id = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "antigravity"))
    quota(agy_id, claude=0, gemini=85)
    response2, _ = await spawn(client, auth, make_dir, engine="claude", task_type="general")
    run2 = await result(client, auth, response2)
    assert run2["status"] == "succeeded"
    assert (run2["engine"], run2["model"]) == ("antigravity", FLASH)
    ev2 = await run_events(client, auth, run2["id"])
    fb2 = next(e["payload"] for e in ev2 if e["type"] == "engine.fallback")
    assert fb2["from_engine"] == "claude" and fb2["to_model"] == FLASH
    assert fb2["fallback_tier"] == "task_fit"


async def test_pinned_antigravity_pool_exhaustion_uses_another_pool_without_blocking(
    client, auth, make_dir, accounts, synthetic,
):
    claude_id = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "claude"))
    usage.seed(
        claude_id,
        usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(time.time() + 1000))], updated_at=time.time()),
        refresh=True,
    )
    agy_id = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "antigravity"))
    quota(agy_id, claude=0, gemini=90, argon=70)
    response, _ = await spawn(client, auth, make_dir, engine="antigravity", model=SONNET, task_type="general")
    run = await result(client, auth, response)
    assert run["status"] == "succeeded"
    assert (run["engine"], run["model"]) == ("antigravity", FLASH)
    ev = await run_events(client, auth, run["id"])
    fb = next(e["payload"] for e in ev if e["type"] == "engine.fallback")
    assert fb["from_model"] == SONNET and fb["to_model"] == FLASH


async def test_strict_engine_worker_waits_instead_of_switching_routes(
    client, auth, make_dir, accounts, synthetic,
):
    from helpers import wait_event

    claude_id = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "claude"))
    usage.seed(
        claude_id,
        usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(time.time() + 1000))], updated_at=time.time()),
        refresh=True,
    )
    response, _ = await spawn(client, auth, make_dir, engine="claude", strict_engine=True)
    assert response.status_code == 201, response.text
    assert response.json()["strict_engine"] is True
    runs = (await client.get(f"/api/sessions/{response.json()['worker_id']}/runs", headers=auth)).json()
    waiting = await wait_event(client, auth, runs[0]["id"], "run.waiting")
    assert waiting["payload"] and not agent_calls()
    await client.post(f"/api/runs/{runs[0]['id']}/cancel", headers=auth)


async def test_work_scope_pinned_session_fallback_never_mixes_personal_accounts(
    client, auth, make_dir, accounts, synthetic,
):
    claude_work = (
        await client.post(
            "/api/accounts",
            json={"name": "claude-work", "engine": "claude", "pool": "work", "work_enabled": True},
            headers=auth,
        )
    ).json()
    codex = next(a for a in accounts if a["engine"] == "codex")
    await client.patch(f"/api/accounts/{codex['id']}", json={"pool": "work", "work_enabled": True}, headers=auth)
    usage.seed(
        uuid.UUID(claude_work["id"]),
        usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(time.time() + 1000))], updated_at=time.time()),
        refresh=True,
    )
    orch = await _session(client, auth, make_dir, kind="orchestrator", pool="work")
    h = _orch(issue_token(orch["id"]))
    response = await client.post(
        "/api/orchestrator/workers",
        json={"title": "work-pinned", "prompt": "fixture", "engine": "claude"},
        headers=h,
    )
    run = await result(client, auth, response)
    assert run["status"] == "succeeded"
    assert (run["engine"], run["profile"], run["model"]) == ("codex", "codex", "gpt-6-luna")


async def test_verification_excludes_flash_and_routine_supports_low_medium_flash(
    client, auth, make_dir, accounts, synthetic,
):
    from ttakkari.runs import worker_routing as routing

    assert all(
        not m.startswith("gemini-3.8-flash")
        for stage in routing.ROUTES["verification"]
        for _, m in stage
    )
    codex_id = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "codex"))
    usage.seed(
        codex_id,
        usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(time.time() + 1000))], updated_at=time.time()),
        refresh=True,
    )
    agy_id = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "antigravity"))
    quota(agy_id, claude=0, gemini=95)
    response, _ = await spawn(client, auth, make_dir, task_type="verification")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("claude", "sonnet")

    low_resp, _ = await spawn(
        client, auth, make_dir, task_type="routine", model="gemini-3.8-flash-low",
    )
    low_run = await result(client, auth, low_resp)
    assert (low_run["engine"], low_run["model"], low_run["effort"]) == (
        "antigravity", "gemini-3.8-flash-low", None,
    )


async def test_flash_implementation_marks_independent_verification_and_records_false_completion(
    client, auth, make_dir, accounts, synthetic,
):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    resp = await client.post(
        "/api/orchestrator/workers",
        json={"title": "flash-impl", "prompt": "fixture", "task_type": "routine"},
        headers=h,
    )
    run = await result(client, auth, resp)
    assert run["model"] == FLASH
    assert run["usage"]["verification_required"] is True
    assert run["usage"]["verification_status"] == "pending"
    assert run["usage"]["false_completion"] is None

    wid = resp.json()["worker_id"]
    worker_out = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert worker_out["verification_required"] is True
    assert worker_out["verification_status"] == "pending"
    assert worker_out["routing"]["verification_required"] is True
    assert worker_out["routing"]["recommended_verifier"]["excluded_family"] == "gemini-3.8-flash"

    orch_runs = (await client.get(f"/api/sessions/{orch['id']}/runs", headers=auth)).json()
    report_run = next(r for r in orch_runs if "[작업 보고]" in r["prompt"])
    assert "독립 검증 필요" in report_run["prompt"]
    assert f"antigravity/{FLASH}" in report_run["prompt"]

    verify = await client.post(
        f"/api/orchestrator/workers/{wid}/messages",
        json={
            "verification_outcome": "failed",
            "verification_note": "Unit test fixture uncovered incomplete file cleanup",
        },
        headers=h,
    )
    assert verify.status_code == 202, verify.text
    verified = verify.json()
    assert verified["verification_status"] == "failed"
    assert verified["false_completion"] is True

    ev = await run_events(client, auth, run["id"])
    ver_ev = next(e["payload"] for e in ev if e["type"] == "worker.verification")
    assert ver_ev["outcome"] == "failed" and ver_ev["false_completion"] is True

    metrics = (await client.get("/api/orchestrator/workers/metrics", headers=h)).json()
    flash_bucket = next(m for m in metrics["metrics"] if m["model"] == FLASH and m["task_type"] == "routine")
    assert flash_bucket["verification_required_runs"] == 1
    assert flash_bucket["false_completion_runs"] == 1



async def legacy_wait(client, auth, make_dir, accounts, *, last_model=ARGON, strict=False):
    """Persist the shape of a pre-policy quota wait, without launching an agent."""
    from ttakkari.runs import events

    sess = await _session(client, auth, make_dir, engine="antigravity", model=ARGON)
    codex = next(a for a in accounts if a["engine"] == "codex")
    agy = next(a for a in accounts if a["engine"] == "antigravity")
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, uuid.UUID(sess["id"]))
        s.kind, s.strict_engine = "worker", strict
        s.engine_sessions = {agy["id"]: "original-argon-conversation",
                             f"{codex['id']}:gpt-6.1-sol": "saved-sol-conversation"}
        r = Run(session_id=s.id, workspace_id=s.workspace_id, prompt="continue architecture review",
                engine="antigravity" if last_model == ARGON else "codex", model=last_model,
                status="queued", started_at=utcnow()-timedelta(hours=2),
                resume_after=utcnow()+timedelta(days=7), last_seq=0,
                checkpoint_ref="refs/ttakkari/checkpoint/preserved")
        db.add(r)
        await db.flush()
        events.append(db, r, "tool.result", {"output": "commit d76f13c already created; check side effects"})
        events.append(db, r, "run.waiting", {"until": r.resume_after.isoformat(),
                                              "handoff": "PRESERVED PROGRESS: do not recreate d76f13c"})
        await db.commit()
        return r


async def test_legacy_argon_wait_rechecks_routes_before_next_week_and_resumes_once(
    client, auth, make_dir, accounts, synthetic, monkeypatch, tmp_path,
):
    import asyncio

    from test_generations import generations

    from ttakkari import generation
    from ttakkari.generation import activate
    from ttakkari.runs import usage_store
    from ttakkari.runs.manager import manager, resume_queued

    agy = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "antigravity"))
    quota(agy, claude=0, gemini=85, argon=0)  # Flash spare capacity cannot downgrade reasoning.
    await usage_store.flush()
    run = await legacy_wait(client, auth, make_dir, accounts)
    async with generations(tmp_path) as (old, new):
        await activate(old.id, expected=None)
        await activate(new.id, expected=old.id)
        monkeypatch.setattr(generation, "current", new)
        monkeypatch.setattr(manager, "_runtime", new)
        await asyncio.gather(resume_queued(), resume_queued())
        async with sessionmaker()() as db:
            assert (await db.get(Run, run.id)).resume_after is None, "new policy left the legacy weekly timer armed"
        done = await wait_run(client, auth, str(run.id))
        assert (done["engine"], done["model"], done["effort"]) == ("codex", "gpt-6.1-sol", "high")
        calls = agent_calls()
        assert len(calls) == 1
        assert "saved-sol-conversation" in calls[0]["argv"]
        assert "PRESERVED PROGRESS" in " ".join(calls[0]["argv"])
        assert done["checkpoint_ref"] == run.checkpoint_ref
        ev = await run_events(client, auth, str(run.id))
        assert sum(e["type"] == "run.resumed" for e in ev) == 1
        assert sum(e["type"] == "run.finished" for e in ev) == 1
        assert next(e["payload"] for e in ev if e["type"] == "engine.fallback")["from_model"] == ARGON
        while manager._handles:
            await asyncio.gather(*(v.task for v in list(manager._handles.values())), return_exceptions=True)


async def test_resumed_pinned_reasoning_keeps_original_model_after_native_fallback(
    client, auth, make_dir, accounts, synthetic,
):
    from ttakkari.runs.manager import manager

    agy = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "antigravity"))
    quota(agy, claude=75, argon=0)
    run = await legacy_wait(client, auth, make_dir, accounts, last_model="gpt-6.1-sol")
    async with sessionmaker()() as db:
        row = await db.get(Run, run.id)
        row.resume_after = None
        await db.commit()
    await manager.dispatch(run.session_id)
    done = await wait_run(client, auth, str(run.id))
    assert done["model"] == "claude-opus-5-5-medium", "resumed reasoning was downgraded to Sonnet"
    assert "PRESERVED PROGRESS" in " ".join(agent_calls()[0]["argv"])


@pytest.mark.parametrize("excluded", ["strict", "all_capped", "cancelled", "backoff", "owned"])
async def test_wait_reconsideration_preserves_locks_cancellation_and_real_unavailability(
    excluded, client, auth, make_dir, accounts, monkeypatch, tmp_path,
):
    from test_generations import generations

    from ttakkari import generation
    from ttakkari.generation import activate
    from ttakkari.runs.manager import manager, resume_queued

    agy = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "antigravity"))
    quota(agy, claude=0, argon=0)
    if excluded == "all_capped":
        for a in accounts:
            if a["engine"] != "antigravity":
                usage.seed(uuid.UUID(a["id"]), usage.Usage(windows=[
                    usage.Window("five_hour", "5h", 100, int(time.time()+1000))], updated_at=time.time()), refresh=True)
    run = await legacy_wait(client, auth, make_dir, accounts, strict=excluded == "strict")
    async with generations(tmp_path) as (old, new):
        await activate(old.id, expected=None)
        if excluded == "owned":
            async with sessionmaker()() as db:
                row = await db.get(Run, run.id)
                row.resume_after = None
                await db.commit()
            assert await old.claim(run.id, "workers", 2)
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            row.resume_after = run.resume_after
            if excluded == "cancelled":
                row.status = "cancelled"
            if excluded == "backoff":
                row.resume_attempts = 1
                db.add(RunEvent(run_id=row.id, seq=row.last_seq+1, type="run.status",
                                payload={"restart_resume": True}))
                row.last_seq += 1
            await db.commit()
        await activate(new.id, expected=old.id)
        monkeypatch.setattr(generation, "current", new)
        monkeypatch.setattr(manager, "_runtime", new)
        await resume_queued()
        async with sessionmaker()() as db:
            row = await db.get(Run, run.id)
            assert row.resume_after == run.resume_after
            if excluded == "owned":
                assert (await db.get(RuntimeLease, run.id)).generation_id == old.id
        assert not agent_calls()


async def test_legacy_argon_conversation_is_not_resumed_as_opus_on_same_account(
    client, auth, make_dir, accounts, synthetic,
):
    from ttakkari.runs.manager import manager

    agy = uuid.UUID(next(a["id"] for a in accounts if a["engine"] == "antigravity"))
    quota(agy, claude=80, argon=0)
    run = await legacy_wait(client, auth, make_dir, accounts)
    async with sessionmaker()() as db:
        (await db.get(Run, run.id)).resume_after = None
        await db.commit()
    await manager.dispatch(run.session_id)
    done = await wait_run(client, auth, str(run.id))
    assert done["model"] == "claude-opus-5-5-medium"
    assert "original-argon-conversation" not in agent_calls()[0]["argv"]
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, run.session_id)
        assert s.engine_sessions[str(agy)] == "original-argon-conversation"
        assert f"{agy}:claude-opus-5-5-medium" in s.engine_sessions


@pytest.mark.parametrize("message", [
    "Permission denied: /workspace/quota.py", "403 Forbidden: quota project access denied",
    "sandbox permission denied for /data/429/report", "MCP authorization failed: quota tool forbidden",
])
def test_permission_failures_with_quota_words_are_not_provider_limits(message):
    assert not profiles.is_usage_limit(message)
