"""Muse 의 생각 수준은 max 로 고정이다(사용자 결정 2026-10-19: account_usage 가 high 로 보여 지시).

설정 기본값, account_usage 표시, 세션·Run 저장값, 오케스트레이터 spawn, 실행 직전 보정, 어댑터 명세까지 한 값이어야 하고
UI·API 요청이 다른 값을 줘도 바뀌지 않는다.
"""
import json
import uuid
from pathlib import Path

from helpers import wait_run
from test_orchestrator import _session

from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.muse import MuseAdapter
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.meetings import routing as meeting_routing
from ttakkari.models import ChatSession, Run, SessionKind
from ttakkari.runs import account_status, profiles, worker_routing
from ttakkari.runs.manager import _launch_model

MUSE = "muse-spark-1.3"


def prof(model=MUSE):
    return profiles.Profile(id=uuid.uuid4(), name="muse", engine="muse", model=model)


def test_settings_defaults_are_max():
    s = get_settings()
    assert s.orchestrator_fallback_models["muse"] == {"model": MUSE, "effort": "max"}
    assert s.worker_default_models["muse"]["effort"] == "max"


def test_every_routing_helper_returns_max_whatever_is_requested():
    for requested in (None, "low", "medium", "high", "xhigh", "max"):
        assert worker_routing.effort(prof(), requested) == "max"
    assert meeting_routing.effort(prof()) == "max"


def test_worker_launch_ignores_the_stored_effort():
    sess = ChatSession(kind=SessionKind.worker, engine="muse")
    for stored in (None, "low", "high"):
        run = Run(prompt="x", effort=stored)
        assert _launch_model(sess, run, prof(), prof()) == (MUSE, "max")
    orch = ChatSession(kind=SessionKind.orchestrator, engine="auto")
    assert _launch_model(orch, Run(prompt="x", effort="low"), prof(), prof()) == (MUSE, "max")


def test_adapter_spec_is_max_even_when_the_launch_spec_says_otherwise(tmp_path, fake_muse_runtime):
    adapter = MuseAdapter()
    for effort in (None, "low", "high"):
        spec = LaunchSpec(cwd=tmp_path, prompt="x", engine_session_id=None, model=MUSE, effort=effort, outbox=tmp_path,
                          write_roots=[], system_note="n")
        cmd = adapter.build_command(spec, {"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
        try:
            assert json.loads(Path(cmd[-1]).read_text())["effort"] == "max"
        finally:
            adapter.cleanup(spec)


async def test_account_usage_shows_max_for_the_muse_orchestrator_model(client, auth):
    await client.post("/api/accounts", json={"name": "muse main", "engine": "muse"}, headers=auth)
    async with sessionmaker()() as db:
        status = await account_status.account_status(db, "personal")
    muse = [a for a in status["accounts"] if a["engine"] == "muse"]
    assert muse and all(a["orchestrator_model"] == {"model": MUSE, "effort": "max"} for a in muse)


async def test_session_and_run_api_cannot_override_muse_effort(client, auth, make_dir, monkeypatch):
    from importlib import import_module
    manager = import_module("ttakkari.runs.manager")
    monkeypatch.setitem(manager.ADAPTERS, "muse", type(manager.ADAPTERS["claude"])())
    created = await _session(client, auth, make_dir, engine="muse", model=MUSE, effort="low")
    assert created["effort"] == "max"
    r = await client.post(f"/api/sessions/{created['id']}/runs", json={"prompt": "hi", "effort": "low"}, headers=auth)
    assert r.status_code == 202, r.text
    assert r.json()["effort"] == "max"
    done = await wait_run(client, auth, r.json()["id"], 20.0)
    assert done["effort"] == "max"


async def test_other_engines_keep_their_own_effort(client, auth, make_dir):
    created = await _session(client, auth, make_dir, engine="claude", effort="low")
    assert created["effort"] == "low"
    assert worker_routing.effort(profiles.Profile(uuid.uuid4(), "c", "claude", model="sonnet"), "low") == "low"

