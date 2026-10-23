"""하네스 조회 2차(ADR 0070): 7개 그룹 분류, 프로젝트별 묶음, 새 수집 경로의 화이트리스트·마스킹·비밀 미열람."""
from __future__ import annotations

import json
import uuid

import pytest
from test_harness_view_api import dump_all, fake_home  # noqa: F401  (fixture 재사용)

from ttakkari.harness_view import service, taxonomy
from ttakkari.orchestrator.runtime import issue_token

pytestmark = [pytest.mark.security]

C = {
    "muse_auth": "CANARY-MUSE-AUTH-101",
    "qwen_env": "CANARY-QWEN-ENV-202",
    "qwen_profile": "CANARY-QWEN-PROFILE-303",
    "proj_env": "CANARY-PROJ-ENV-404",
    "proj_mcp": "CANARY-PROJ-MCP-505",
    "proj_local": "CANARY-PROJ-LOCAL-606",
    "proj_dotenv": "CANARY-PROJ-DOTENV-707",
    "agy_mcp": "CANARY-AGY-MCP-808",
    "escape": "CANARY-PROJ-ESCAPE-909",
    "conf": "CANARY-QWEN-CONF-111",
}
ROUTES = ["/api/harness/groups/claude", "/api/harness/projects", "/api/harness/projects/detail?key=projects/alpha"]
RUNTIME_PY = '''import json


def build_claude_argv(claude, settings_path, prompt, *, turns=20):
    agents = json.dumps({"qwen-worker": {"description": "작은 작업 담당", "tools": ["Read", "Edit"], "maxTurns": turns}})
    argv = [claude, "--bare", "--setting-sources", "user", "--strict-mcp-config", "--agents", agents, "--tools", "Read"]
    return argv
'''


@pytest.fixture
def rich_home(fake_home, tmp_path):  # noqa: F811
    home = fake_home
    (home / ".codex/rules").mkdir()
    (home / ".codex/rules/default.rules").write_text('prefix_rule(pattern=["rm"], decision="forbidden")', encoding="utf-8")
    (home / ".gemini/config").mkdir()
    (home / ".gemini/GEMINI.md").write_text("제미니 지침", encoding="utf-8")
    (home / ".gemini/config/mcp_config.json").write_text(json.dumps(
        {"mcpServers": {"agy-web": {"command": "npx", "env": {"K": C["agy_mcp"]}}}}), encoding="utf-8")
    muse = home / ".config/muse"
    muse.mkdir(parents=True)
    (muse / "settings.json").write_text(json.dumps({"schema_version": 1, "tui": {"theme": "dark"}}), encoding="utf-8")
    (muse / "trust.json").write_text(json.dumps({"schema_version": 1, "projects": {"/x": True}}), encoding="utf-8")
    (muse / "auth.json").write_text(json.dumps({"token": C["muse_auth"]}), encoding="utf-8")
    plug = home / ".local/share/muse/plugins/cache/builtin/muse-core/plan/0123abcd"
    plug.mkdir(parents=True)
    (plug / "SKILL.md").write_text("---\nname: plan\ndescription: 계획 스킬\n---\n본문", encoding="utf-8")
    qwen = home / "development/projects/qwen-local-harness"
    for sub in ("prompts", "skills/qwen-local-delegate", "config", ".runtime/profile", "src/qwen_local"):
        (qwen / sub).mkdir(parents=True)
    (qwen / "AGENTS.md").write_text("# 하네스 개발 지침", encoding="utf-8")
    (qwen / "prompts/worker.md").write_text("너는 로컬 작업자다.", encoding="utf-8")
    (qwen / "skills/qwen-local-delegate/SKILL.md").write_text("---\nname: qwen-local-delegate\ndescription: 위임\n---\nx",
                                                                 encoding="utf-8")
    (qwen / "config/harness.conf").write_text(f"QWEN_LOCAL_PORT=18990\n# token {C['conf']}\n", encoding="utf-8")
    (qwen / ".runtime/settings.json").write_text(json.dumps(
        {"env": {"ANTHROPIC_AUTH_TOKEN": C["qwen_env"], "ANTHROPIC_MODEL": "m"}}), encoding="utf-8")
    (qwen / ".runtime/profile/.claude.json").write_text(C["qwen_profile"], encoding="utf-8")
    (qwen / "src/qwen_local/runtime.py").write_text(RUNTIME_PY, encoding="utf-8")
    dev = home / "development"
    alpha = dev / "projects/alpha"
    (alpha / ".claude/skills/s1").mkdir(parents=True)
    (alpha / ".claude/agents").mkdir()
    (alpha / ".agents/rules").mkdir(parents=True)
    (alpha / "back").mkdir()
    (alpha / "CLAUDE.md").write_text("# alpha 지침", encoding="utf-8")
    (alpha / "AGENTS.md").write_text("# alpha agents", encoding="utf-8")
    (alpha / "back/AGENTS.md").write_text("# 하위 폴더 지침", encoding="utf-8")
    (alpha / ".claude/skills/s1/SKILL.md").write_text("---\nname: s1\ndescription: 프로젝트 스킬\n---\nx", encoding="utf-8")
    (alpha / ".claude/agents/reviewer.md").write_text("---\nname: reviewer\ndescription: 리뷰어\n---\nx", encoding="utf-8")
    (alpha / ".claude/settings.json").write_text(json.dumps({
        "env": {"MY_TOKEN": C["proj_env"]}, "permissions": {"allow": ["Bash(ls)"]},
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo done"}]}]}}), encoding="utf-8")
    (alpha / ".claude/settings.local.json").write_text(json.dumps({"env": {"K": C["proj_local"]}}), encoding="utf-8")
    (alpha / ".agents/rules/r.md").write_text("# 공용 규칙", encoding="utf-8")
    (alpha / ".mcp.json").write_text(json.dumps(
        {"mcpServers": {"p-mcp": {"command": "npx", "args": ["--api-key", C["proj_mcp"]], "env": {"A": C["proj_mcp"]}}}}),
        encoding="utf-8")
    (alpha / ".env").write_text(f"SECRET={C['proj_dotenv']}", encoding="utf-8")
    outside = tmp_path / "proj-outside"
    (outside / "SKILL.md").parent.mkdir()
    (outside / "SKILL.md").write_text(C["escape"], encoding="utf-8")
    (alpha / ".claude/skills/escape").symlink_to(outside, target_is_directory=True)
    corp = dev / "hyperez/corp"
    corp.mkdir(parents=True)
    (corp / "CLAUDE.md").write_text("# 회사 지침", encoding="utf-8")
    (dev / "projects/wt").mkdir(parents=True)
    (dev / "projects/wt/.git").write_text("gitdir: /elsewhere", encoding="utf-8")  # git worktree 는 건너뛴다
    (dev / "projects/wt/CLAUDE.md").write_text("워크트리 지침", encoding="utf-8")
    (dev / "projects/bare").mkdir(parents=True)
    service.reset_cache()
    return home


async def all_items(client, auth, **params) -> list[dict]:
    rows: list[dict] = []
    cursor: int | None = 0
    while cursor is not None:
        page = (await client.get("/api/harness/items", params={"limit": 60, "cursor": cursor, **params}, headers=auth)).json()
        rows += page["items"]
        cursor = page["next_cursor"]
    return rows


@pytest.mark.parametrize("path", ROUTES)
async def test_group_routes_require_owner_login(client, path):
    assert (await client.get(path)).status_code == 401
    assert (await client.get(path, headers={"Authorization": "Bearer nope"})).status_code == 401
    agent = {"Authorization": f"Bearer {issue_token(uuid.uuid4())}"}
    assert (await client.get(path, headers=agent)).status_code in (401, 403)


async def test_every_item_belongs_to_exactly_one_group(client, auth, rich_home):
    summary = (await client.get("/api/harness?refresh=true", headers=auth)).json()
    rows = await all_items(client, auth)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids)) == summary["total"]
    assert {r["hgroup"] for r in rows} <= set(taxonomy.GROUPS)
    assert all(r["concern"] in taxonomy.CONCERNS and r["layer"] for r in rows)
    cards = {g["id"]: g for g in summary["groups"]}
    assert list(cards) == list(taxonomy.GROUP_ORDER) and len(cards) == 7
    assert sum(g["total"] for g in cards.values()) == summary["total"]
    for gid, card in cards.items():
        assert card["total"] == sum(1 for r in rows if r["hgroup"] == gid), gid
        assert card["present"] + card["missing"] == card["total"]
    snap = service._cache
    assert snap is not None and taxonomy.validate(snap.items) == []
    # 레이어는 그 그룹이 정의한 것만 쓴다
    for r in rows:
        assert r["layer"] in {ld.id for ld in taxonomy.GROUP_LAYERS[r["hgroup"]]}, (r["name"], r["hgroup"], r["layer"])


async def test_group_membership_rules(client, auth, rich_home):
    await client.get("/api/harness?refresh=true", headers=auth)
    rows = await all_items(client, auth)

    def find(name, **kw):
        hits = [r for r in rows if r["name"] == name and all(r[k] == v for k, v in kw.items())]
        assert hits, (name, kw, [(r["name"], r["hgroup"], r["concern"]) for r in rows if r["name"] == name])
        return hits[0]

    assert find("CLAUDE.md", engine="claude", scope="global")["hgroup"] == "claude"
    assert find("CLAUDE.md", engine="claude", scope="global")["layer"] == "user"
    assert find("AGENTS.md", engine="codex", scope="global")["hgroup"] == "codex"
    assert find("GEMINI.md", engine="antigravity")["hgroup"] == "antigravity"
    assert find("default.rules")["concern"] == "setting"  # 실행 규칙은 권한
    assert find("shared", engine="shared", category="skill")["hgroup"] == "antigravity"
    assert find("agy-web")["hgroup"] == "antigravity" and find("agy-web")["concern"] == "mcp"
    assert find("settings.json", hgroup="muse")["layer"] == "user"
    assert find("auth.json (로그인 정보)")["hgroup"] == "muse"
    assert find("plan", hgroup="muse")["layer"] == "builtin" and find("plan", hgroup="muse")["concern"] == "skill"
    assert find("muse-core", hgroup="muse")["concern"] == "plugin"
    assert find("PreToolUse 가드 훅(Run 마다 주입)")["layer"] == "session"
    assert find("AGENTS.md", hgroup="qwen")["concern"] == "instruction"
    assert find("prompts/worker.md")["hgroup"] == "qwen"
    assert find("qwen-local-delegate", hgroup="qwen")["concern"] == "skill"
    assert find("qwen-worker")["concern"] == "agent" and find("qwen-worker")["layer"] == "runtime"
    assert find("Qwen 작업자 · 짧은 계약")["hgroup"] == "qwen" and find("Qwen 작업자 · 짧은 계약")["layer"] == "session"
    assert find("설정 · Qwen")["hgroup"] == "qwen" and find("Qwen 작업자 활성화(DB)")["hgroup"] == "qwen"
    assert find("공통 지침(접수 제외 모든 실행)")["hgroup"] == "ttakkari"
    assert find("접수 Qwen · KIND_SYSTEM")["hgroup"] == "ttakkari"
    assert find("SYSTEM_NOTE")["layer"] == "code" and find("SYSTEM_NOTE")["concern"] == "prompt"
    assert find("차단 명령 이름")["hgroup"] == "ttakkari" and find("차단 명령 이름")["concern"] == "guard"
    assert find("claude 실행 환경")["concern"] == "capability"
    assert find("spawn_worker")["hgroup"] == "ttakkari" and find("spawn_worker")["concern"] == "mcp"
    # Qwen 하네스 저장소는 프로젝트별 그룹에 중복으로 나오지 않는다
    assert not [r for r in rows if r["hgroup"] == "projects" and "qwen-local-harness" in (r["path"] or "")]
    # 없는 항목은 없음과 근거를 보여 준다
    none_hook = find("Qwen 하네스 훅")
    assert none_hook["present"] is False and "훅" in none_hook["description"]
    assert find("Muse 전역 지침 파일")["present"] is False


async def test_muse_and_qwen_never_expose_login_or_profile_state(client, auth, rich_home):
    await client.get("/api/harness?refresh=true", headers=auth)
    rows = await all_items(client, auth)
    paths = " ".join(r["path"] or "" for r in rows)
    assert ".runtime/profile" not in paths and "oauth" not in paths.lower()
    presence = next(r for r in rows if r["name"] == "auth.json (로그인 정보)")
    detail = (await client.get(f"/api/harness/items/{presence['id']}", headers=auth)).json()
    assert detail["body"] == "" and "읽지 않음" in detail["active_note"]
    agent = next(r for r in rows if r["name"] == "qwen-worker")
    body = (await client.get(f"/api/harness/items/{agent['id']}", headers=auth)).json()["body"]
    assert "작은 작업 담당" in body and "Edit" in body
    argv = next(r for r in rows if r["name"].startswith("Claude Code 실행 옵션"))
    cmd = json.loads((await client.get(f"/api/harness/items/{argv['id']}", headers=auth)).json()["body"])["argv"]
    assert "--bare" in cmd and "--strict-mcp-config" in cmd and "<claude>" in cmd
    generated = next(r for r in rows if r["name"].startswith(".runtime/settings.json"))
    gen_body = json.loads((await client.get(f"/api/harness/items/{generated['id']}", headers=auth)).json()["body"])
    assert gen_body["env"] == {"ANTHROPIC_AUTH_TOKEN": "[REDACTED]", "ANTHROPIC_MODEL": "[REDACTED]"}


async def test_projects_are_grouped_per_project_then_engine_then_concern(client, auth, rich_home):
    listing = (await client.get("/api/harness/projects", headers=auth)).json()
    by_key = {p["key"]: p for p in listing["projects"]}
    assert set(by_key) == {"projects/alpha", "hyperez/corp", "proj"}
    assert by_key["hyperez/corp"]["pool"] == "company" and by_key["projects/alpha"]["pool"] == "personal"
    assert listing["company"] == 1 and listing["total"] == 3
    # 하네스 파일이 없는 프로젝트(bare)와 git worktree(wt, 건너뜀)는 목록에 없고 "하네스 없음"에 있다
    assert "projects/bare" in listing["without_harness"] and "projects/wt" not in by_key
    assert listing["projects"][0]["pool"] == "company"  # 회사 프로젝트가 먼저
    detail = (await client.get("/api/harness/projects/detail", params={"key": "projects/alpha"}, headers=auth)).json()
    engines = {e["id"]: e for e in detail["engines"]}
    assert set(engines) == {"claude", "codex", "shared"}
    claude = {c["id"]: c for c in engines["claude"]["concerns"]}
    assert {"instruction", "skill", "agent", "setting", "hook", "mcp"} <= set(claude)
    skills = {i["name"]: i for i in claude["skill"]["items"]}
    assert skills["s1"]["layer"] == "root" and skills["s1"]["present"]
    assert not skills["escape"]["present"], "허용 밖을 가리키는 심볼릭 링크는 읽지 않는다"
    codex_md = {c["id"]: c for c in engines["codex"]["concerns"]}["instruction"]["items"]
    assert [i["layer"] for i in codex_md] == ["root", "nested"], "루트가 먼저, 하위 폴더가 그 위에 얹힌다"
    assert {i["name"] for i in {c["id"]: c for c in engines["shared"]["concerns"]}["instruction"]["items"]} == {"r.md"}
    every = [i for e in detail["engines"] for c in e["concerns"] for i in c["items"]]
    assert all(i["hgroup"] == "projects" and i["project"] == "projects/alpha" and i["pool"] == "personal" for i in every)
    # 프로젝트 필터 검색과 범위 배지
    corp = await all_items(client, auth, hgroup="projects", pool="company")
    assert {r["project"] for r in corp} == {"hyperez/corp"}
    assert (await client.get("/api/harness/projects/detail", params={"key": "nope/none"}, headers=auth)).status_code == 404
    for bad in ("../../etc", "projects/../x", "/etc/passwd", "a/b/c", ""):
        assert (await client.get("/api/harness/projects/detail", params={"key": bad}, headers=auth)).status_code == 422, bad
    # 프로젝트 카드의 수는 목록과 같다
    cards = {g["id"]: g for g in (await client.get("/api/harness", headers=auth)).json()["groups"]}
    assert cards["projects"]["projects"] == 3 and cards["projects"]["company_projects"] == 1
    assert cards["projects"]["total"] == sum(p["total"] for p in listing["projects"])


async def test_no_canary_reaches_group_project_or_search_responses(client, auth, rich_home):
    seen = [(await client.get("/api/harness?refresh=true", headers=auth)).json()]
    for gid in taxonomy.GROUPS:
        r = await client.get(f"/api/harness/groups/{gid}", headers=auth)
        assert r.status_code == 200, gid
        seen.append(r.json())
    listing = (await client.get("/api/harness/projects", headers=auth)).json()
    seen.append(listing)
    for p in listing["projects"]:
        seen.append((await client.get("/api/harness/projects/detail", params={"key": p["key"]}, headers=auth)).json())
    rows = await all_items(client, auth)
    seen.append(rows)
    for r in rows:
        d = await client.get(f"/api/harness/items/{r['id']}", params={"length": 40000}, headers=auth)
        assert d.status_code == 200
        seen.append(d.json())
    for term in C.values():
        assert (await client.get("/api/harness/items", params={"q": term}, headers=auth)).json()["total"] == 0, term
    text = dump_all(seen)
    for name, canary in C.items():
        assert canary not in text, f"{name} 비밀 값이 응답에 노출됨"
    assert "[REDACTED]" in text
    assert not [r for r in rows if (r["path"] or "").endswith("/.env")]
    assert "escape" not in {r["name"] for r in rows if r["present"]}


async def test_group_detail_layers_and_notes(client, auth, rich_home):
    claude = (await client.get("/api/harness/groups/claude", headers=auth)).json()
    layers = {layer["id"]: layer for layer in claude["layers"]}
    assert [layer["id"] for layer in claude["layers"]] == ["user", "account", "project", "session"]
    assert [layer["order"] for layer in claude["layers"]] == [1, 2, 3, 4]
    assert layers["user"]["evidence"] == "external" and layers["session"]["evidence"] == "code"
    assert layers["user"]["total"] > 0 and layers["project"]["link"]["group"] == "projects"
    assert layers["project"]["link"]["projects"] >= 1
    phases = {i["phase"] for i in layers["session"]["injection"]}
    assert {"prompt", "delivery", "guard", "tool"} <= phases and "load" not in phases
    assert [c["id"] for c in claude["concerns"]] == [c for c in taxonomy.CONCERN_ORDER if c in {x["id"] for x in claude["concerns"]}]
    qwen = (await client.get("/api/harness/groups/qwen", headers=auth)).json()
    qlayers = {layer["id"]: layer for layer in qwen["layers"]}
    assert qlayers["project"]["applies"] is False and qlayers["harness"]["evidence"] == "code"
    assert {"instruction", "skill", "agent", "setting", "hook", "mcp"} <= {c["id"] for c in qwen["concerns"]}
    hook = next(c for c in qwen["concerns"] if c["id"] == "hook")
    assert hook["exists"] is False  # 없음과 근거만 있는 관심사
    ttakkari = (await client.get("/api/harness/groups/ttakkari", headers=auth)).json()
    assert [layer["id"] for layer in ttakkari["layers"]] == ["code", "config", "file", "db", "measured"]
    assert {"prompt", "policy", "guard", "mcp", "capability"} <= {c["id"] for c in ttakkari["concerns"]}
    projects = (await client.get("/api/harness/groups/projects", headers=auth)).json()
    assert [layer["id"] for layer in projects["layers"]] == ["root", "nested"]
    assert (await client.get("/api/harness/groups/nope", headers=auth)).status_code == 422


async def test_search_filters_by_group_concern_layer_and_returns_breadcrumb(client, auth, rich_home):
    rows = await all_items(client, auth, hgroup="claude", concern="skill")
    assert rows and all(r["hgroup"] == "claude" and r["concern"] == "skill" for r in rows)
    assert rows[0]["crumb"] == [{"kind": "group", "id": "claude", "label": "Claude 하네스"},
                                {"kind": "concern", "id": "skill", "label": "스킬"}]
    found = (await client.get("/api/harness/items", params={"q": "프로젝트 스킬"}, headers=auth)).json()["items"]
    hit = next(r for r in found if r["name"] == "s1")
    assert [c["kind"] for c in hit["crumb"]] == ["group", "project", "engine", "concern"]
    assert hit["crumb"][1]["label"] == "alpha" and hit["crumb"][0]["id"] == "projects"
    nested = await all_items(client, auth, hgroup="projects", project="projects/alpha", layer="nested")
    assert {r["name"] for r in nested} == {"AGENTS.md"} and nested[0]["group"] == "projects/alpha/back"
    for bad in ({"hgroup": "x"}, {"concern": "x"}, {"layer": "x"}, {"project": "../x"}, {"pool": "x"}):
        assert (await client.get("/api/harness/items", params=bad, headers=auth)).status_code == 422, bad


async def test_detail_has_breadcrumb_layer_and_counterparts(client, auth, rich_home):
    rows = await all_items(client, auth, q="지침")
    claude_md = next(r for r in rows if r["name"] == "CLAUDE.md" and r["hgroup"] == "claude")
    detail = (await client.get(f"/api/harness/items/{claude_md['id']}", headers=auth)).json()
    assert detail["layer_label"] == "사용자 전역" and detail["crumb"][0]["id"] == "claude"
    peers: dict[str, list[dict]] = {}
    for p in detail["counterparts"]["items"]:
        peers.setdefault(p["hgroup"], []).append(p)
    assert {"codex", "antigravity"} <= set(peers)
    assert peers["codex"][0]["name"] == "AGENTS.md" and peers["codex"][0]["present"]
    assert "GEMINI.md" in {p["name"] for p in peers["antigravity"]}
    # 스킬처럼 수십 개인 관심사는 항목 대신 관심사 링크만 준다
    skill = next(r for r in await all_items(client, auth, hgroup="claude", concern="skill") if r["name"] == "alpha")
    sk = (await client.get(f"/api/harness/items/{skill['id']}", headers=auth)).json()
    assert sk["counterparts"]["items"] == [] and any(c["concern"] == "skill" for c in sk["counterparts"]["concerns"])
    # 프로젝트 안에서는 같은 레이어의 다른 엔진 항목
    proj = next(r for r in await all_items(client, auth, hgroup="projects", project="projects/alpha", layer="root")
                if r["name"] == "CLAUDE.md")
    pd = (await client.get(f"/api/harness/items/{proj['id']}", headers=auth)).json()
    assert {(p["label"], p["name"]) for p in pd["counterparts"]["items"]} == {("Codex", "AGENTS.md"), ("공용(.agents)", "r.md")}
    ttakkari = next(r for r in await all_items(client, auth, hgroup="ttakkari", concern="guard"))
    assert (await client.get(f"/api/harness/items/{ttakkari['id']}", headers=auth)).json()["counterparts"] == {
        "items": [], "concerns": []}


def test_project_locator_rules(tmp_path):
    from ttakkari.harness_view.projects import locate

    root = tmp_path / "development"
    assert locate(root, root / "projects/aip").key == "projects/aip"
    assert locate(root, root / "projects/aip/back").sub == "back"
    corp = locate(root, root / "hyperez/ezwork/ezwork.hyperez.io")
    assert (corp.key, corp.sub, corp.pool) == ("hyperez/ezwork", "ezwork.hyperez.io", "company")
    other = locate(root, root / "tooling/muse")
    assert (other.key, other.sub, other.pool) == ("tooling", "muse", "personal")
    assert locate(root, root).key == "development"
