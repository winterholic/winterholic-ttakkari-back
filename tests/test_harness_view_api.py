"""하네스 조회 API: 소유자 전용(401/403), 비밀 비노출, 검색·필터·상세, 레이어와 실제 주입 목록의 일치(ADR 0069)."""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from conftest import HOME
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.harness_view import service
from ttakkari.models import SessionKind
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import instructions as instructions_mod

pytestmark = [pytest.mark.security]

CANARIES = {
    "env": "CANARY-ENV-VALUE-111",
    "header": "CANARY-HEADER-222",
    "arg": "CANARY-ARG-333",
    "mcpenv": "CANARY-MCPENV-444",
    "cred": "CANARY-CRED-555",
    "history": "CANARY-HISTORY-666",
    "account": "CANARY-ACCOUNT-777",
    "toml": "CANARY-TOML-888",
    "auth": "CANARY-CODEX-AUTH-999",
    "oauth": "CANARY-AGY-OAUTH-000",
    "dotenv": "CANARY-DOTENV-123",
    "body": "ghp_" + "Z9y8X7w6V5" * 3,
}
ROUTES = ["/api/harness", "/api/harness/items", "/api/harness/items/0123456789abcd",
          "/api/harness/layers?session=lead&engine=claude"]


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "userhome"
    claude, codex, gemini, agents = home / ".claude", home / ".codex", home / ".gemini", home / ".agents"
    for sub in ("skills/alpha", "skills/dup-a", "dev-experts-skills/dev-x", "memory", "rule-cards"):
        (claude / sub).mkdir(parents=True)
    (claude / "skills/alpha/SKILL.md").write_text(
        f"---\nname: alpha\ndescription: 알파 스킬 설명 {CANARIES['body']}\n---\n본문 [링크](missing.md) `~/.claude/none/x.md`\n",
        encoding="utf-8")
    (claude / "skills/dup-a/SKILL.md").write_text("---\nname: alpha\ndescription: 중복\n---\nx", encoding="utf-8")
    (claude / "dev-experts-skills/dev-x/SKILL.md").write_text("---\nname: dev-x\n---\n개발", encoding="utf-8")
    (claude / "CLAUDE.md").write_text("# 지침\n" + "줄\n" * 450, encoding="utf-8")
    (claude / "memory/MEMORY.md").write_text("- [a](gone.md) — 없는 파일", encoding="utf-8")
    (claude / "rule-cards/git.md").write_text("# 커밋 규칙", encoding="utf-8")
    (claude / "settings.json").write_text(json.dumps({
        "env": {"MY_TOKEN": CANARIES["env"]},
        "permissions": {"defaultMode": "bypassPermissions"},
        "skillOverrides": {"alpha": "off", "ghost": "off"},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": f"python3 {claude}/scripts/missing.py",
                                       "timeout": 5}]}]},
        "mcpServers": {"web": {"command": "npx", "args": ["--api-key", CANARIES["arg"]],
                               "headers": {"Authorization": f"Bearer {CANARIES['header']}"},
                               "env": {"K": CANARIES["mcpenv"]}}},
        "enabledPlugins": {"p@m": True},
    }), encoding="utf-8")
    (claude / ".credentials.json").write_text(json.dumps({"token": CANARIES["cred"]}), encoding="utf-8")
    (claude / "history.jsonl").write_text(CANARIES["history"], encoding="utf-8")
    (claude / ".env").write_text(f"SECRET={CANARIES['dotenv']}", encoding="utf-8")
    (claude / ".claude.json").write_text(json.dumps({
        "mcpServers": {"user-mcp": {"command": "x", "env": {"A": CANARIES["mcpenv"]}}},
        "oauthAccount": {"emailAddress": CANARIES["account"]}}), encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({
        "mcpServers": {"home-mcp": {"command": "y", "env": {"A": CANARIES["mcpenv"]}}},
        "oauthAccount": {"emailAddress": CANARIES["account"]}, "userID": CANARIES["account"]}), encoding="utf-8")
    (home / "notes-outside.md").write_text(CANARIES["env"], encoding="utf-8")
    codex.mkdir()
    (codex / "AGENTS.md").write_text("codex 지침", encoding="utf-8")
    (codex / "auth.json").write_text(CANARIES["auth"], encoding="utf-8")
    (codex / "config.toml").write_text(
        'model = "gpt"\nproject_doc_fallback_filenames = ["CLAUDE.md"]\n'
        f'[mcp_servers.obs]\ncommand = "npx"\nbearer_token = "{CANARIES["toml"]}"\n[mcp_servers.obs.env]\nKEY = "{CANARIES["toml"]}"\n'
        f'[hooks.state."{codex}/hooks.json:stop:0:0"]\nenabled = false\n', encoding="utf-8")
    (codex / "hooks.json").write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo"}]}]}}),
                                      encoding="utf-8")
    (gemini / "antigravity-cli").mkdir(parents=True)
    (gemini / "antigravity-cli/antigravity-oauth-token").write_text(CANARIES["oauth"], encoding="utf-8")
    (gemini / "antigravity-cli/mcp/ttakkari").mkdir(parents=True)
    (gemini / "antigravity-cli/mcp/ttakkari/config.json").write_text(CANARIES["oauth"], encoding="utf-8")
    (agents / "skills/shared").mkdir(parents=True)
    (agents / "skills/shared/SKILL.md").write_text("---\nname: shared\ndescription: 공용\n---\nx", encoding="utf-8")
    (claude / "skills/shared").symlink_to(agents / "skills/shared", target_is_directory=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(CANARIES["env"], encoding="utf-8")
    (claude / "skills/escape").symlink_to(outside, target_is_directory=True)
    project = home / "development/proj"
    project.mkdir(parents=True)
    (project / "CLAUDE.md").write_text("프로젝트 지침", encoding="utf-8")
    monkeypatch.setattr(service, "user_home", lambda: home)
    monkeypatch.setattr(service, "project_roots", lambda: [home / "development"])
    service.reset_cache()
    yield home
    service.reset_cache()


def dump_all(data) -> str:
    return json.dumps(data, ensure_ascii=False)


@pytest.mark.parametrize("path", ROUTES)
async def test_owner_login_required(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"Authorization": "Bearer nope"})).status_code == 401


@pytest.mark.parametrize("path", ROUTES)
async def test_agent_session_token_cannot_read(client, path):
    agent = {"Authorization": f"Bearer {issue_token(uuid.uuid4())}"}
    assert (await client.get(path, headers=agent)).status_code in (401, 403)


async def test_no_secret_value_reaches_any_response(client, auth, fake_home):
    seen = []
    summary = (await client.get("/api/harness?refresh=true", headers=auth)).json()
    seen.append(summary)
    listing = (await client.get("/api/harness/items?limit=60", headers=auth)).json()
    seen.append(listing)
    ids = [i["id"] for i in listing["items"]]
    cursor = listing["next_cursor"]
    while cursor is not None:
        page = (await client.get("/api/harness/items", params={"limit": 60, "cursor": cursor}, headers=auth)).json()
        seen.append(page)
        ids += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
    for item_id in ids:
        r = await client.get(f"/api/harness/items/{item_id}", params={"length": 40000}, headers=auth)
        assert r.status_code == 200
        seen.append(r.json())
    for engine in ("claude", "codex", "antigravity", "muse", "qwen-local"):
        for session in ("desk", "lead", "worker", "orchestrator"):
            r = await client.get("/api/harness/layers", params={"session": session, "engine": engine}, headers=auth)
            assert r.status_code == 200
            seen.append(r.json())
    for term in CANARIES.values():
        r = await client.get("/api/harness/items", params={"q": term}, headers=auth)
        assert r.json()["total"] == 0, term
    text = dump_all(seen)
    for name, canary in CANARIES.items():
        assert canary not in text, f"{name} 비밀 값이 응답에 노출됨"
    assert "[REDACTED]" in text


async def test_user_scope_mcp_from_home_claude_json_shows_only_server_names(client, auth, fake_home):
    data = (await client.get("/api/harness/items", params={"category": "mcp", "q": "home-mcp"}, headers=auth)).json()
    assert data["total"] == 1 and data["items"][0]["name"] == "home-mcp"
    detail = (await client.get(f"/api/harness/items/{data['items'][0]['id']}", headers=auth)).json()
    assert json.loads(detail["body"])["env"] == {"A": "[REDACTED]"}


async def test_secret_files_are_not_listed_or_readable(client, auth, fake_home):
    data = (await client.get("/api/harness/items?limit=60", headers=auth)).json()
    paths = " ".join(i["path"] or "" for i in data["items"])
    for forbidden in (".credentials.json", "history.jsonl", "auth.json", "antigravity-oauth-token", ".env"):
        assert forbidden not in paths
    escape = [i for i in data["items"] if i["name"] == "escape"]
    assert not escape or not escape[0]["present"]


async def test_overview_counts_and_warnings(client, auth, fake_home):
    r = await client.get("/api/harness?refresh=true", headers=auth)
    assert r.status_code == 200
    body = r.json()
    cats = {c["id"]: c for c in body["categories"]}
    assert cats["skill"]["present"] >= 4 and cats["instruction"]["present"] >= 3
    assert cats["mcp"]["total"] > 90 and cats["guard"]["total"] > 20
    assert body["warning_kinds"]["broken_ref"] >= 3
    assert body["warning_kinds"]["duplicate"] >= 1 and body["warning_kinds"]["too_long"] >= 1
    messages = " ".join(w["message"] for w in body["warnings"])
    assert "깨진 참조" in messages and "훅 스크립트 없음" in messages and "중복 이름" in messages
    assert any(c["missing"] for c in body["categories"]), "없는 항목은 없음으로 센다"


async def test_search_filters_and_detail(client, auth, fake_home):
    r = await client.get("/api/harness/items", params={"q": "알파", "category": "skill"}, headers=auth)
    hits = r.json()["items"]
    assert [h["name"] for h in hits][:1] == ["alpha"] and "description" in hits[0]["matched_in"]
    body_hit = (await client.get("/api/harness/items", params={"q": "커밋 규칙"}, headers=auth)).json()["items"][0]
    assert body_hit["name"] == "git" and "body" in body_hit["matched_in"] and "커밋" in body_hit["snippet"]
    inactive = (await client.get("/api/harness/items", params={"category": "skill", "active": "false"},
                                 headers=auth)).json()["items"]
    assert any(i["name"] == "alpha" and "skillOverrides" in i["active_note"] for i in inactive)
    codex = (await client.get("/api/harness/items", params={"engine": "codex", "category": "hook"},
                              headers=auth)).json()["items"]
    assert codex and codex[0]["active"] is False and "hooks.state" in codex[0]["active_note"]
    detail = (await client.get(f"/api/harness/items/{hits[0]['id']}", headers=auth)).json()
    assert detail["frontmatter"]["name"] == "alpha" and detail["path"].endswith("skills/alpha/SKILL.md")
    assert detail["chars"] == len(detail["body"]) and detail["next_offset"] is None
    assert (await client.get("/api/harness/items/ffffffffffffff", headers=auth)).status_code == 404
    assert (await client.get("/api/harness/items/../../etc/passwd", headers=auth)).status_code in (404, 422)
    assert (await client.get("/api/harness/items", params={"category": "bogus"}, headers=auth)).status_code == 422
    assert (await client.get("/api/harness/items", params={"limit": 1000}, headers=auth)).status_code == 422


async def test_long_body_is_paged(client, auth, fake_home):
    hit = (await client.get("/api/harness/items", params={"q": "지침", "category": "instruction",
                                                          "engine": "claude", "scope": "global"}, headers=auth)).json()["items"][0]
    first = (await client.get(f"/api/harness/items/{hit['id']}", params={"length": 500}, headers=auth)).json()
    assert first["next_offset"] == 500 and len(first["body"]) == 500
    second = (await client.get(f"/api/harness/items/{hit['id']}", params={"offset": 500, "length": 500},
                               headers=auth)).json()
    assert second["offset"] == 500
    assert any("줄로 길다" in w for w in first["warnings"])


async def test_layers_match_the_real_injected_parts(client, auth, fake_home):
    from ttakkari.models import ChatSession
    from ttakkari.runs import manager

    instructions_mod.write("common", "공통 지침 본문")
    instructions_mod.write("lead", "기획 지침 본문")
    instructions_mod.write("worker", "작업 지침 본문")
    try:
        for session, kind, parent in (("lead", SessionKind.orchestrator, uuid.uuid4()),
                                      ("orchestrator", SessionKind.orchestrator, None),
                                      ("worker", SessionKind.worker, None)):
            sess = ChatSession()
            sess.kind, sess.parent_id = kind, parent
            async with sessionmaker()() as db:
                parts = await manager._system_note_parts(db, Path("<outbox>"), sess, scope="all")
            r = await client.get("/api/harness/layers", params={"session": session, "engine": "claude"}, headers=auth)
            layers = r.json()["layers"]
            applied = [(layer["id"], layer["chars"]) for layer in layers if layer["phase"] == "prompt" and layer["applies"]]
            assert applied == [(pid, len(text)) for pid, text in parts], session
            assert [layer["order"] for layer in layers] == list(range(1, len(layers) + 1))
        qwen = (await client.get("/api/harness/layers", params={"session": "worker", "engine": "qwen-local"},
                                 headers=auth)).json()
        assert not any(layer["applies"] for layer in qwen["layers"] if layer["id"] == "system_note")
        assert any(layer["id"] == "qwen_short_note" and layer["applies"] for layer in qwen["layers"])
        common = next(layer for layer in (await client.get(
            "/api/harness/layers", params={"session": "worker", "engine": "codex"}, headers=auth)).json()["layers"]
            if layer["id"] == "common_instructions")
        assert common["item_id"]
        detail = (await client.get(f"/api/harness/items/{common['item_id']}", headers=auth)).json()
        assert detail["body"] == "공통 지침 본문" and detail["source"] == "file"
    finally:
        for role in ("common", "lead", "worker"):
            instructions_mod.reset(role)
        service.reset_cache()


async def test_missing_instruction_layers_are_listed_as_not_applied(client, auth, fake_home):
    for role in ("common", "lead", "worker"):
        instructions_mod.reset(role)
    layers = (await client.get("/api/harness/layers", params={"session": "worker", "engine": "claude"},
                               headers=auth)).json()["layers"]
    by_id = {layer["id"]: layer for layer in layers}
    assert by_id["common_instructions"]["applies"] is False and by_id["role_instructions"]["applies"] is False
    assert by_id["pinned_brief"]["applies"] is False
    desk = (await client.get("/api/harness/layers", params={"session": "desk", "engine": "claude"},
                             headers=auth)).json()
    assert desk["layers"][0]["id"] == "desk_system" and desk["layers"][0]["origin"] == "code"


async def test_layers_reject_unknown_values(client, auth):
    assert (await client.get("/api/harness/layers", params={"session": "x", "engine": "claude"},
                             headers=auth)).status_code == 422
    assert (await client.get("/api/harness/layers", params={"session": "lead", "engine": "gpt"},
                             headers=auth)).status_code == 422


async def test_mcp_tool_items_match_server_tools(client, auth, fake_home):
    from ttakkari.orchestrator import mcp_server

    listed = await mcp_server.server.list_tools()
    names = {t.name for t in (listed[0] if isinstance(listed, tuple) else listed)}
    tools = {}
    cursor = 0
    while cursor is not None:
        page = (await client.get("/api/harness/items", params={"category": "mcp", "group": "따까리 MCP 도구",
                                                               "limit": 60, "cursor": cursor}, headers=auth)).json()
        tools.update({i["name"]: i for i in page["items"]})
        cursor = page["next_cursor"]
    assert set(tools) == names
    assert tools["spawn_worker"]["sessions"] == ["lead", "orchestrator"]
    assert tools["recall"]["sessions"] == ["lead", "worker", "orchestrator"]


async def test_snapshot_reads_db_accounts_without_paths_of_secrets(client, auth, fake_home):
    from ttakkari.models import EngineAccount

    async with sessionmaker()() as db:
        db.add(EngineAccount(name="내 클로드", engine="claude", config_dir=str(HOME / "accounts/none"), pool="personal"))
        await db.commit()
        assert (await db.scalars(select(EngineAccount))).first() is not None
    service.reset_cache()
    data = (await client.get("/api/harness/items", params={"category": "policy", "q": "내 클로드"}, headers=auth)).json()
    assert data["total"] == 1 and data["items"][0]["source"] == "db"


async def test_account_memory_is_listed_but_account_secrets_are_not(client, auth, fake_home):
    import shutil

    from ttakkari.models import EngineAccount

    acc_id = uuid.uuid4()
    base = HOME / "accounts" / str(acc_id)
    (base / "projects/-proj/memory").mkdir(parents=True)
    (base / "projects/-proj/memory/MEMORY.md").write_text("- [x](gone.md) — 기억 색인", encoding="utf-8")
    (base / ".credentials.json").write_text(CANARIES["cred"], encoding="utf-8")
    (base / "projects/-proj/transcript.jsonl").write_text(CANARIES["history"], encoding="utf-8")
    (base / "CLAUDE.md").symlink_to(fake_home / ".claude/CLAUDE.md")
    try:
        async with sessionmaker()() as db:
            db.add(EngineAccount(id=acc_id, name="계정A", engine="claude", config_dir=str(base), pool="personal"))
            await db.commit()
        service.reset_cache()
        memory = (await client.get("/api/harness/items", params={"category": "memory", "scope": "account"},
                                   headers=auth)).json()
        assert [m["name"] for m in memory["items"]] == ["MEMORY.md"]
        assert memory["items"][0]["group"] == "따까리 계정 계정A"
        assert any("깨진 참조" in w for w in memory["items"][0]["warnings"])
        acct = (await client.get("/api/harness/items", params={"category": "policy", "q": "계정A"}, headers=auth)).json()
        body = (await client.get(f"/api/harness/items/{acct['items'][0]['id']}", headers=auth)).json()["body"]
        assert "전역 파일과 같음(링크)" in body and "없음" in body
        everything = dump_all([memory, acct, body])
        assert CANARIES["cred"] not in everything and CANARIES["history"] not in everything
    finally:
        shutil.rmtree(base, ignore_errors=True)
        service.reset_cache()
