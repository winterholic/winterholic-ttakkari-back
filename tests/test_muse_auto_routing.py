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
from ttakkari.models import EngineAccount
from ttakkari.orchestrator.runtime import issue_token, model_routing_note
from ttakkari.runs import usage
from ttakkari.runs import worker_routing as routing

SONNET = "claude-sonnet-5-5-high"
FLASH = "gemini-3.8-flash-high"
MUSE_FULL = "muse-wonder-full-1.0"
MUSE_SPARK = "muse-spark-1.3"


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


def test_default_policy_and_acceptance_exclude_muse():
    assert not routing.muse_auto_enabled()
    assert "muse" not in {e for task in routing.policy() for e in engines(routing.policy(), task)}
    assert not routing.accepts_model("general", MUSE_FULL)
    assert '"engine": "muse"' not in model_routing_note()


def test_both_flags_are_required(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "muse_auto_routing", True)
    assert not routing.muse_auto_enabled(), "auto routing alone must not enable a disabled engine"
    monkeypatch.setattr(settings, "muse_enabled", True)
    monkeypatch.setattr(settings, "muse_auto_routing", False)
    assert not routing.muse_auto_enabled()


def test_enabled_policy_places_muse_only_in_general_and_routine_first_stage(muse_gate):
    policy = routing.policy()
    assert policy["general"][0] == [{"engine": "antigravity", "model": SONNET}, {"engine": "muse", "model": MUSE_FULL}]
    assert policy["routine"][0] == [{"engine": "antigravity", "model": FLASH}, {"engine": "muse", "model": MUSE_SPARK}]
    for task in ("design", "verification", "reasoning"):
        assert "muse" not in engines(policy, task)
    assert routing.accepts_model("general", MUSE_FULL) and routing.accepts_model("routine", MUSE_SPARK)
    assert not routing.accepts_model("verification", MUSE_FULL)
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
        assert routing.needs_independent_verification(task, MUSE_FULL)
        assert routing.needs_independent_verification(task, MUSE_SPARK)
    assert not routing.needs_independent_verification("design", MUSE_FULL)
    assert routing.verifier_recommendation(MUSE_FULL)["excluded_family"] == "muse"
    assert routing.verifier_recommendation()["excluded_family"] == "gemini-3.8-flash"
    assert all(engine != "muse" for stage in routing.ROUTES["verification"] for engine, _ in stage)


def test_pinned_stages_follow_the_gate_and_do_not_offer_a_second_muse_model(monkeypatch):
    def flat(stages):
        return {pair for stage in stages for pair in stage}

    assert ("muse", MUSE_FULL) not in flat(routing.pinned_stages("claude", task_type="general"))
    settings = get_settings()
    monkeypatch.setattr(settings, "muse_enabled", True)
    monkeypatch.setattr(settings, "muse_auto_routing", True)
    monkeypatch.setattr(routing, "MUSE_AUTO_ROUTING_ACCEPTED", True)
    assert ("muse", MUSE_FULL) in flat(routing.pinned_stages("claude", task_type="general"))
    muse_pinned = routing.pinned_stages("muse", task_type="routine")
    assert [pair for pair in flat(muse_pinned) if pair[0] == "muse"] == [("muse", MUSE_FULL)]


async def test_gate_off_keeps_sonnet_even_with_idle_muse_account(client, auth, make_dir, muse):
    muse_usage(muse, used=0)
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", SONNET), run


async def test_general_prefers_muse_when_its_measured_headroom_is_larger(client, auth, make_dir, muse, muse_gate):
    muse_usage(muse, used=0)
    response, h = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["status"], run["engine"], run["model"]) == ("succeeded", "muse", MUSE_FULL), run
    wid = response.json()["worker_id"]
    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert detail["verification_required"] is True and detail["verification_status"] == "pending"
    assert detail["routing"]["verification_required"] is True
    assert detail["routing"]["recommended_verifier"]["excluded_family"] == "muse"
    assert run["usage"]["verification_required"] is True


async def test_routine_can_use_muse_spark_when_it_has_more_room_than_flash(client, auth, make_dir, muse, muse_gate, accounts):
    muse_usage(muse, used=0)
    quota(agy_id(accounts), gemini=40)
    response, _ = await spawn(client, auth, make_dir, task_type="routine")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("muse", MUSE_SPARK), run


async def test_busy_muse_account_yields_to_better_headroom(client, auth, make_dir, muse, muse_gate, accounts):
    muse_usage(muse, used=70)  # 30 left < Sonnet's 80 left
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", SONNET), run


async def test_unknown_muse_quota_never_outranks_measured_sonnet(client, auth, make_dir, muse, muse_gate):
    muse_usage(muse, used=0, age=3600)  # stale means unknown capacity, not spare capacity
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
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
    assert (run["engine"], run["model"]) == ("muse", MUSE_FULL), run


async def test_unknown_muse_with_exhausted_sonnet_uses_the_existing_flash_fallback(client, auth, make_dir, muse, muse_gate, accounts):
    quota(agy_id(accounts), claude=0)
    response, _ = await spawn(client, auth, make_dir, task_type="general")
    run = await result(client, auth, response)
    assert (run["engine"], run["model"]) == ("antigravity", FLASH), run


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
    assert {"engine": "muse", "model": MUSE_FULL} in body["worker_routing"]["general"][0]
    assert "Muse never verifies" in body["note"]
