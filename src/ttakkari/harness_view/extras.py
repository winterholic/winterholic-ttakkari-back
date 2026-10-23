"""Muse·Qwen Code 하네스와 Antigravity·Codex 보강 수집(ADR 0070).

위치는 코드·파일에서 확인한 것만 읽는다. 어댑터(agents/muse.py·agents/qwen_local.py)와 엔진 폴더의 실제 구조가 근거이고,
찾지 못한 관심사는 "없음"과 근거 문구를 항목으로 남긴다. 로그인 파일(auth.json)은 존재만 확인하고 열지 않는다.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
from typing import Any

from ttakkari.harness_view.model import Item
from ttakkari.harness_view.safe import Denied, mask_text
from ttakkari.harness_view.sources import (
    Ctx,
    _mcp_items,
    _missing,
    _mtime,
    _read_json,
    file_item,
    json_item,
    mark_aliases,
    text_item,
)

QWEN_REL = Path("development/projects/qwen-local-harness")
MUSE_SESSIONS = ("lead", "worker", "orchestrator")


def muse_config(ctx: Ctx) -> Path:
    return ctx.user_home / ".config/muse"


def muse_data(ctx: Ctx) -> Path:
    return ctx.user_home / ".local/share/muse"


def qwen_root(user_home: Path, worker_path: Path | None) -> Path:
    """Qwen 하네스 저장소 루트. 설정의 qwen-worker 경로(<root>/bin/qwen-worker)가 홈 안일 때만 따른다."""
    if worker_path is not None:
        try:
            worker_path.resolve().relative_to(user_home.resolve())
            return worker_path.parents[1]
        except (ValueError, IndexError):
            pass
    return user_home / QWEN_REL


def _tag(item: Item, group: str, layer: str, concern: str = "") -> Item:
    item.hint_group, item.hint_layer = group, layer
    if concern:
        item.hint_concern = concern
    return item


def _none(ctx: Ctx, category: str, name: str, key: str, group: str, layer: str, why: str, *, scope: str = "global",
          engine: str, path: str = "") -> Item:
    """파일이 없다는 사실과 그렇게 보는 근거를 항목으로 남긴다."""
    item = Item(category=category, name=name, key=f"none|{key}", scope=scope, engine=engine, group=group,
                description=why, path=path or None, present=False, active=False, active_note="없음", kind="text")
    item.body = why
    item.hint_layer = layer
    return item


# ---------------------------------------------------------------- Muse

def collect_muse(ctx: Ctx) -> list[Item]:
    cfg, data = muse_config(ctx), muse_data(ctx)
    out: list[Item] = []

    def tag(item: Item, layer: str, concern: str = "") -> Item:
        return _tag(item, "muse", layer, concern)

    settings_path = cfg / "settings.json"
    if settings_path.exists():
        doc = _read_json(ctx, settings_path)
        out.append(tag(json_item(
            "setting", "settings.json", f"{settings_path}|all", doc, scope="global", engine="muse", group="Muse 설정",
            description="모델 추론 강도·TUI 설정(저장된 값). 로그인·신뢰 정보는 다른 파일", path=ctx.disp(settings_path),
            mtime=_mtime(settings_path), active=True,
            active_note="따까리는 managed_hooks_path 가 있으면 실행을 거부한다(agents/muse.py)"), "user"))
    else:
        out.append(tag(_missing(ctx, "setting", "settings.json", settings_path, scope="global", engine="muse",
                                description="Muse 설정 파일 없음", group="Muse 설정"), "user"))
    trust = cfg / "trust.json"
    if trust.exists():
        doc = _read_json(ctx, trust)
        projects = doc.get("projects") if isinstance(doc, dict) and isinstance(doc.get("projects"), dict) else {}
        out.append(tag(json_item(
            "setting", "trust.json (신뢰한 프로젝트)", f"{trust}|all", doc, scope="global", engine="muse",
            group="Muse 설정", description=f"신뢰한 프로젝트 {len(projects)}개", path=ctx.disp(trust),
            mtime=_mtime(trust), active=True), "user"))
    auth = cfg / "auth.json"
    if ctx.files.exists(auth):
        item = Item(category="setting", name="auth.json (로그인 정보)", key=f"{auth}|presence", scope="global",
                    engine="muse", group="Muse 설정", description="로그인 토큰 파일. 존재만 확인하고 열지 않는다.",
                    path=ctx.disp(auth), active=True, active_note="비밀 파일이라 읽지 않음(존재만 확인)", kind="text")
        item.mtime = _mtime(auth)
        out.append(tag(item, "user"))
    out.append(tag(_none(
        ctx, "instruction", "Muse 전역 지침 파일", "muse|instruction", "Muse 지침", "user",
        "~/.config/muse 와 ~/.local/share/muse 에 지침(규칙) 파일이 없다. Muse 의 규칙은 `/rules import` 로 "
        "가져오는 방식인데 저장 위치는 코드·파일에서 확인되지 않았다(확인 필요). 따까리는 지침을 spec.json 의 "
        "system_note 로 사용자 입력 앞에 붙여 전달한다(세션 주입 레이어).", engine="muse",
        path=ctx.disp(cfg)), "user"))
    if not (cfg / "skills").is_dir():
        out.append(tag(_none(
            ctx, "skill", "Muse 개인 스킬", "muse|skills", "Muse 스킬", "user",
            "~/.config/muse/skills 폴더가 없다(개인 스킬은 `$CONFIG_DIR/skills/<id>` 로 설치된다). 아래 번들 스킬만 있다.",
            engine="muse", path=ctx.disp(cfg / "skills")), "user"))
    # 번들 플러그인과 그 스킬(muse-core·threejs). 플러그인 캐시가 실제로 읽히는 쪽이라 여기만 센다.
    plugins_root = data / "plugins/cache/builtin"
    plugin_names: list[str] = []
    try:
        plugin_names = sorted(e.name for e in os.scandir(plugins_root) if e.is_dir(follow_symlinks=False))
    except OSError:
        pass
    # 캐시 구조: builtin/<플러그인>/<스킬>/<내용 해시>/SKILL.md. 같은 스킬의 해시가 여럿이면 가장 최근 것만 쓴다.
    latest: dict[tuple[str, str], Path] = {}
    for p in sorted(ctx.files.glob(data, "plugins/cache/builtin/*/*/*/SKILL.md")):
        key = (p.parent.parent.parent.name, p.parent.parent.name)
        if key not in latest or (_mtime(p) or 0) > (_mtime(latest[key]) or 0):
            latest[key] = p
    per_plugin: dict[str, int] = {}
    for (plugin, skill_name), p in sorted(latest.items()):
        per_plugin[plugin] = per_plugin.get(plugin, 0) + 1
        item = file_item(ctx, p, "skill", name=skill_name, scope="global", engine="muse", group=f"번들 · {plugin}")
        item.active, item.active_note = True, "번들 플러그인이 제공하는 스킬"
        out.append(tag(item, "builtin"))
    for name in plugin_names:
        out.append(tag(json_item(
            "plugin", name, f"{plugins_root}|{name}", {"name": name, "source": "builtin", "skills": per_plugin.get(name, 0)},
            scope="global", engine="muse", group="Muse 번들 플러그인", path=ctx.disp(plugins_root / name),
            description=f"번들 플러그인 · 스킬 {per_plugin.get(name, 0)}개", active=True,
            active_note="Muse 와 함께 설치된 플러그인(수정 불가)"), "builtin"))
    if not plugin_names:
        out.append(tag(_missing(ctx, "plugin", "Muse 플러그인", plugins_root, scope="global", engine="muse",
                                description="번들 플러그인 폴더 없음", group="Muse 번들 플러그인"), "builtin"))
    hook = json_item(
        "hook", "PreToolUse 가드 훅(Run 마다 주입)", "muse|injected-hook",
        {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "<python> security/muse_hook.py <policy.json>",
                                              "timeout": 10}]}]},
         "policy": ["write_roots", "protected(~/.ttakkari)", "private_root", "cwd", "mcp_servers", "role"]},
        scope="ttakkari", engine="muse", group="따까리 주입", source="code",
        path="back/src/ttakkari/agents/muse.py · back/.muse-runtime/bridge.mjs",
        description="브리지가 Run 마다 임시 hooks.json 을 만들어 managed 훅으로 등록한다. 사용자 훅은 쓰지 않는다.",
        sessions=MUSE_SESSIONS, active=True, active_note="managed 훅이 안 보이면 실행을 중단(bridge.mjs)")
    out.append(tag(hook, "session"))
    out.append(tag(_none(
        ctx, "hook", "Muse 사용자 훅", "muse|user-hook", "Muse 훅", "user",
        "settings.json 에 managed_hooks_path 가 없다. 사용자가 이미 등록한 훅이 있으면 따까리는 합성하지 않고 "
        "실행을 거부한다(agents/muse.py).", engine="muse", path=ctx.disp(settings_path)), "user"))
    mcp = json_item(
        "mcp", "ttakkari MCP (Run 마다 spec.mcp_servers)", "muse|injected-mcp",
        {"servers": "spec.mcp_servers", "role": "agent | orchestrator", "env": {"TTK_ORCH_TOKEN": "세션·Run 한정 토큰"}},
        scope="ttakkari", engine="muse", group="따까리 주입", source="code",
        path="back/src/ttakkari/agents/muse.py", sessions=MUSE_SESSIONS, active=True,
        description="Muse SDK 브리지에 따까리 MCP 서버를 넘긴다. 사용자 전역 MCP 설정은 없다.")
    mcp.meta["secret_keys"] = ["TTK_ORCH_TOKEN"]
    out.append(tag(mcp, "session"))
    out.append(tag(_none(
        ctx, "mcp", "Muse 사용자 MCP 서버", "muse|user-mcp", "Muse MCP", "user",
        "settings.json 에 MCP 서버 키가 없다(`muse-core:migrate` 로 Claude·Codex 설정을 가져올 수 있다).",
        engine="muse", path=ctx.disp(settings_path)), "user"))
    out.append(tag(_none(
        ctx, "memory", "Muse 메모리", "muse|memory", "Muse 메모리", "user",
        "개인 메모리(personal·personal_project)의 저장 위치는 코드·파일에서 확인되지 않았고 파일도 찾지 못했다"
        "(확인 필요). 프로젝트 메모리는 `<repo>/.agents/memory` 이며 프로젝트별 하네스에서 본다.",
        engine="muse"), "user"))
    mark_aliases(out)
    return out


# ---------------------------------------------------------------- Qwen Code(qwen-local-harness)

def _lit(node: ast.AST) -> Any:
    """리터럴만 값으로 꺼내고, 변수·호출은 <이름> 문자열로 둔다(코드를 실행하지 않는다)."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        return {str(_lit(k)): _lit(v) for k, v in zip(node.keys, node.values, strict=True) if k is not None}
    if isinstance(node, ast.List | ast.Tuple):
        return [_lit(e) for e in node.elts]
    return f"<{ast.unparse(node)}>"


def _literal_argv(text: str) -> dict[str, Any]:
    """runtime.py 의 build_claude_argv 에서 고정 인자와 --agents 정의를 AST 로 꺼낸다. 코드를 실행하지 않는다."""
    facts: dict[str, Any] = {"argv": [], "agents": None}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return facts
    for fn in ast.walk(tree):
        if not (isinstance(fn, ast.FunctionDef) and fn.name == "build_claude_argv"):
            continue
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "agents" for t in node.targets):
                call = node.value
                if isinstance(call, ast.Call) and call.args:
                    facts["agents"] = _lit(call.args[0])
            is_argv = isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "argv" for t in node.targets)
            if is_argv and isinstance(node.value, ast.List):
                facts["argv"] = _lit(node.value)
    return facts


def collect_qwen(ctx: Ctx, root: Path) -> list[Item]:
    out: list[Item] = []

    def tag(item: Item, layer: str, concern: str = "") -> Item:
        return _tag(item, "qwen", layer, concern)

    shown = ctx.disp(root)
    if not root.is_dir():
        out.append(tag(_missing(
            ctx, "instruction", "qwen-local-harness", root, scope="global", engine="qwen-local",
            description="qwen-worker 하네스 저장소를 찾지 못했다(설정 qwen_local_worker_path 기준)", group="Qwen 하네스"),
            "harness"))
        return out
    agents_md = root / "AGENTS.md"
    item = file_item(ctx, agents_md, "instruction", name="AGENTS.md", scope="global", engine="qwen-local",
                     group="하네스 저장소", frontmatter=False,
                     description="하네스 저장소를 개발할 때 쓰는 지침(Codex·Claude 가 이 저장소를 열 때)")
    item.active, item.active_note = True, ("worker 실행에는 주입되지 않는다: --bare 로 지침 자동 탐색을 끄고 "
                                           "prompts/worker.md 만 붙인다(runtime.py)")
    out.append(tag(item, "harness"))
    worker = root / "prompts/worker.md"
    item = file_item(ctx, worker, "instruction", name="prompts/worker.md", scope="global", engine="qwen-local",
                     group="하네스 저장소", frontmatter=False,
                     description="qwen-worker 시스템 프롬프트(`--append-system-prompt-file`)")
    item.active, item.active_note = True, "qwen-worker 실행마다 붙는다(worker.py)"
    out.append(tag(item, "harness"))
    skill = root / "skills/qwen-local-delegate/SKILL.md"
    item = file_item(ctx, skill, "skill", name="qwen-local-delegate", scope="global", engine="qwen-local",
                     group="하네스 저장소")
    item.active, item.active_note = True, "실행 전에 전용 프로필(.runtime/profile/skills)로 복사된다(runner.py)"
    out.append(tag(item, "harness"))
    conf = root / "config/harness.conf"
    try:
        text, _ = ctx.files.read_text(conf)
        masked, count = mask_text(text)
        entry = text_item("setting", "config/harness.conf", f"{conf}|conf", masked, scope="global", engine="qwen-local",
                          group="하네스 저장소", source="file", path=ctx.disp(conf), mtime=_mtime(conf),
                          description="포트·모델·컨텍스트·스레드 등 기본값(환경 변수 QWEN_LOCAL_* 가 우선)",
                          active=True)
        entry.masked = count
        out.append(tag(entry, "harness"))
    except Denied:
        out.append(tag(_missing(ctx, "setting", "config/harness.conf", conf, scope="global", engine="qwen-local",
                                description="하네스 기본 설정 파일 없음", group="하네스 저장소"), "harness"))
    generated = root / ".runtime/settings.json"
    if generated.exists():
        out.append(tag(json_item(
            "setting", ".runtime/settings.json (마지막 실행 설정)", f"{generated}|all", _read_json(ctx, generated),
            scope="global", engine="qwen-local", group="실행 시 생성", path=ctx.disp(generated),
            mtime=_mtime(generated), source="generated", active=True,
            description="요청마다 하네스가 다시 쓰는 `--settings` 파일. env 값은 숨김(키 이름만)"), "runtime"))
    facts: dict[str, Any] = {"argv": [], "agents": None}
    runtime = root / "src/qwen_local/runtime.py"
    try:
        facts = _literal_argv(ctx.files.read_text(runtime)[0])
    except Denied:
        pass
    if facts["argv"]:
        fixed = [a for a in facts["argv"] if isinstance(a, str)]
        out.append(tag(json_item(
            "setting", "Claude Code 실행 옵션(--bare 등)", f"{runtime}|argv", {"argv": fixed}, scope="global",
            engine="qwen-local", group="실행 옵션", source="code", path=f"{shown}/src/qwen_local/runtime.py build_claude_argv",
            description="`--bare --setting-sources user --strict-mcp-config --permission-mode acceptEdits` 와 허용 도구 목록",
            active=True, active_note="Claude Code 를 로컬 Qwen 모델에 연결해 쓴다. 원본 ~/.claude 는 읽지 않는다"),
            "runtime"))
    if isinstance(facts["agents"], dict) and facts["agents"]:
        for name, spec in facts["agents"].items():
            out.append(tag(json_item(
                "agent", str(name), f"{runtime}|agent|{name}", spec, scope="global", engine="qwen-local",
                group="실행 옵션", source="code", path=f"{shown}/src/qwen_local/runtime.py build_claude_argv --agents",
                description=str(spec.get("description", "")) if isinstance(spec, dict) else "", active=True,
                active_note="`--agents` 로 인라인 주입되는 하위 에이전트"), "runtime"))
    out.append(tag(_none(
        ctx, "hook", "Qwen 하네스 훅", "qwen|hook", "Qwen 훅", "harness",
        "훅을 쓰지 않는다. AGENTS.md 가 훅·MCP 추가를 금지하고 `--bare` 가 훅을 끈다(README).", engine="qwen-local",
        path=shown), "harness"))
    out.append(tag(_none(
        ctx, "mcp", "Qwen 하네스 MCP", "qwen|mcp", "Qwen MCP", "harness",
        "MCP 서버가 없다. `--strict-mcp-config` 로 MCP 설정을 비우고 따까리 어댑터도 MCP 를 주입하지 않는다"
        "(agents/qwen_local.py).", engine="qwen-local", path=shown), "harness"))
    out.append(tag(_none(
        ctx, "memory", "Qwen 하네스 메모리", "qwen|memory", "Qwen 메모리", "harness",
        "자동 메모리를 쓰지 않는다. `--bare` 는 자동 메모리·지침 탐색을 끈다(README 기준, 엔진 동작은 확인 필요).",
        engine="qwen-local", path=shown), "harness"))
    out.append(tag(_none(
        ctx, "plugin", "Qwen 하네스 플러그인", "qwen|plugin", "Qwen 플러그인", "harness",
        "플러그인을 쓰지 않는다. `--bare` 가 플러그인 동기화를 끈다(README 기준, 확인 필요).", engine="qwen-local",
        path=shown), "harness"))
    mark_aliases(out)
    return out


# ---------------------------------------------------------------- Antigravity·Codex 보강

def collect_antigravity_extras(ctx: Ctx) -> list[Item]:
    out: list[Item] = []
    mcp_path = ctx.gemini / "config/mcp_config.json"
    if mcp_path.exists():
        try:
            doc = json.loads(ctx.files.read_text(mcp_path)[0] or "{}")
        except (Denied, ValueError):
            doc = {}
        servers = doc.get("mcpServers") if isinstance(doc, dict) and isinstance(doc.get("mcpServers"), dict) else {}
        out += _mcp_items(ctx, servers, engine="antigravity", source=ctx.disp(mcp_path), group="Antigravity MCP",
                          scope="global", mtime=_mtime(mcp_path), sessions=())
        if not servers:
            out.append(_missing(ctx, "mcp", "mcp_config.json", mcp_path, scope="global", engine="antigravity",
                                description="MCP 설정 파일은 있으나 서버가 비어 있다", group="Antigravity MCP"))
    else:
        out.append(_missing(ctx, "mcp", "mcp_config.json", mcp_path, scope="global", engine="antigravity",
                            description="Antigravity 사용자 MCP 설정 없음", group="Antigravity MCP"))
    for p in sorted(ctx.files.glob(ctx.gemini / "antigravity-cli/builtin", "skills/*/SKILL.md")):
        item = file_item(ctx, p, "skill", name=p.parent.name, scope="global", engine="antigravity", group="내장 스킬")
        item.active, item.active_note = True, "Antigravity CLI 에 내장된 스킬"
        item.hint_layer = "builtin"
        out.append(item)
    return out


def collect_codex_extras(ctx: Ctx) -> list[Item]:
    flag = (ctx.codex_config.get("features") or {}).get("memories") if isinstance(ctx.codex_config.get("features"), dict) else None
    db = ctx.codex / "memories_1.sqlite"
    present = bool(ctx.files.exists(db))
    item = Item(category="memory", name="Codex 메모리(opt-in)", key=f"{db}|presence", scope="global", engine="codex",
                group="Codex 메모리", present=present, kind="text",
                description="`[features] memories` 를 켜면 Codex 가 스레드 기억을 만든다. 내용은 읽지 않는다.",
                path=ctx.disp(db), active=bool(flag), active_note=f"features.memories={flag!r}" if flag is not None
                else "features.memories 설정 없음(기본 꺼짐)")
    item.body = ("기억 저장소 파일 존재 여부와 설정 값만 보여 준다. 따까리는 sqlite 를 열지 않는다."
                 if present else "기억 저장소 파일이 없다.")
    if present:
        item.mtime = _mtime(db)
    return [item]


