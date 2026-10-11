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
    assert run["status"] == "failed" and "켜진 claude 계정이 없습니다" in run["error"]


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
