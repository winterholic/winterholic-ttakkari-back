"""엔진 계정 관리(ADR 0006): 이름·켜기·우선순위가 실제 실행 순서를 바꾼다."""
from __future__ import annotations

from conftest import HOME, agent_calls
from helpers import run_to_end, start_run, wait_event


async def _accounts(client, auth) -> list[dict]:
    r = await client.get("/api/accounts", headers=auth)
    assert r.status_code == 200
    return r.json()


async def _session(client, auth, make_dir, **body) -> dict:
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    return (await client.post("/api/sessions", json={"workspace_id": ws["id"], **body}, headers=auth)).json()


async def test_defaults_are_seeded_in_priority_order(client, auth):
    accs = await _accounts(client, auth)
    assert [(a["name"], a["engine"], a["enabled"], a["priority"]) for a in accs] == [
        ("claude", "claude", True, 0), ("codex", "codex", True, 1)]
    assert {a["login"] for a in accs} <= {"ok", "unknown", "missing"}
    assert {a["name"]: a["max_used_percent"] for a in accs} == {"claude": 100, "codex": 100}


async def test_concurrent_first_runs_seed_accounts_once(monkeypatch):
    import asyncio

    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import AsyncSession

    from ttakkari.db import sessionmaker
    from ttakkari.models import EngineAccount
    from ttakkari.runs.profiles import ensure_seeded

    barrier = asyncio.Barrier(2)
    seen = set()
    original = AsyncSession.scalar

    async def simultaneous_empty_read(session, statement, *args, **kwargs):
        value = await original(session, statement, *args, **kwargs)
        sql = str(statement)
        if id(session) not in seen and "count(*)" in sql and "engine_accounts" in sql:
            seen.add(id(session))
            await asyncio.wait_for(barrier.wait(), 5)
        return value

    monkeypatch.setattr(AsyncSession, "scalar", simultaneous_empty_read)

    async def seed():
        async with sessionmaker()() as db:
            await ensure_seeded(db)

    results = await asyncio.gather(seed(), seed(), return_exceptions=True)
    assert not any(isinstance(r, BaseException) for r in results), results
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(EngineAccount).order_by(EngineAccount.priority)))
        assert [a.name for a in rows] == ["claude", "codex"]


async def test_create_rename_toggle_delete(client, auth, make_dir):
    d = make_dir()
    r = await client.post("/api/accounts", json={"name": "회사 계정", "engine": "claude", "config_dir": str(d)}, headers=auth)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["priority"] == 2 and a["config_dir"] == str(d)
    assert a["max_used_percent"] == 100
    assert (await client.post("/api/accounts", json={"name": "회사 계정", "engine": "codex"}, headers=auth)).status_code == 409
    r = await client.patch(f"/api/accounts/{a['id']}", json={"name": "개인", "enabled": False}, headers=auth)
    assert r.json()["name"] == "개인" and r.json()["enabled"] is False
    assert (await client.patch(f"/api/accounts/{a['id']}", json={"max_used_percent": 95}, headers=auth)).json()["max_used_percent"] == 95
    assert (await client.patch(f"/api/accounts/{a['id']}", json={"max_used_percent": 101}, headers=auth)).status_code == 422
    assert (await client.patch(f"/api/accounts/{a['id']}", json={"max_used_percent": 49}, headers=auth)).status_code == 422
    assert (await client.delete(f"/api/accounts/{a['id']}", headers=auth)).status_code == 204
    assert [x["name"] for x in await _accounts(client, auth)] == ["claude", "codex"]


async def test_config_dir_validation(client, auth):
    bad = [{"config_dir": "relative/dir"}, {"config_dir": str(HOME / "x")}]
    for b in bad:
        r = await client.post("/api/accounts", json={"name": "x", "engine": "claude", **b}, headers=auth)
        assert r.status_code == 422, (b, r.text)


async def test_named_accounts_use_database_default_until_explicitly_configured(client, auth):
    for name, engine in (("내 클로드", "claude"), ("Daddy codex", "codex"), ("FDE", "claude")):
        response = await client.post("/api/accounts", json={"name": name, "engine": engine}, headers=auth)
        assert response.status_code == 201, response.text
        assert response.json()["max_used_percent"] == 100


async def test_reorder_changes_fallback_order(client, auth, make_dir):
    accs = await _accounts(client, auth)
    r = await client.post("/api/accounts/reorder", json={"ids": [accs[1]["id"], accs[0]["id"]]}, headers=auth)
    assert [a["name"] for a in r.json()] == ["codex", "claude"]
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await run_to_end(client, auth, sess["id"], "hi")
    assert run["profile"] == "codex"
    partial = await client.post("/api/accounts/reorder", json={"ids": [accs[0]["id"]]}, headers=auth)
    assert partial.status_code == 200
    assert [a["id"] for a in partial.json()] == [accs[0]["id"], accs[1]["id"]]
    assert (await client.post("/api/accounts/reorder", json={"ids": [accs[0]["id"], accs[0]["id"]]},
                              headers=auth)).status_code == 400
    from uuid import uuid4
    assert (await client.post("/api/accounts/reorder", json={"ids": [str(uuid4())]},
                              headers=auth)).status_code == 400


async def test_reorder_accepts_stale_subset_after_account_is_added(client, auth):
    original = await _accounts(client, auth)
    added = (await client.post("/api/accounts", json={"name": "new", "engine": "claude"}, headers=auth)).json()
    later = (await client.post("/api/accounts", json={"name": "later", "engine": "codex"}, headers=auth)).json()
    ordered = await client.post("/api/accounts/reorder",
                                json={"ids": [original[1]["id"], original[0]["id"]]}, headers=auth)
    assert ordered.status_code == 200, ordered.text
    assert [a["id"] for a in ordered.json()] == [original[1]["id"], original[0]["id"], added["id"], later["id"]]


async def test_disabled_account_is_skipped_and_explicit_engine_fails_clearly(client, auth, make_dir):
    claude = (await _accounts(client, auth))[0]
    await client.patch(f"/api/accounts/{claude['id']}", json={"enabled": False}, headers=auth)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, orch["id"], "hi"))["profile"] == "codex"
    chat = await _session(client, auth, make_dir, engine="claude")
    run = await run_to_end(client, auth, chat["id"], "hi")
    assert run["status"] == "failed" and "켜진 개인 claude 계정이 없습니다" in run["error"]


async def test_second_claude_account_takes_over_with_its_config_dir(client, auth, make_dir):
    d = make_dir()
    await client.post("/api/accounts", json={"name": "claude-2", "engine": "claude", "config_dir": str(d)}, headers=auth)
    accs = await _accounts(client, auth)
    await client.post("/api/accounts/reorder", json={"ids": [accs[0]["id"], accs[2]["id"], accs[1]["id"]]}, headers=auth)
    chat = await _session(client, auth, make_dir, engine="claude", strict_engine=True)
    started = await start_run(client, auth, chat["id"], "CLAUDE_LIMIT")
    # 두 claude 계정 모두 한도면 실패하지 않고 초기화를 기다리지만, 두 번째 계정은 자기 폴더로 실행됐다.
    await wait_event(client, auth, started["id"], "run.waiting")
    calls = [c for c in agent_calls() if c["engine"] == "claude"]
    assert [c["account_dir"] for c in calls] == [None, str(d)]
    run = (await client.get(f"/api/runs/{started['id']}", headers=auth)).json()
    assert run["profile"] == "claude-2" and run["status"] == "queued"
    listed = {a["name"]: a for a in await _accounts(client, auth)}
    assert listed["claude"]["cooldown_seconds"] > 0 and listed["claude-2"]["cooldown_seconds"] > 0
    r = await client.post(f"/api/accounts/{listed['claude']['id']}/cooldown/clear", headers=auth)
    assert r.json()["cooldown_seconds"] == 0


async def test_rename_keeps_conversation(client, auth, make_dir):
    chat = await _session(client, auth, make_dir, engine="claude")
    await run_to_end(client, auth, chat["id"], "first")
    claude = (await _accounts(client, auth))[0]
    await client.patch(f"/api/accounts/{claude['id']}", json={"name": "메인"}, headers=auth)
    run = await run_to_end(client, auth, chat["id"], "second")
    assert run["profile"] == "메인"
    assert "--resume" in agent_calls()[-1]["argv"]


async def test_default_account_does_not_inherit_parent_account_dir(client, auth, make_dir, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/somewhere/else")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/somewhere/else")
    for engine in ("claude", "codex"):
        chat = await _session(client, auth, make_dir, engine=engine)
        await run_to_end(client, auth, chat["id"], "hi")
        assert agent_calls()[-1]["account_dir"] is None


async def test_extra_account_gets_its_own_login_folder_automatically(client, auth):
    a = (await client.post("/api/accounts", json={"name": "회사", "engine": "claude"}, headers=auth)).json()
    assert a["config_dir"] == str(HOME / "accounts" / a["id"]) and (HOME / "accounts" / a["id"]).is_dir()
    assert a["login_command"] == f"CLAUDE_CONFIG_DIR={a['config_dir']} claude"
    # 기본 계정은 그대로 기본 위치를 쓴다.
    base = next(x for x in (await client.get("/api/accounts", headers=auth)).json() if x["name"] == "claude")
    assert base["config_dir"] is None
    cx = (await client.post("/api/accounts", json={"name": "codex-2", "engine": "codex"}, headers=auth)).json()
    assert cx["login_command"].startswith("CODEX_HOME=") and cx["login_command"].endswith("codex login")


async def test_temporary_override_reorders_until_it_expires(client, auth, make_dir):
    from datetime import UTC, datetime, timedelta

    accs = await _accounts(client, auth)
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    r = await client.put("/api/accounts/override", json={"ids": [accs[1]["id"]], "until": until}, headers=auth)
    assert r.status_code == 200 and r.json()["active"]
    assert (await client.get("/api/accounts/override", headers=auth)).json()["ids"] == [accs[1]["id"]]
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, sess["id"], "hi"))["profile"] == "codex"
    # 기본 순서 자체는 그대로다.
    assert [a["name"] for a in await _accounts(client, auth)] == ["claude", "codex"]
    assert (await client.delete("/api/accounts/override", headers=auth)).status_code == 204
    assert (await run_to_end(client, auth, sess["id"], "again"))["profile"] == "claude"
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert (await client.put("/api/accounts/override", json={"ids": [accs[1]["id"]], "until": past}, headers=auth)).status_code == 400


async def test_expired_override_is_ignored(client, auth, make_dir):
    from datetime import UTC, datetime, timedelta

    from ttakkari.db import sessionmaker
    from ttakkari.models import AppSetting

    accs = await _accounts(client, auth)
    async with sessionmaker()() as db:
        db.add(AppSetting(key="account_priority_override:personal",
                          value={"ids": [accs[1]["id"]], "until": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}))
        await db.commit()
    assert (await client.get("/api/accounts/override", headers=auth)).json() == {"active": False, "ids": [], "until": None}
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, sess["id"], "hi"))["profile"] == "claude"


async def test_model_goes_only_to_matching_engine(client, auth, make_dir):
    sess = await _session(client, auth, make_dir, kind="orchestrator", model="opus")
    await run_to_end(client, auth, sess["id"], "CLAUDE_LIMIT")
    calls = agent_calls()
    claude = next(c for c in calls if c["engine"] == "claude")["argv"]
    codex = next(c for c in calls if c["engine"] == "codex")["argv"]
    # claude 모델 이름(opus)은 codex 로 가지 않는다. 오케스트레이터는 지침의 허용 모델로 맞춘다.
    assert claude[claude.index("--model") + 1] == "opus" and codex[codex.index("-m") + 1] == "gpt-6.1-sol"


async def test_usage_from_stream_and_codex_session_log(client, auth, make_dir, monkeypatch):
    import json as _json

    from ttakkari.runs import usage

    async def no_token(_d):
        return None
    monkeypatch.setattr(usage, "_claude_token", no_token)
    # 기본 계정(~/.codex)이 실제 홈의 로그를 읽지 않게 한다.
    monkeypatch.setattr(usage, "HOME", HOME / "fake-home")
    usage.clear_cache()
    d = make_dir()
    cx = (await client.post("/api/accounts", json={"name": "cx2", "engine": "codex"}, headers=auth)).json()
    sess_dir = HOME / "accounts" / cx["id"] / "sessions" / "2026" / "10" / "11"
    sess_dir.mkdir(parents=True)
    (sess_dir / "rollout.jsonl").write_text(_json.dumps({"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {
        "primary": {"used_percent": 37.0, "window_minutes": 300, "resets_at": 1791734450},
        "secondary": {"used_percent": 12.5, "window_minutes": 10080, "resets_at": 1792321250}, "plan_type": "plus"}}}) + "\n")
    chat = await _session(client, auth, make_dir, engine="claude")
    await run_to_end(client, auth, chat["id"], "hi")
    rows = {r["name"]: r for r in (await client.get("/api/accounts/usage", params={"refresh": True}, headers=auth)).json()}
    cl = rows["claude"]
    assert cl["source"] == "stream" and [(w["key"], w["percent"]) for w in cl["windows"]] == [("five_hour", 42), ("seven_day", 10)]
    assert cl["rank"] == 1 and "토큰" in cl["error"]
    c2 = rows["cx2"]
    assert c2["source"] == "session_log" and c2["plan"] == "plus"
    assert [(w["label"], w["percent"]) for w in c2["windows"]] == [("5시간", 37), ("주간", 12)]
    assert rows["codex"]["windows"] == [] and rows["codex"]["error"]
    assert d


def test_usage_api_parsing_prefers_limits_list():
    from ttakkari.runs.usage import keychain_service, windows_from_api

    body = {"five_hour": {"utilization": 60.0, "resets_at": "2026-10-11T15:19:59+00:00"},
            "limits": [{"kind": "session", "percent": 61, "resets_at": "2026-10-11T15:19:59+00:00"},
                       {"kind": "weekly_all", "percent": 52, "resets_at": "2026-10-15T00:59:59+00:00"},
                       {"kind": "weekly_scoped", "percent": 30, "resets_at": "2026-10-15T00:59:59+00:00",
                        "scope": {"model": {"display_name": "Fable"}}}]}
    assert [(w.key, w.label, w.percent) for w in windows_from_api(body)] == [
        ("five_hour", "5시간", 61), ("seven_day", "주간", 52), ("seven_day_fable", "주간 Fable", 30)]
    assert [(w.key, w.percent) for w in windows_from_api({"five_hour": {"utilization": 60.0}})] == [("five_hour", 60)]
    assert keychain_service(None) == "Claude Code-credentials"
    assert keychain_service("/Users/winterholic/.ttakkari/accounts/890cf835-c9cf-4322-840e-a11b1bf98a4e") == "Claude Code-credentials-a519acfa"


def test_reset_time_parsed_from_claude_session_limit_message():
    from ttakkari.runs.profiles import is_usage_limit, reset_seconds_from

    message = "You've hit your session limit \u00b7 resets 7:20pm (Asia/Seoul)"
    assert is_usage_limit(message)
    seconds = reset_seconds_from(message)
    assert seconds is not None and 0 < seconds <= 86430
    assert not is_usage_limit("session limit settings")


def test_reset_time_parsed_from_codex_limit_message():
    from datetime import datetime, timedelta

    from ttakkari.runs.profiles import reset_seconds_from

    when = datetime.now().astimezone() + timedelta(hours=3)
    msg = f"You’ve hit your usage limit. Upgrade to Pro ... or try again at {when:%b} {when.day}th, {when.year} {when:%I:%M %p}."
    left = reset_seconds_from(None, msg)
    assert left is not None and abs(left - 3 * 3600) < 120
    assert reset_seconds_from("no date here") is None


async def test_work_group_sessions_use_only_work_accounts_personal_use_all(client, auth, make_dir):
    from ttakkari.orchestrator.runtime import issue_token

    w = (await client.post("/api/accounts", json={"name": "회사 codex", "engine": "codex", "pool": "work"}, headers=auth)).json()
    # 회사 그룹 계정도 전체 목록의 맨 뒤 순번을 받는다.
    assert w["pool"] == "work" and w["priority"] == len(await _accounts(client, auth)) - 1
    # 회사 대화는 개인 계정(claude 1순위)을 건너뛰고 회사 그룹 계정만 쓴다.
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": "w", "root_path": str(root)}, headers=auth)).json()
    orch = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator", "pool": "work"},
                              headers=auth)).json()
    assert orch["pool"] == "work"
    run = await run_to_end(client, auth, orch["id"], "hi")
    assert run["profile"] == "회사 codex" and agent_calls()[-1]["engine"] == "codex"
    wid = (await client.post("/api/orchestrator/workers", json={"title": "t", "prompt": "go"},
                             headers={"Authorization": f"Bearer {issue_token(orch['id'])}"})).json()["worker_id"]
    worker = (await client.get(f"/api/sessions/{wid}", headers=auth)).json()
    assert worker["pool"] == "work"
    # 개인 대화는 전체 목록 순서를 따른다(회사 계정도 후보에 있다).
    personal = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, personal["id"], "hi"))["profile"] == "claude"
    # 회사 계정을 1순위로 올리면 개인 대화도 그 계정을 쓴다.
    accs = await _accounts(client, auth)
    ids = [w["id"]] + [a["id"] for a in accs if a["id"] != w["id"]]
    assert (await client.post("/api/accounts/reorder", json={"ids": ids}, headers=auth)).status_code == 200
    again = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, again["id"], "hi"))["profile"] == "회사 codex"
    # 회사 대화 목록만 거르기
    got = [s["id"] for s in (await client.get("/api/sessions", params={"pool": "work"}, headers=auth)).json()]
    assert got == [orch["id"]]


async def test_work_session_with_no_enabled_work_accounts_fails_clearly(client, auth, make_dir):
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": "w", "root_path": str(root)}, headers=auth)).json()
    s = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator", "pool": "work"},
                           headers=auth)).json()
    run = await run_to_end(client, auth, s["id"], "hi")
    assert run["status"] == "failed" and "회사" in run["error"]


async def test_tag_account_as_work_keeps_position_and_reorder_is_global(client, auth):
    accs = await _accounts(client, auth)
    r = await client.patch(f"/api/accounts/{accs[1]['id']}", json={"pool": "work"}, headers=auth)
    assert r.json()["pool"] == "work" and r.json()["priority"] == accs[1]["priority"]
    a2 = (await client.post("/api/accounts", json={"name": "회사2", "engine": "claude", "pool": "work"}, headers=auth)).json()
    r = await client.post("/api/accounts/reorder", json={"ids": [a2["id"], accs[1]["id"], accs[0]["id"]]}, headers=auth)
    assert r.status_code == 200 and [a["name"] for a in r.json()] == ["회사2", "codex", "claude"]
    partial = await client.post("/api/accounts/reorder",
                                json={"ids": [accs[0]["id"], a2["id"]]}, headers=auth)
    assert partial.status_code == 200
    assert [a["id"] for a in partial.json()] == [accs[0]["id"], a2["id"], accs[1]["id"]]
    usage = (await client.get("/api/accounts/usage", headers=auth)).json()
    assert [(u["id"], u["rank"]) for u in usage] == [
        (accs[0]["id"], 1), (a2["id"], 2), (accs[1]["id"], 3)]
    # 회사 그룹에서 빼면 다시 개인으로, 자리는 그대로.
    r = await client.patch(f"/api/accounts/{accs[1]['id']}", json={"pool": "personal"}, headers=auth)
    assert r.json()["pool"] == "personal"


async def _work_session(client, auth, make_dir) -> dict:
    return await _session(client, auth, make_dir, kind="orchestrator", pool="work")


async def test_override_is_per_mode(client, auth, make_dir):
    from datetime import UTC, datetime, timedelta

    w1 = (await client.post("/api/accounts", json={"name": "회사 A", "engine": "claude", "pool": "work"}, headers=auth)).json()
    w2 = (await client.post("/api/accounts", json={"name": "회사 B", "engine": "claude", "pool": "work"}, headers=auth)).json()
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    ghost = "00000000-0000-0000-0000-000000000001"
    assert (await client.put("/api/accounts/override", json={"ids": [ghost], "until": until}, headers=auth)).status_code == 400
    # 회사 모드 임시 순서에는 회사에 들어온 계정만 쓸 수 있다.
    codex = next(a for a in await _accounts(client, auth) if a["name"] == "codex")
    assert (await client.put("/api/accounts/override", params={"pool": "work"}, json={"ids": [codex["id"]], "until": until},
                             headers=auth)).status_code == 400
    r = await client.put("/api/accounts/override", params={"pool": "work"}, json={"ids": [w2["id"]], "until": until}, headers=auth)
    assert r.json()["active"]
    # 회사 모드 임시 순서는 회사 대화에만 먹고, 개인 모드 임시 순서는 따로 비어 있다.
    assert (await client.get("/api/accounts/override", headers=auth)).json()["active"] is False
    assert (await client.get("/api/accounts/override", params={"pool": "work"}, headers=auth)).json()["ids"] == [w2["id"]]
    assert (await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi"))["profile"] == "회사 B"
    p = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, p["id"], "hi"))["profile"] == "claude"
    # 개인 모드 임시 순서(회사 A)는 개인 대화에만 먹는다.
    assert (await client.put("/api/accounts/override", json={"ids": [w1["id"]], "until": until}, headers=auth)).json()["active"]
    p2 = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, p2["id"], "hi"))["profile"] == "회사 A"
    assert (await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi"))["profile"] == "회사 B"
    # 회사 쪽만 끝내면 회사 대화는 기본 회사 순서로 돌아가고 개인 쪽은 그대로다.
    assert (await client.delete("/api/accounts/override", params={"pool": "work"}, headers=auth)).status_code == 204
    assert (await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi"))["profile"] == "회사 A"
    assert (await client.get("/api/accounts/override", headers=auth)).json()["active"] is True


async def test_on_off_is_independent_per_mode(client, auth, make_dir):
    w = (await client.post("/api/accounts", json={"name": "회사 A", "engine": "claude", "pool": "work"}, headers=auth)).json()
    assert w["enabled"] is True and w["work_enabled"] is True
    # 개인 모드에서만 끄면 회사 모드는 그대로 켜져 있다.
    r = await client.patch(f"/api/accounts/{w['id']}", json={"enabled": False}, headers=auth)
    assert r.json()["enabled"] is False and r.json()["work_enabled"] is True
    assert (await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi"))["profile"] == "회사 A"
    # 개인 대화는 꺼진 계정을 건너뛰고 다음(claude)을 쓴다. 회사 계정을 1순위로 올려도 마찬가지다.
    ids = [w["id"]] + [a["id"] for a in await _accounts(client, auth) if a["id"] != w["id"]]
    await client.post("/api/accounts/reorder", json={"ids": ids}, headers=auth)
    assert (await run_to_end(client, auth, (await _session(client, auth, make_dir, kind="orchestrator"))["id"], "hi"))["profile"] == "claude"
    # 반대로 회사 모드에서만 끄면 회사 대화는 실패하고, 개인 모드는 켜 두었으면 그대로 쓴다.
    await client.patch(f"/api/accounts/{w['id']}", json={"enabled": True, "work_enabled": False}, headers=auth)
    again = (await client.get("/api/accounts", headers=auth)).json()
    row = next(a for a in again if a["id"] == w["id"])
    assert row["enabled"] is True and row["work_enabled"] is False
    failed = await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi")
    assert failed["status"] == "failed" and "회사" in failed["error"]
    assert (await run_to_end(client, auth, (await _session(client, auth, make_dir, kind="orchestrator"))["id"], "hi"))["profile"] == "회사 A"
    # 회사에 들어오지 않은 계정은 회사 모드 켜기를 바꿀 수 없다.
    claude = next(a for a in again if a["name"] == "claude")
    assert (await client.patch(f"/api/accounts/{claude['id']}", json={"work_enabled": False}, headers=auth)).status_code == 400


async def test_work_order_is_independent_and_new_members_go_last(client, auth, make_dir):
    accs = await _accounts(client, auth)
    codex, claude = accs[1], accs[0]
    # 회사에 들이는 순서가 회사 모드 순서다(개인 순서와 무관). 들일 때 회사 모드는 켜진 채로 시작한다.
    r = await client.patch(f"/api/accounts/{codex['id']}", json={"pool": "work"}, headers=auth)
    assert r.json()["pool"] == "work" and r.json()["work_enabled"] is True and r.json()["priority"] == codex["priority"]
    r = await client.patch(f"/api/accounts/{claude['id']}", json={"pool": "work"}, headers=auth)
    assert r.json()["work_priority"] > (await client.get("/api/accounts", headers=auth)).json()[1]["work_priority"]
    work = await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi")
    assert work["profile"] == "codex"
    # 회사 순서만 바꾸면 회사 대화만 달라지고 개인 순서는 그대로다.
    r = await client.post("/api/accounts/reorder", params={"pool": "work"}, json={"ids": [claude["id"], codex["id"]]}, headers=auth)
    assert [a["name"] for a in r.json()] == ["claude", "codex"]
    assert [a["name"] for a in await _accounts(client, auth)] == ["claude", "codex"]
    assert (await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi"))["profile"] == "claude"
    await client.post("/api/accounts/reorder", json={"ids": [codex["id"], claude["id"]]}, headers=auth)
    assert (await run_to_end(client, auth, (await _session(client, auth, make_dir, kind="orchestrator"))["id"], "hi"))["profile"] == "codex"
    assert (await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi"))["profile"] == "claude"
    # 회사 순서 저장은 회사에 없는 계정이 섞이면 거절하고, 읽은 뒤 들어온 회사 계정은 뒤에 보존한다.
    third = (await client.post("/api/accounts", json={"name": "회사 C", "engine": "claude", "pool": "work"}, headers=auth)).json()
    outsider = (await client.post("/api/accounts", json={"name": "개인 D", "engine": "claude"}, headers=auth)).json()
    assert (await client.post("/api/accounts/reorder", params={"pool": "work"}, json={"ids": [outsider["id"]]}, headers=auth)).status_code == 400
    r = await client.post("/api/accounts/reorder", params={"pool": "work"}, json={"ids": [codex["id"]]}, headers=auth)
    assert [a["id"] for a in r.json()] == [codex["id"], claude["id"], third["id"]]
    # 회사에서 빼면 회사 대화는 더 이상 쓰지 않는다. 개인 목록에는 그대로 남는다.
    for a in (codex, claude, third):
        await client.patch(f"/api/accounts/{a['id']}", json={"pool": "personal"}, headers=auth)
    assert len(await _accounts(client, auth)) == 4
    assert (await run_to_end(client, auth, (await _work_session(client, auth, make_dir))["id"], "hi"))["status"] == "failed"
    # 다시 들이면 회사 순서 맨 뒤에서 새로 시작한다.
    r = await client.patch(f"/api/accounts/{codex['id']}", json={"pool": "work"}, headers=auth)
    assert r.json()["work_priority"] == 0


async def test_usage_reports_both_modes(client, auth):
    accs = await _accounts(client, auth)
    await client.patch(f"/api/accounts/{accs[1]['id']}", json={"pool": "work"}, headers=auth)
    await client.patch(f"/api/accounts/{accs[1]['id']}", json={"work_enabled": False}, headers=auth)
    usage = {u["name"]: u for u in (await client.get("/api/accounts/usage", headers=auth)).json()}
    assert usage["claude"]["work_rank"] is None and usage["claude"]["enabled"] is True
    assert usage["codex"]["rank"] == 2 and usage["codex"]["work_rank"] == 1
    assert usage["codex"]["enabled"] is True and usage["codex"]["work_enabled"] is False


async def test_codex_usage_from_backend_with_reset_credits_and_reset(client, auth, monkeypatch):
    from ttakkari.runs import profiles, usage

    usage.clear_cache()
    cx = (await client.post("/api/accounts", json={"name": "cx-api", "engine": "codex"}, headers=auth)).json()

    async def fake_get(_d, path):
        if path == "usage":
            return {"plan_type": "plus", "rate_limit": {
                "primary_window": {"used_percent": 100, "limit_window_seconds": 18000, "reset_at": 1791731809},
                "secondary_window": {"used_percent": 25, "limit_window_seconds": 604800, "reset_at": 1792300604}},
                "rate_limit_reset_credits": {"available_count": 3}}
        return {"available_count": 3, "credits": [
            {"status": "available", "expires_at": "2026-10-29T18:45:40Z"},
            {"status": "available", "expires_at": "2026-10-22T20:19:22Z"},
            {"status": "used", "expires_at": "2026-10-01T00:00:00Z"}]}

    calls = []

    async def fake_post(_d, path, body):
        calls.append((path, body))
        return 200, {"code": "reset"}

    monkeypatch.setattr(usage, "_codex_get", fake_get)
    monkeypatch.setattr(usage, "_codex_post", fake_post)
    row = next(r for r in (await client.get("/api/accounts/usage", params={"refresh": True}, headers=auth)).json()
               if r["name"] == "cx-api")
    assert row["source"] == "api" and row["plan"] == "plus"
    assert [(w["key"], w["percent"]) for w in row["windows"]] == [("five_hour", 100), ("seven_day", 25)]
    assert row["reset_credits"]["available"] == 3
    # 쓸 수 있는 것 중 가장 먼저 만료되는 크레딧
    from datetime import UTC, datetime
    assert row["reset_credits"]["next_expires_at"] == int(datetime(2026, 10, 22, 20, 19, 22, tzinfo=UTC).timestamp())

    import uuid as _uuid
    profiles.cool_down(_uuid.UUID(cx["id"]))
    r = await client.post(f"/api/accounts/{cx['id']}/codex-reset", headers=auth)
    assert r.json() == {"outcome": "reset", "message": "재설정했어요. 한도가 바로 풀렸어요."}
    assert calls[0][0] == "rate-limit-reset-credits/consume" and calls[0][1]["redeem_request_id"]
    assert profiles.cooldown_remaining(_uuid.UUID(cx["id"])) == 0
    claude = (await _accounts(client, auth))[0]
    assert (await client.post(f"/api/accounts/{claude['id']}/codex-reset", headers=auth)).status_code == 400


async def test_antigravity_second_account_cannot_alias_host_login(client, auth):
    first = await client.post('/api/accounts', json={'name': 'agy-a', 'engine': 'antigravity'}, headers=auth)
    assert first.status_code == 201, first.text
    second = await client.post('/api/accounts', json={'name': 'agy-b', 'engine': 'antigravity'}, headers=auth)
    assert second.status_code == 409, second.text
    assert second.json()['code'] == 'antigravity_isolation_unsupported'
    assert [a['name'] for a in await _accounts(client, auth) if a['engine'] == 'antigravity'] == ['agy-a']


async def test_concurrent_antigravity_creation_cannot_alias_host_login(client, auth):
    import asyncio
    await _accounts(client, auth)
    responses = await asyncio.gather(*(
        client.post('/api/accounts', json={'name': f'agy-{i}', 'engine': 'antigravity'}, headers=auth)
        for i in range(2)
    ))
    assert sorted(r.status_code for r in responses) == [201, 409]
    assert len([a for a in await _accounts(client, auth) if a['engine'] == 'antigravity']) == 1
