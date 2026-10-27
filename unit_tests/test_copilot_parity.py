"""Copilot must be usable by the planner, not merely a registered adapter."""
import time
from pathlib import Path
from uuid import uuid4

import pytest

from ttakkari.agents.base import ParseState
from ttakkari.agents.copilot import CopilotAdapter
from ttakkari.runs import copilot_usage, profiles, usage, worker_routing


async def test_system_quota_preserves_login_home_without_ambient_tokens(monkeypatch):
    observed = {}

    async def rpc(**kwargs):
        observed.update(kwargs)
        return {"quotaSnapshots": {"chat": {"isUnlimitedEntitlement": True}}}

    monkeypatch.setattr(copilot_usage, "_quota_rpc", rpc)
    monkeypatch.setenv("GH_TOKEN", "synthetic-must-strip")
    result = await copilot_usage.fetch_usage(uuid4(), "copilot", None)
    assert not result.error
    assert observed["env"]["HOME"] == str(Path.home())
    assert "GH_TOKEN" not in observed["env"]
    assert observed["use_logged_in_user"] is True and observed["token"] is None


def sample(aid, remaining, *, error=None, reset=None):
    now = time.time()
    usage.seed(aid, usage.Usage(source="copilot_sdk", updated_at=now, error=error, windows=[
        usage.Window("copilot_premium_interactions", "monthly", min(99, round(100-remaining)) if remaining else 100,
                     reset or now+3600, remaining)]), refresh=True)


def test_monthly_quota_is_measured_capacity_and_routes_real_work():
    aid = uuid4()
    sample(aid, 98.1)
    p = profiles.Profile(aid, "copilot", "copilot", model="auto")
    room = profiles._usage_headroom(p)
    assert room and room[0] == pytest.approx(98.1)
    capacity = worker_routing.capacity(p, 1)
    assert capacity["quota_known"] and capacity["headroom_per_run_percent"] == pytest.approx(49.05)
    assert capacity["quota_windows"][0]["remaining_percent"] == 98.1
    from ttakkari.runs import account_status, usage_collector
    assert account_status._cap_remaining(aid, 100, account_status._usage(aid, time.time(), engine="copilot"),
                                         engine="copilot") == pytest.approx(98.1)
    assert usage_collector.lowest_remaining(usage.cached(aid), time.time()) == pytest.approx(98.1)
    for task in ("general", "routine"):
        assert worker_routing.first_stage([p], task) == [p]
        assert {"engine": "copilot", "model": "auto"} in worker_routing.policy()[task][0]
        assert worker_routing.receipt(task, p, [p], [], {}, trigger="initial")["verification_required"]
    for task in ("design", "verification", "reasoning"):
        assert not any(r["engine"] == "copilot" for stage in worker_routing.policy()[task] for r in stage)


def test_monthly_exhaustion_and_caps_block_even_after_refresh_failure():
    aid = uuid4()
    p = profiles.Profile(aid, "copilot", "copilot", model="auto", max_used_percent=95)
    sample(aid, 4.9, error="temporary provider failure")
    assert profiles._usage_headroom(p) is None
    assert profiles.usage_limit_until(p) > time.time()
    sample(aid, 0)
    assert profiles.usage_limit_until(p) > time.time()
    sample(aid, 98.1)
    assert profiles.usage_limit_until(p) is None
    assert profiles._usage_headroom(p)[0] == pytest.approx(93.1)


def test_unknown_and_unlimited_do_not_invent_routing_headroom():
    aid = uuid4()
    usage.seed(aid, usage.Usage(source="copilot_sdk", updated_at=time.time(),
                             quota_unlimited={"copilot_chat": "chat"}), refresh=True)
    assert profiles._usage_headroom(profiles.Profile(aid, "copilot", "copilot", model="auto")) is None


def test_monthly_collector_uses_exact_remaining():
    from ttakkari.runs.usage_collector import lowest_remaining
    aid = uuid4()
    sample(aid, 18.1)
    assert lowest_remaining(usage.cached(aid), time.time()) == 18.1


def test_copilot_context_event_drives_existing_context_rotation():
    state = ParseState()
    events = CopilotAdapter().parse_line({"type": "session.usage_info", "data": {
        "currentTokens": 140000, "tokenLimit": 200000, "messagesLength": 40}}, state, Path.cwd())
    assert (state.context_tokens, state.context_window) == (140000, 200000)
    from ttakkari.runs.context_usage import measure
    assert measure(state, "copilot", None)["context_percent"] == 70
    assert events


@pytest.mark.parametrize("tokens,window", [(True, 100), (-1, 100), (20, 0), ("20", 100)])
def test_invalid_context_is_not_published(tokens, window):
    state = ParseState()
    CopilotAdapter().parse_line({"type": "session.usage_info", "data": {
        "currentTokens": tokens, "tokenLimit": window}}, state, Path.cwd())
    assert state.context_tokens is None and state.context_window is None


async def test_activation_tool_timeout_covers_bounded_auth_and_quota(monkeypatch):
    from ttakkari.orchestrator import mcp_server
    observed = {}

    async def call(method, path, **kwargs):
        observed.update(kwargs)
        return '{}'

    monkeypatch.setattr(mcp_server, '_call', call)
    await mcp_server.activate_copilot_account()
    # Version (10s), auth (40s), quota (20s), SDK stop (5s), request overhead.
    assert observed.get('timeout', 30) >= 90
