"""오케스트레이터 auto 의 Muse 폴백: 네이티브가 모두 한도면 Muse 로 이어서 돈다."""
import uuid
from importlib import import_module

from conftest import agent_calls
from helpers import run_to_end
from test_orchestrator import _session

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, SessionKind
from ttakkari.runs import profiles
from ttakkari.runs.manager import _available_profiles, _launch_model


def test_orchestrator_launch_pair_for_muse():
    sess = ChatSession(kind=SessionKind.orchestrator, engine="auto")
    run = Run(prompt="fixture")
    prof = profiles.Profile(id=uuid.uuid4(), name="muse", engine="muse", model="muse-spark-1.3")
    assert _launch_model(sess, run, prof, prof) == ("muse-spark-1.3", "max")
    assert get_settings().orchestrator_fallback_models["muse"]["model"] == "muse-spark-1.3"


async def _seed_muse(client, auth):
    await client.get("/api/accounts", headers=auth)
    created = await client.post("/api/accounts", json={"name": "muse main", "engine": "muse"}, headers=auth)
    assert created.status_code == 201, created.text
    return created.json()


async def test_auto_hides_muse_unless_orchestrator_flag(client, auth, monkeypatch):
    muse = await _seed_muse(client, auth)
    async with sessionmaker()() as db:
        default, _ = await profiles.availability(db, "auto", "personal")
        assert all(p.engine != "muse" for p in default)
        flagged, _ = await profiles.availability(db, "auto", "personal", include_muse=True)
        assert any(str(p.id) == muse["id"] and p.engine == "muse" for p in flagged)
        assert all(p.engine in {"claude", "codex", "muse"} for p in flagged)
        monkeypatch.setattr(get_settings(), "muse_enabled", False)
        disabled, _ = await profiles.availability(db, "auto", "personal", include_muse=True)
        assert all(p.engine != "muse" for p in disabled)


async def test_orchestrator_gets_muse_but_desk_and_chat_do_not(client, auth):
    muse = await _seed_muse(client, auth)
    async with sessionmaker()() as db:
        for kind, expected in ((SessionKind.orchestrator, True), (SessionKind.desk, False),
                               (SessionKind.chat, False)):
            sess = ChatSession(kind=kind, engine="auto", pool="personal")
            run = Run(prompt="fixture")
            free, _ = await _available_profiles(db, sess, run, False)
            assert (any(str(p.id) == muse["id"] for p in free)) is expected, kind
        work = ChatSession(kind=SessionKind.orchestrator, engine="auto", pool="work")
        free, _ = await _available_profiles(db, work, Run(prompt="fixture"), False)
        assert all(p.engine != "muse" for p in free)


async def test_orchestrator_run_fails_over_to_muse_when_natives_limited(client, auth, make_dir, monkeypatch):
    muse = await _seed_muse(client, auth)
    manager = import_module("ttakkari.runs.manager")
    monkeypatch.setitem(manager.ADAPTERS, "muse", type(manager.ADAPTERS["claude"])())
    rows = (await client.get("/api/accounts", headers=auth)).json()
    for row in rows:
        if row["engine"] in {"claude", "codex"}:
            profiles.cool_down(uuid.UUID(row["id"]), 600)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    run = await run_to_end(client, auth, orch["id"], "대기 없이 Muse 로 이어서 실행")
    assert run["status"] == "succeeded", run
    assert run["engine"] == "muse", run
    assert run["profile"] == muse["name"]
    launched = [c for c in agent_calls() if "--model" in c["argv"]]
    assert launched, "가짜 에이전트 호출이 기록되지 않음"
    argv = launched[-1]["argv"]
    assert argv[argv.index("--model") + 1] == "muse-spark-1.3"
    assert argv[argv.index("--effort") + 1] == "max"
