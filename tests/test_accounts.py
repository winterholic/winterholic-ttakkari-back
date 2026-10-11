"""엔진 계정 관리(ADR 0006): 이름·켜기·우선순위가 실제 실행 순서를 바꾼다."""
from __future__ import annotations

from conftest import HOME, agent_calls
from helpers import run_to_end


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


async def test_create_rename_toggle_delete(client, auth, make_dir):
    d = make_dir()
    r = await client.post("/api/accounts", json={"name": "회사 계정", "engine": "claude", "config_dir": str(d)}, headers=auth)
    assert r.status_code == 201, r.text
    a = r.json()
    assert a["priority"] == 2 and a["config_dir"] == str(d)
    assert (await client.post("/api/accounts", json={"name": "회사 계정", "engine": "codex"}, headers=auth)).status_code == 409
    r = await client.patch(f"/api/accounts/{a['id']}", json={"name": "개인", "enabled": False}, headers=auth)
    assert r.json()["name"] == "개인" and r.json()["enabled"] is False
    assert (await client.delete(f"/api/accounts/{a['id']}", headers=auth)).status_code == 204
    assert [x["name"] for x in await _accounts(client, auth)] == ["claude", "codex"]


async def test_config_dir_validation(client, auth):
    bad = [{"config_dir": "relative/dir"}, {"config_dir": str(HOME / "x")}]
    for b in bad:
        r = await client.post("/api/accounts", json={"name": "x", "engine": "claude", **b}, headers=auth)
        assert r.status_code == 422, (b, r.text)


async def test_reorder_changes_fallback_order(client, auth, make_dir):
    accs = await _accounts(client, auth)
    r = await client.post("/api/accounts/reorder", json={"ids": [accs[1]["id"], accs[0]["id"]]}, headers=auth)
    assert [a["name"] for a in r.json()] == ["codex", "claude"]
    sess = await _session(client, auth, make_dir, kind="orchestrator")
    run = await run_to_end(client, auth, sess["id"], "hi")
    assert run["profile"] == "codex"
    assert (await client.post("/api/accounts/reorder", json={"ids": [accs[0]["id"]]}, headers=auth)).status_code == 400


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
    chat = await _session(client, auth, make_dir, engine="claude")
    run = await run_to_end(client, auth, chat["id"], "CLAUDE_LIMIT")
    # 두 claude 계정 모두 한도면 실패하지만, 두 번째 계정은 자기 폴더로 실행됐다.
    calls = [c for c in agent_calls() if c["engine"] == "claude"]
    assert [c["account_dir"] for c in calls] == [None, str(d)]
    assert run["profile"] == "claude-2"
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
    assert claude[claude.index("--model") + 1] == "opus" and "-m" not in codex


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


def test_reset_time_parsed_from_codex_limit_message():
    from datetime import datetime, timedelta

    from ttakkari.runs.profiles import reset_seconds_from

    when = datetime.now().astimezone() + timedelta(hours=3)
    msg = f"You’ve hit your usage limit. Upgrade to Pro ... or try again at {when:%b} {when.day}th, {when.year} {when:%I:%M %p}."
    left = reset_seconds_from(None, msg)
    assert left is not None and abs(left - 3 * 3600) < 120
    assert reset_seconds_from("no date here") is None


async def test_work_pool_sessions_use_only_work_accounts(client, auth, make_dir):
    from ttakkari.orchestrator.runtime import issue_token

    w = (await client.post("/api/accounts", json={"name": "회사 codex", "engine": "codex", "pool": "work"}, headers=auth)).json()
    assert w["pool"] == "work" and w["priority"] == 0
    # 회사 대화는 개인 계정(claude 1순위)을 건너뛰고 회사 계정만 쓴다.
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": "w", "root_path": str(root)}, headers=auth)).json()
    orch = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator", "pool": "work"},
                              headers=auth)).json()
    assert orch["pool"] == "work"
    run = await run_to_end(client, auth, orch["id"], "hi")
    assert run["profile"] == "회사 codex" and agent_calls()[-1]["engine"] == "codex"
    # 작업 세션도 회사 묶음을 물려받는다.
    wid = (await client.post("/api/orchestrator/workers", json={"title": "t", "prompt": "go"},
                             headers={"Authorization": f"Bearer {issue_token(orch['id'])}"})).json()["worker_id"]
    worker = (await client.get(f"/api/sessions/{wid}", headers=auth)).json()
    assert worker["pool"] == "work"
    # 개인 대화는 여전히 개인 계정.
    personal = await _session(client, auth, make_dir, kind="orchestrator")
    assert (await run_to_end(client, auth, personal["id"], "hi"))["profile"] == "claude"
    # 회사 대화 목록만 거르기
    ids = [s["id"] for s in (await client.get("/api/sessions", params={"pool": "work"}, headers=auth)).json()]
    assert ids == [orch["id"]]


async def test_work_pool_with_no_enabled_accounts_fails_clearly(client, auth, make_dir):
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": "w", "root_path": str(root)}, headers=auth)).json()
    s = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator", "pool": "work"},
                           headers=auth)).json()
    run = await run_to_end(client, auth, s["id"], "hi")
    assert run["status"] == "failed" and "회사" in run["error"]


async def test_move_account_between_pools_and_reorder_within_pool(client, auth):
    accs = await _accounts(client, auth)
    r = await client.patch(f"/api/accounts/{accs[1]['id']}", json={"pool": "work"}, headers=auth)
    assert r.json()["pool"] == "work"
    a2 = (await client.post("/api/accounts", json={"name": "회사2", "engine": "claude", "pool": "work"}, headers=auth)).json()
    r = await client.post("/api/accounts/reorder", params={"pool": "work"}, json={"ids": [a2["id"], accs[1]["id"]]}, headers=auth)
    assert r.status_code == 200 and [a["name"] for a in r.json()] == ["회사2", "codex"]
    assert (await client.post("/api/accounts/reorder", json={"ids": [accs[0]["id"], a2["id"]]}, headers=auth)).status_code == 400
    usage = (await client.get("/api/accounts/usage", headers=auth)).json()
    assert [(u["pool"], u["name"], u["rank"]) for u in usage] == [("personal", "claude", 1), ("work", "회사2", 1), ("work", "codex", 2)]


async def test_override_is_per_pool(client, auth, make_dir):
    from datetime import UTC, datetime, timedelta

    a = (await client.post("/api/accounts", json={"name": "회사 A", "engine": "claude", "pool": "work"}, headers=auth)).json()
    until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    accs = await _accounts(client, auth)
    personal_ids = [x["id"] for x in accs if x["pool"] == "personal"]
    assert (await client.put("/api/accounts/override", params={"pool": "work"}, json={"ids": [personal_ids[0]], "until": until},
                             headers=auth)).status_code == 400
    r = await client.put("/api/accounts/override", params={"pool": "work"}, json={"ids": [a["id"]], "until": until}, headers=auth)
    assert r.json()["active"]
    assert (await client.get("/api/accounts/override", headers=auth)).json()["active"] is False
    assert (await client.get("/api/accounts/override", params={"pool": "work"}, headers=auth)).json()["ids"] == [a["id"]]
