"""Muse in auto worker routing: gated, quota-aware, personal-only, and never a verifier."""
import time
import uuid
from importlib import import_module

import pytest
from helpers import run_events
from test_orchestrator import _orch, _session

pytest_plugins = ["test_worker_routing"]

from test_worker_routing import (  # noqa: F401  (fixtures imported for pytest)
    quota,
    result,
    spawn,
    synthetic,
)

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, EngineAccount
from ttakkari.orchestrator.runtime import issue_token, model_routing_note
from ttakkari.runs import profiles, usage
from ttakkari.runs import worker_routing as routing

SONNET = "claude-sonnet-5-5-high"
FLASH = "gemini-3.8-flash-high"
MUSE_MODEL = "muse-spark-1.3"


@pytest.fixture
def muse_gate(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "muse_enabled", True)
    monkeypatch.setattr(settings, "muse_auto_routing", True)
    monkeypatch.setattr(routing, "MUSE_AUTO_ROUTING_ACCEPTED", True)


@pytest.fixture
async def muse(accounts, synthetic, monkeypatch):  # noqa: F811
    m = import_module("ttakkari.runs.manager")
    monkeypatch.setitem(m.ADAPTERS, "muse", type(m.ADAPTERS["claude"])())
    async with sessionmaker()() as db:
        account = EngineAccount(name="muse test", engine="muse", enabled=True, priority=99, pool="personal")
        db.add(account)
        await db.commit()
        return account.id


def muse_usage(aid, *, used=0, age=0):
    now = time.time()
    windows = [usage.Window("five_hour", "5h", used, int(now + 1000)), usage.Window("seven_day", "7d", used, int(now + 9000))]
    usage.seed(aid, usage.Usage(windows=windows, updated_at=now - age, source="muse_cli"), refresh=True)


def agy_id(rows):
    return uuid.UUID(next(a["id"] for a in rows if a["engine"] == "antigravity"))


def engines(policy, task):
    return {entry["engine"] for stage in policy[task] for entry in stage}


def test_default_policy_and_acceptance_include_verified_muse():
    assert routing.muse_auto_enabled()
    assert "muse" in engines(routing.policy(), "general")
    assert "muse" in engines(routing.policy(), "routine")
    assert routing.accepts_model("general", MUSE_MODEL)
    assert '"engine": "muse"' in model_routing_note()


def test_both_flags_are_required(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "muse_enabled", False)
    monkeypatch.setattr(settings, "muse_auto_routing", True)
    assert not routing.muse_auto_enabled(), "auto routing alone must not enable a disabled engine"
    monkeypatch.setattr(settings, "muse_enabled", True)
    monkeypatch.setattr(settings, "muse_auto_routing", False)
    assert not routing.muse_auto_enabled()
    assert all("muse" not in engines(routing.policy(), task) for task in routing.ROUTES)
    assert not routing.accepts_model("general", MUSE_MODEL)
    assert '"engine": "muse"' not in model_routing_note()


def test_enabled_policy_places_muse_only_in_general_and_routine_first_stage(muse_gate):
    policy = routing.policy()
    assert policy["general"][0] == [{"engine": "antigravity", "model": SONNET}, {"engine": "muse", "model": MUSE_MODEL}]
    assert policy["routine"][0] == [{"engine": "antigravity", "model": FLASH}, {"engine": "muse", "model": MUSE_MODEL}]
    for task in ("design", "verification", "reasoning"):
        assert "muse" not in engines(policy, task)
    assert routing.accepts_model("general", MUSE_MODEL) and routing.accepts_model("routine", MUSE_MODEL)
    assert not routing.accepts_model("verification", MUSE_MODEL)
    assert '"engine": "muse"' in model_routing_note()
    # The default model of an auto session stays the first stage's first entry, never Muse.
    assert routing.ROUTES["general"][0][0][1] == SONNET


def test_environment_flags_cannot_bypass_unverified_muse_acceptance_latch(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "muse_enabled", True)
    monkeypatch.setattr(settings, "muse_auto_routing", True)
    monkeypatch.setattr(routing, "MUSE_AUTO_ROUTING_ACCEPTED", False)

    assert not routing.muse_auto_enabled()
    assert all("muse" not in engines(routing.policy(), task) for task in routing.ROUTES)


def test_muse_results_need_independent_verification_but_design_does_not():
    for task in ("general", "routine"):
        assert routing.needs_independent_verification(task, MUSE_MODEL)
    assert not routing.needs_independent_verification("design", MUSE_MODEL)
    assert routing.verifier_recommendation(MUSE_MODEL)["excluded_family"] == "muse"
    assert routing.verifier_recommendation()["excluded_family"] == "gemini-3.8-flash"
    assert all(engine != "muse" for stage in routing.ROUTES["verification"] for engine, _ in stage)


def test_pinned_stages_follow_the_gate_and_do_not_offer_a_second_muse_model(monkeypatch):
    def flat(stages):
        return {pair for stage in stages for pair in stage}

    settings = get_settings()
    monkeypatch.setattr(settings, "muse_auto_routing", False)
    assert ("muse", MUSE_MODEL) not in flat(routing.pinned_stages("claude", task_type="general"))
    monkeypatch.setattr(settings, "muse_enabled", True)
    monkeypatch.setattr(settings, "muse_auto_routing", True)
    monkeypatch.setattr(routing, "MUSE_AUTO_ROUTING_ACCEPTED", True)
    assert ("muse", MUSE_MODEL) in flat(routing.pinned_stages("claude", task_type="general"))
    muse_pinned = routing.pinned_stages("muse", task_type="routine")
    assert [pair for pair in flat(muse_pinned) if pair[0] == "muse"] == [("muse", MUSE_MODEL)]


async def test_gate_off_keeps_sonnet_even_with_idle_muse_account(client, auth, make_dir, muse, monkeypatch):
    monkeypatch.setattr(get_settings(), "muse_auto_routing", False)
    muse_usage(muse, used=0)
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", SONNET), run


async def test_general_prefers_muse_when_its_measured_headroom_is_larger(client, auth, make_dir, muse):
    muse_usage(muse, used=0)
    response, h = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["status"], run["engine"], run["model"]) == ("succeeded", "muse", MUSE_MODEL), run
    wid = response.json()["worker_id"]
    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert detail["verification_required"] is True and detail["verification_status"] == "pending"
    assert detail["routing"]["verification_required"] is True
    assert detail["routing"]["recommended_verifier"]["excluded_family"] == "muse"
    assert run["usage"]["verification_required"] is True

    async with sessionmaker()() as db:
        worker = await db.get(ChatSession, uuid.UUID(wid))
        parent_id = worker.parent_id
    orch = await client.get(f"/api/sessions/{parent_id}/runs", headers=auth)
    report = next(r for r in orch.json() if "[작업 보고]" in r["prompt"])
    assert f"muse/{MUSE_MODEL}" in report["prompt"]
    assert "Gemini 3.8 Flash 계열이 수행" not in report["prompt"]


async def test_routine_can_use_muse_spark_when_it_has_more_room_than_flash(client, auth, make_dir, muse, muse_gate, accounts):
    muse_usage(muse, used=0)
    quota(agy_id(accounts), gemini=40)
    response, _ = await spawn(client, auth, make_dir, task_type="routine")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("muse", MUSE_MODEL), run


async def test_busy_muse_account_yields_to_better_headroom(client, auth, make_dir, muse, muse_gate, accounts):
    muse_usage(muse, used=70)  # 30 left < Sonnet's 80 left
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", SONNET), run


async def test_unknown_muse_quota_is_explored_once_then_yields_to_measured_sonnet(client, auth, make_dir, muse, muse_gate):
    # Stale means unknown capacity, never spare capacity. An unknown Muse still gets one rate-limited real task
    # so a stream observation can end the unknown state; otherwise it could never be chosen again.
    muse_usage(muse, used=0, age=3600)
    first = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (first["engine"], first["model"]) == ("muse", MUSE_MODEL), first
    receipt = next(e["payload"] for e in await run_events(client, auth, first["id"]) if e["type"] == "worker.routing")
    assert receipt["selected"]["quota_known"] is False
    second = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (second["engine"], second["model"]) == ("antigravity", SONNET), second
    # After the interval the unobserved Muse is explored again.
    profiles._probe_last[muse] = time.time() - profiles.MUSE_PROBE_INTERVAL - 1
    third = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (third["engine"], third["model"]) == ("muse", MUSE_MODEL), third


async def test_muse_idle_after_five_hour_reset_with_stale_exhaustion_is_explored(client, auth, make_dir, muse, muse_gate):
    # 2026-10-18 incident: 5h window observed at 100% before its reset, weekly 33%, nothing observed since.
    now = time.time()
    usage.seed(muse, usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(now - 3600)),
                                          usage.Window("seven_day", "7d", 33, int(now + 86400))],
                                 updated_at=now - 36000, source="muse_cli", login="ok", error="/usage unavailable"),
               refresh=True)
    assert profiles.usage_limit_until(profiles.Profile(muse, "m", "muse", model=MUSE_MODEL)) is None
    run = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (run["engine"], run["model"]) == ("muse", MUSE_MODEL), run


async def test_unknown_muse_without_login_is_never_explored(client, auth, make_dir, muse, muse_gate):
    now = time.time()
    usage.seed(muse, usage.Usage(login="missing", error="Muse 구독 로그인이 필요해요", updated_at=now), refresh=True)
    run = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (run["engine"], run["model"]) == ("antigravity", SONNET), run


async def test_unknown_muse_in_cooldown_is_not_explored(client, auth, make_dir, muse, muse_gate):
    muse_usage(muse, used=0, age=3600)
    profiles.cool_down(muse, 600)
    run = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (run["engine"], run["model"]) == ("antigravity", SONNET), run


async def test_exhausted_muse_falls_back_without_blocking_other_pools(client, auth, make_dir, muse, muse_gate):
    muse_usage(muse, used=100)
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", SONNET), run
    receipt = next(e["payload"] for e in await run_events(client, auth, run["id"]) if e["type"] == "worker.routing")
    assert any(b["engine"] == "muse" for b in receipt["blocked"])


async def test_muse_absorbs_general_when_sonnet_pool_is_exhausted_before_flash(client, auth, make_dir, muse, muse_gate, accounts):
    quota(agy_id(accounts), claude=0)
    muse_usage(muse, used=10)
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("muse", MUSE_MODEL), run


async def test_unknown_muse_with_exhausted_sonnet_is_explored_then_uses_the_flash_fallback(client, auth, make_dir, muse, muse_gate, accounts):
    quota(agy_id(accounts), claude=0)
    first = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (first["engine"], first["model"]) == ("muse", MUSE_MODEL), first
    second = await result(client, auth, (await spawn(client, auth, make_dir, task_type="general"))[0])
    assert (second["engine"], second["model"]) == ("antigravity", FLASH), second


async def test_muse_engine_without_model_defaults_to_spark_1_3_and_auto_never_defaults_to_muse(client, auth, make_dir, muse, muse_gate):
    muse_usage(muse, used=0)
    response, _ = await spawn(client, auth, make_dir, engine="muse")
    assert response.status_code == 201, response.text
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("muse", MUSE_MODEL), run
    assert get_settings().worker_default_models["muse"]["model"] == MUSE_MODEL
    assert get_settings().engine_models["muse"] == [MUSE_MODEL]
    assert routing.primary_target("muse") == ("muse", MUSE_MODEL)
    # An unsupported or older Muse model is not a silent default: unknown ids fall back to 1.3 in the pinned route.
    assert routing.primary_target("muse", "muse-spark-1.2") == ("muse", MUSE_MODEL)
    auto, _ = await spawn(client, auth, make_dir, engine="auto")
    assert auto.status_code == 201 and (await result(client, auth, auto))["model"] != "muse-spark-1.2"


@pytest.mark.parametrize("task", ["design", "verification", "reasoning"])
async def test_muse_is_not_a_candidate_for_design_verification_or_reasoning(task, client, auth, make_dir, muse, muse_gate):
    muse_usage(muse, used=0)
    async with sessionmaker()() as db:
        ready, blocked = await routing.availability(db, task, "personal")
    assert all(p.engine != "muse" for p in ready) and all(p.engine != "muse" for p, _ in blocked)


async def test_company_scope_never_sees_the_personal_muse_account(client, auth, make_dir, muse, muse_gate):
    muse_usage(muse, used=0)
    async with sessionmaker()() as db:
        ready, blocked = await routing.availability(db, "general", "work")
    assert all(p.engine != "muse" for p in ready) and all(p.engine != "muse" for p, _ in blocked)


async def test_account_usage_reports_the_gate_and_muse_policy(client, auth, make_dir, muse, muse_gate):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    body = (await client.get("/api/orchestrator/accounts/usage", headers=_orch(issue_token(orch["id"])))).json()
    assert body["muse_auto_routing"] is True
    assert {"engine": "muse", "model": MUSE_MODEL} in body["worker_routing"]["general"][0]
    assert "Muse never verifies" in body["note"]
