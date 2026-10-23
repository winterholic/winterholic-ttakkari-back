"""하네스 항목 수집. 외부 도구(Claude Code·Codex·Antigravity)의 파일과, 따까리가 세션에 주입하는 코드·설정을 읽는다.

모든 파일 읽기는 SafeFiles 를 거치고, 값은 마스킹한 뒤에만 Item 에 담는다. 없는 항목은 present=False 로 남긴다.
"""
from __future__ import annotations

import json
import os
import re
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ttakkari.harness_view.model import LONG_CHARS, LONG_LINES, Item, parse_frontmatter
from ttakkari.harness_view.safe import (
    Denied,
    Root,
    SafeFiles,
    count_masked,
    mask_config,
    mask_text,
)

SKILL_GROUPS = {
    "skills": "스킬", "dev-experts-skills": "개발 전문가", "biz-experts-skills": "비즈니스 전문가",
    "life-experts-skills": "생활 전문가", "stock-experts": "주식 전문가",
}
MAX_SKILL_DEPTH = 5
PROJECT_SCAN_DIRS = 4000
PROJECT_FILES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")
SKIP_DIRS = frozenset({"node_modules", ".git", ".venv", "venv", "__pycache__", "dist", "build", ".next", "target"})
# Codex 프로젝트 문서 한도(project_doc_max_bytes 기본값). 넘으면 뒤가 잘린다.
CODEX_DOC_LIMIT = 32 * 1024
CLAUDE_MD_LIMIT_LINES = 200


@dataclass
class Ctx:
    user_home: Path
    ttakkari_home: Path
    files: SafeFiles
    settings: Any
    now: float
    accounts: list[dict[str, Any]] = field(default_factory=list)
    app_settings: dict[str, Any] = field(default_factory=dict)
    pinned_brief: str = ""
    claude_settings: dict[str, Any] = field(default_factory=dict)
    codex_config: dict[str, Any] = field(default_factory=dict)

    @property
    def claude(self) -> Path:
        return self.user_home / ".claude"

    @property
    def codex(self) -> Path:
        return self.user_home / ".codex"

    @property
    def gemini(self) -> Path:
        return self.user_home / ".gemini"

    @property
    def agents(self) -> Path:
        return self.user_home / ".agents"

    @property
    def harness_dir(self) -> Path:
        return self.ttakkari_home / "harness"

    def disp(self, path: Path | str) -> str:
        text = str(path)
        for base, short in ((str(self.user_home), "~"),):
            if text == base or text.startswith(base + os.sep):
                return short + text[len(base):]
        return text


def build_safe_files(user_home: Path, ttakkari_home: Path, project_roots: list[Path]) -> SafeFiles:
    skill_patterns = tuple(
        f"{g}{'/*' * depth}/SKILL.md" for g in SKILL_GROUPS for depth in range(1, MAX_SKILL_DEPTH))
    roots = [
        Root(user_home / ".claude", (
            "CLAUDE.md", "settings.json", "settings.local.json", ".claude.json", "agents/*.md", "commands/*.md",
            "commands/*/*.md", "rule-cards/*.md", "memory/*.md", "plugins/installed_plugins.json",
            "plugins/known_marketplaces.json", *skill_patterns)),
        Root(user_home / ".codex", ("AGENTS.md", "config.toml", "hooks.json", "rules/*.rules", "agents/*.toml",
                                    "references/*.md", "skills/*/SKILL.md", "skills/*/*/SKILL.md")),
        Root(user_home / ".gemini", ("GEMINI.md", "config/hooks.json", "antigravity-cli/settings.json")),
        Root(user_home / ".agents", ("skills/*/SKILL.md", "rules/*.md", "rules/*.mdc", ".skill-lock.json")),
        # 사용자 범위 MCP 가 들어 있는 파일. mcpServers 키만 꺼내고(read_json_subset) 존재 탐색에는 쓰지 않는다.
        Root(user_home, (".claude.json",), probe=False),
        Root(ttakkari_home / "harness", ("*.md",)),
        Root(ttakkari_home / "accounts", ("*/projects/*/memory/*.md",)),
        *[Root(r, ("CLAUDE.md", "AGENTS.md", "GEMINI.md", "*/CLAUDE.md", "*/AGENTS.md", "*/GEMINI.md",
                   "*/*/CLAUDE.md", "*/*/AGENTS.md", "*/*/GEMINI.md", "*/*/*/CLAUDE.md", "*/*/*/AGENTS.md",
                   "*/*/*/GEMINI.md", ".agents/rules/*.md", "*/.agents/rules/*.md", "*/*/.agents/rules/*.md"))
          for r in project_roots],
    ]
    return SafeFiles(roots)


# ---------------------------------------------------------------- 공통 도우미

def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _missing(ctx: Ctx, category: str, name: str, path: Path, *, scope: str, engine: str, description: str,
             group: str = "") -> Item:
    return Item(category=category, name=name, key=f"missing|{path}", scope=scope, engine=engine, group=group,
                description=description, path=ctx.disp(path), present=False, active=False,
                active_note="파일 없음", kind="text")


def file_item(ctx: Ctx, path: Path, category: str, *, name: str, scope: str, engine: str, group: str = "",
              description: str = "", sessions: tuple[str, ...] = (), kind: str = "markdown",
              frontmatter: bool = True) -> Item:
    item = Item(category=category, name=name, key=str(path), scope=scope, engine=engine, group=group,
                description=description, sessions=sessions, path=ctx.disp(path), kind=kind)
    try:
        real = ctx.files.resolve(path)
        text, truncated = ctx.files.read_text(path)
    except Denied as exc:
        item.present, item.active, item.active_note = False, False, f"읽지 않음: {exc}"
        item.warnings.append(f"읽기 거부: {exc}")
        return item
    item.mtime = _mtime(real)
    item.abs_path = str(path)
    item.truncated = truncated
    if str(real) != os.path.abspath(path):
        item.real_path = ctx.disp(real)
    if frontmatter and kind == "markdown":
        meta, _ = parse_frontmatter(text)
        if meta:
            item.meta["frontmatter"] = {k: mask_text(v)[0] for k, v in meta.items()}
            item.description = item.description or mask_text(meta.get("description", ""))[0]
            item.name = meta.get("name") or item.name
    item.body, item.masked = mask_text(text)
    if not item.description and kind == "markdown":
        item.description = _first_line(item.body)
    item.refs = extract_refs(text)
    if truncated:
        item.warnings.append("본문이 256KB 를 넘어 앞부분만 읽었다")
    return item


def _first_line(body: str) -> str:
    """frontmatter 가 없는 문서의 설명 대신 쓰는 첫 제목·문장."""
    _, rest = parse_frontmatter(body)
    for line in rest.splitlines():
        line = line.strip().lstrip("#>-*").strip()
        if line:
            return line[:140]
    return ""


_LINK_RE = re.compile(r"\]\(([^)\s#]+\.(?:md|json|toml|py|sh|mjs|rules))(?:#[^)]*)?\)")
_TILDE_RE = re.compile(r"(?<![\w/])(~/[\w@./\-]+\.(?:md|json|toml|py|sh|mjs|rules))")


def extract_refs(text: str) -> list[str]:
    """깨진 참조 검사 후보. 오탐을 줄이려고 마크다운 링크와 `~/` 로 시작하는 경로만 본다(백틱 상대 경로는 예시가 많다)."""
    refs = [m for m in _LINK_RE.findall(text) if not re.match(r"[a-z]+:", m)]
    refs += _TILDE_RE.findall(text)
    out: list[str] = []
    for ref in refs:
        if ref not in out and len(ref) < 300:
            out.append(ref)
    return out[:60]


def check_refs(ctx: Ctx, items: list[Item]) -> None:
    """허용 루트 안의 경로만 존재를 확인한다. 루트 밖 참조는 판정하지 않는다(경로 탐색 금지)."""
    for item in items:
        if not item.present or not item.refs or not item.abs_path:
            continue
        base = Path(item.abs_path).parent
        broken = []
        for ref in item.refs:
            target = (ctx.user_home / ref[2:] if ref.startswith("~/") else Path(ref) if ref.startswith("/")
                      else base / ref)
            # 허용 루트 안에서 없을 때만 깨진 참조다. 루트 밖이면 판정을 보류한다(None).
            if ctx.files.exists(target) is False:
                broken.append(ref)
        if broken:
            item.warnings.append("깨진 참조: " + ", ".join(broken[:5]) + (" 외" if len(broken) > 5 else ""))
        item.refs = []


def length_warnings(item: Item, *, doc: str) -> None:
    if doc == "claude" and item.lines > LONG_LINES:
        item.warnings.append(f"CLAUDE.md 가 {item.lines}줄로 길다(권장 {CLAUDE_MD_LIMIT_LINES}줄 안팎)")
    elif doc == "claude" and item.chars > LONG_CHARS:
        item.warnings.append(f"CLAUDE.md 가 {item.chars:,}자로 길다")
    elif doc == "codex" and len(item.body.encode()) > CODEX_DOC_LIMIT:
        item.warnings.append("AGENTS.md 가 32KiB 를 넘어 Codex 가 뒤를 자른다")
    elif doc == "memory-index" and item.lines > CLAUDE_MD_LIMIT_LINES:
        item.warnings.append(f"MEMORY.md 가 {item.lines}줄이라 {CLAUDE_MD_LIMIT_LINES}줄 뒤는 로드되지 않는다")


def json_item(category: str, name: str, key: str, data: Any, *, scope: str, engine: str, group: str = "",
              description: str = "", path: str | None = None, mtime: float | None = None, source: str = "file",
              sessions: tuple[str, ...] = (), active: bool | None = None, active_note: str = "") -> Item:
    masked = mask_config(data)
    body = json.dumps(masked, ensure_ascii=False, indent=2, default=str)
    item = Item(category=category, name=name, key=key, scope=scope, engine=engine, group=group,
                description=description, path=path, mtime=mtime, source=source, kind="json", body=body,
                sessions=sessions, active=active, active_note=active_note)
    item.masked = count_masked(data, masked)
    return item


def text_item(category: str, name: str, key: str, text: str, *, scope: str, engine: str, group: str = "",
              description: str = "", source: str = "code", path: str | None = None, mtime: float | None = None,
              sessions: tuple[str, ...] = (), kind: str = "text", active: bool | None = True,
              active_note: str = "") -> Item:
    body, masked = mask_text(text)
    return Item(category=category, name=name, key=key, scope=scope, engine=engine, group=group,
                description=description, source=source, path=path, mtime=mtime, kind=kind, body=body,
                masked=masked, sessions=sessions, active=active, active_note=active_note)


def claude_sessions(ctx: Ctx) -> tuple[str, ...]:
    """Claude 엔진 세션에서 사용자 전역 설정이 실리는 세션 종류. 접수는 --setting-sources local 이라 제외다."""
    return () if ctx.settings.claude_setting_sources else ("lead", "worker", "orchestrator")


def _walk_skills(base: Path, depth: int = 0, seen: set[str] | None = None) -> Iterator[Path]:
    seen = seen if seen is not None else set()
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name)
    except OSError:
        return
    real = os.path.realpath(base)
    if real in seen:
        return
    seen.add(real)
    for entry in entries:
        if entry.name.startswith(".") or entry.name in SKIP_DIRS:
            continue
        if entry.is_file() and entry.name == "SKILL.md":
            yield Path(entry.path)
        elif entry.is_dir() and depth < MAX_SKILL_DEPTH - 1:
            yield from _walk_skills(Path(entry.path), depth + 1, seen)


# ---------------------------------------------------------------- Claude Code

def load_claude_settings(ctx: Ctx) -> None:
    path = ctx.claude / "settings.json"
    try:
        text, _ = ctx.files.read_text(path)
        data = json.loads(text)
        ctx.claude_settings = data if isinstance(data, dict) else {}
    except (Denied, ValueError):
        ctx.claude_settings = {}


def load_codex_config(ctx: Ctx) -> None:
    try:
        text, _ = ctx.files.read_text(ctx.codex / "config.toml")
        ctx.codex_config = tomllib.loads(text)
    except (Denied, ValueError):
        ctx.codex_config = {}


def collect_claude_instructions(ctx: Ctx) -> list[Item]:
    path = ctx.claude / "CLAUDE.md"
    sess = claude_sessions(ctx)
    if not path.exists():
        return [_missing(ctx, "instruction", "CLAUDE.md", path, scope="global", engine="claude",
                         description="Claude Code 사용자 전역 지침")]
    item = file_item(ctx, path, "instruction", name="CLAUDE.md", scope="global", engine="claude",
                     description="Claude Code 사용자 전역 지침", sessions=sess, frontmatter=False)
    item.active = True
    item.active_note = ("따까리 기획·작업 세션에 CLAUDE.md 로 로드(claude_setting_sources 비어 있음)" if sess
                        else "claude_setting_sources 설정으로 따까리 세션에서는 제외")
    length_warnings(item, doc="claude")
    return [item]


def collect_codex_instructions(ctx: Ctx) -> list[Item]:
    path = ctx.codex / "AGENTS.md"
    if not path.exists():
        return [_missing(ctx, "instruction", "AGENTS.md", path, scope="global", engine="codex",
                         description="Codex 전역 지침")]
    item = file_item(ctx, path, "instruction", name="AGENTS.md", scope="global", engine="codex",
                     description="Codex 전역 지침(CODEX_HOME)", sessions=("lead", "worker", "orchestrator"),
                     frontmatter=False)
    item.active, item.active_note = True, "Codex 는 CODEX_HOME 의 AGENTS.md 를 읽는다(CLI 동작, 따까리 코드 밖)"
    length_warnings(item, doc="codex")
    return [item]


def collect_gemini_instructions(ctx: Ctx) -> list[Item]:
    out = []
    path = ctx.gemini / "GEMINI.md"
    if path.exists():
        item = file_item(ctx, path, "instruction", name="GEMINI.md", scope="global", engine="antigravity",
                         description="Gemini/Antigravity 전역 지침", frontmatter=False)
        item.active, item.active_note = True, "Antigravity 가 읽는지는 확인 필요(따까리 코드 밖)"
        out.append(item)
    else:
        out.append(_missing(ctx, "instruction", "GEMINI.md", path, scope="global", engine="antigravity",
                            description="Gemini/Antigravity 전역 지침"))
    rules = sorted(ctx.files.glob(ctx.agents, "rules/*.md"))
    if rules:
        for p in rules:
            out.append(file_item(ctx, p, "rule", name=p.stem, scope="global", engine="shared", group="agents rules",
                                 description="~/.agents/rules"))
    else:
        out.append(_missing(ctx, "rule", ".agents/rules", ctx.agents / "rules", scope="global", engine="shared",
                            description="전역 Antigravity 규칙 폴더", group="agents rules"))
    return out


def collect_projects(ctx: Ctx, roots: list[Path]) -> list[Item]:
    """프로젝트 루트의 CLAUDE.md·AGENTS.md·GEMINI.md. git worktree(.git 파일)는 건너뛰어 중복을 줄인다."""
    out: list[Item] = []
    budget = [PROJECT_SCAN_DIRS]
    for root in roots:
        if not root.is_dir():
            continue
        stack = [(root, 0)]
        while stack and budget[0] > 0:
            current, depth = stack.pop()
            budget[0] -= 1
            git = current / ".git"
            if depth > 0 and git.is_file():
                continue
            for fname in PROJECT_FILES:
                p = current / fname
                if p.is_file():
                    engine = {"CLAUDE.md": "claude", "AGENTS.md": "codex", "GEMINI.md": "antigravity"}[fname]
                    rel = p.relative_to(root.parent).parent.as_posix()
                    item = file_item(ctx, p, "instruction", name=f"{rel}/{fname}", scope="project", engine=engine,
                                     group=rel, description="프로젝트 지침", frontmatter=False,
                                     sessions=("lead", "worker", "orchestrator"))
                    item.active, item.active_note = True, "작업 디렉터리가 이 프로젝트일 때 로드"
                    if fname == "CLAUDE.md":
                        length_warnings(item, doc="claude")
                    elif fname == "AGENTS.md":
                        length_warnings(item, doc="codex")
                    out.append(item)
            for rule in sorted(current.glob(".agents/rules/*.md")):
                rel = rule.relative_to(root.parent).parent.parent.parent.as_posix()
                out.append(file_item(ctx, rule, "rule", name=f"{rel}/{rule.name}", scope="project", engine="shared",
                                     group=rel, description="프로젝트 .agents/rules", frontmatter=False))
            if depth < 3:
                try:
                    subs = sorted(e.path for e in os.scandir(current)
                                  if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS
                                  and not e.name.startswith("."))
                except OSError:
                    subs = []
                stack.extend((Path(s), depth + 1) for s in reversed(subs))
    return out


def collect_skills(ctx: Ctx) -> list[Item]:
    overrides = ctx.claude_settings.get("skillOverrides") if isinstance(ctx.claude_settings.get("skillOverrides"), dict) else {}
    sess = claude_sessions(ctx)
    items: list[Item] = []
    for group, label in SKILL_GROUPS.items():
        base = ctx.claude / group
        if not base.is_dir():
            items.append(_missing(ctx, "skill", group, base, scope="global", engine="claude",
                                  description=f"{label} 스킬 폴더", group=label))
            continue
        for skill in _walk_skills(base):
            dirname = skill.parent.name
            synced = skill.relative_to(base).parts[0] == "synced"
            item = file_item(ctx, skill, "skill", name=dirname, scope="global", engine="claude",
                             group=f"{label} · claude.ai 동기화" if synced else label, sessions=sess)
            fm = item.meta.get("frontmatter", {})
            override = overrides.get(item.name) or overrides.get(dirname)
            item.active, item.active_note = True, "프롬프트의 스킬 목록에 노출" if sess else "따까리 세션에서는 제외"
            if override == "off":
                item.active, item.active_note = False, "settings.json skillOverrides 에서 꺼짐"
            elif override:
                item.active_note = f"skillOverrides: {override}"
            elif str(fm.get("disable-model-invocation", "")).lower() == "true":
                item.active_note = "사용자 호출 전용(disable-model-invocation)"
            if not item.description and item.present:
                item.warnings.append("frontmatter description 없음")
            if item.present and fm.get("name") and fm["name"] != dirname:
                item.warnings.append(f"폴더 이름({dirname})과 frontmatter name({fm['name']})이 다르다")
            items.append(item)
    for p in ctx.files.glob(ctx.agents, "skills/*/SKILL.md"):
        item = file_item(ctx, p, "skill", name=p.parent.name, scope="global", engine="shared", group="~/.agents 공용")
        item.active, item.active_note = True, "공용 스킬 폴더(Codex·Antigravity 등이 읽는다)"
        if not item.description and item.present:
            item.warnings.append("frontmatter description 없음")
        items.append(item)
    codex_skills = list(ctx.files.glob(ctx.codex, "skills/*/SKILL.md"))
    if codex_skills:
        for p in codex_skills:
            item = file_item(ctx, p, "skill", name=p.parent.name, scope="global", engine="codex", group="Codex")
            item.active = True
            items.append(item)
    else:
        items.append(_missing(ctx, "skill", "codex skills", ctx.codex / "skills", scope="global", engine="codex",
                              description="Codex 스킬 폴더(비어 있음)", group="Codex"))
    mark_aliases(items)
    flag_duplicates(items)
    return items


def mark_aliases(items: list[Item]) -> None:
    """심볼릭 링크로 같은 파일을 가리키는 항목은 원본(링크가 아닌 쪽)의 별칭으로 표시해 중복 경고를 피한다."""
    originals = {i.path: i for i in items if i.present and i.path and not i.real_path}
    for item in items:
        if item.present and item.real_path and item.real_path in originals:
            item.alias_of = originals[item.real_path].id


def flag_duplicates(items: list[Item]) -> None:
    seen: dict[tuple[str, str, str], Item] = {}
    for item in items:
        if not item.present or item.alias_of:
            continue
        key = (item.category, item.engine, item.name.lower())
        other = seen.get(key)
        if other is None:
            seen[key] = item
        else:
            item.warnings.append(f"중복 이름: {other.path}")
            if not any("중복 이름" in w for w in other.warnings):
                other.warnings.append(f"중복 이름: {item.path}")


def collect_claude_agents_commands_rules(ctx: Ctx) -> list[Item]:
    items: list[Item] = []
    sess = claude_sessions(ctx)
    for sub, category, label in (("agents", "agent", "Claude 서브에이전트"), ("commands", "agent", "Claude 슬래시 커맨드"),
                                 ("rule-cards", "rule", "규칙 카드")):
        base = ctx.claude / sub
        paths = sorted(ctx.files.glob(base, "*.md")) + (sorted(ctx.files.glob(base, "*/*.md")) if sub == "commands" else [])
        if not paths:
            items.append(_missing(ctx, category, sub, base, scope="global", engine="claude",
                                  description=f"{label} 폴더(비어 있거나 없음)", group=label))
        for p in paths:
            item = file_item(ctx, p, category, name=p.stem, scope="global", engine="claude", group=label, sessions=sess)
            item.active = True
            items.append(item)
    for p in sorted(ctx.files.glob(ctx.codex, "agents/*.toml")):
        item = file_item(ctx, p, "agent", name=p.stem, scope="global", engine="codex", group="Codex 에이전트",
                         kind="text", frontmatter=False)
        try:
            data = tomllib.loads(item.body)
            item.description = str(data.get("description") or data.get("developer_instructions", ""))[:300]
            item.name = str(data.get("name") or item.name)
        except ValueError:
            item.warnings.append("TOML 해석 실패")
        item.active = True
        items.append(item)
    for p in sorted(ctx.files.glob(ctx.codex, "rules/*.rules")):
        item = file_item(ctx, p, "rule", name=p.name, scope="global", engine="codex", group="Codex 실행 규칙",
                         kind="text", frontmatter=False)
        rules = re.findall(r'decision="(\w+)"', item.body)
        item.description = f"prefix_rule {len(rules)}개: " + ", ".join(
            f"{d} {rules.count(d)}" for d in sorted(set(rules)))
        item.active = True
        items.append(item)
    for p in sorted(ctx.files.glob(ctx.codex, "references/*.md")):
        item = file_item(ctx, p, "rule", name=p.name, scope="global", engine="codex", group="Codex 참조 문서",
                         frontmatter=False)
        item.active = True
        items.append(item)
    return items


def collect_memory(ctx: Ctx) -> list[Item]:
    items: list[Item] = []
    base = ctx.claude / "memory"
    for p in sorted(ctx.files.glob(base, "*.md")):
        item = file_item(ctx, p, "memory", name=p.name, scope="global", engine="claude", group="~/.claude/memory",
                         sessions=claude_sessions(ctx))
        item.active, item.active_note = True, "MEMORY.md 가 색인, 나머지는 필요할 때 읽는다"
        if p.name == "MEMORY.md":
            length_warnings(item, doc="memory-index")
        items.append(item)
    if not items:
        items.append(_missing(ctx, "memory", "memory", base, scope="global", engine="claude",
                              description="Claude 전역 메모리 폴더", group="~/.claude/memory"))
    names = {a["id"]: a["name"] for a in ctx.accounts}
    for p in sorted(ctx.files.glob(ctx.ttakkari_home / "accounts", "*/projects/*/memory/*.md")):
        account = p.relative_to(ctx.ttakkari_home / "accounts").parts[0]
        label = names.get(account, account[:8])
        item = file_item(ctx, p, "memory", name=p.name, scope="account", engine="claude",
                         group=f"따까리 계정 {label}", sessions=("lead", "worker", "orchestrator"))
        item.active, item.active_note = True, "해당 계정(CLAUDE_CONFIG_DIR)으로 도는 Claude 세션의 자동 메모리"
        if p.name == "MEMORY.md":
            length_warnings(item, doc="memory-index")
        items.append(item)
    return items


def _hook_items(ctx: Ctx, hooks: Any, *, engine: str, source_path: Path, config_key: str, mtime: float | None,
                enabled: Callable[[str, int, int], bool | None] | None = None,
                disabled_all: bool = False) -> list[Item]:
    items: list[Item] = []
    if not isinstance(hooks, dict):
        return items
    sess = claude_sessions(ctx) if engine == "claude" else ("lead", "worker", "orchestrator")
    for event, groups in hooks.items():
        if not isinstance(groups, list):
            continue
        for gi, group in enumerate(groups):
            if not isinstance(group, dict):
                continue
            matcher = group.get("matcher") or "*"
            inner = group.get("hooks") if isinstance(group.get("hooks"), list) else [group]
            for hi, hook in enumerate(inner):
                if not isinstance(hook, dict):
                    continue
                command = str(hook.get("command", hook.get("type", "")))
                script = _script_name(command)
                masked_cmd = mask_config(command)
                name = f"{event} · {matcher} · {script or masked_cmd[:48]}"
                desc = str(hook.get("statusMessage") or masked_cmd)[:260]
                item = json_item("hook", name, f"{source_path}|{config_key}|{event}|{gi}|{hi}",
                                 {"event": event, "matcher": group.get("matcher"), "hook": hook},
                                 scope="global", engine=engine, group=event, description=desc, mtime=mtime,
                                 path=f"{ctx.disp(source_path)} › {config_key}.{event}[{gi}].hooks[{hi}]", sessions=sess)
                state = enabled(event, gi, hi) if enabled else None
                item.active = not disabled_all if state is None else state
                item.active_note = ("disableAllHooks 로 꺼짐" if disabled_all else
                                    "" if state is None else "config.toml hooks.state 값")
                item.warnings.extend(_missing_script_warnings(ctx, command))
                items.append(item)
    return items


def _script_name(command: str) -> str:
    m = re.search(r"([\w.\-/~$]+\.(?:py|sh|mjs|js|cmd))\b", command)
    return Path(m.group(1)).name if m else ""


def _missing_script_warnings(ctx: Ctx, command: str) -> list[str]:
    out = []
    for m in re.finditer(r"(?<![\w.])((?:~/|/)[\w.\-/]+\.(?:py|sh|mjs|js))", command):
        raw = m.group(1)
        path = ctx.user_home / raw[2:] if raw.startswith("~/") else Path(raw)
        # 허용 루트 안(~/.claude 등)에 있는 스크립트만 존재 여부를 본다.
        if ctx.files.exists(path) is False:
            out.append(f"훅 스크립트 없음: {ctx.disp(path)}")
    return out[:3]


def collect_claude_settings(ctx: Ctx) -> list[Item]:
    path = ctx.claude / "settings.json"
    if not path.exists():
        return [_missing(ctx, "setting", "settings.json", path, scope="global", engine="claude",
                         description="Claude Code 사용자 설정")]
    data = ctx.claude_settings
    mt = _mtime(path)
    sess = claude_sessions(ctx)
    disp = ctx.disp(path)
    items: list[Item] = []
    whole = json_item("setting", "settings.json 전체", f"{path}|all", data, scope="global", engine="claude",
                      group="Claude 설정", description="마스킹된 전체 보기(env·headers 값은 숨김)", path=disp, mtime=mt,
                      sessions=sess, active=True, active_note="사용자 전역 설정" if sess else "따까리 세션에서는 제외")
    whole.warnings.extend(_missing_script_warnings(ctx, json.dumps(data.get("statusLine", {}))))
    items.append(whole)
    perms = data.get("permissions") or {}
    items.append(json_item("setting", "permissions", f"{path}|permissions", perms, scope="global", engine="claude",
                           group="Claude 설정", path=f"{disp} › permissions", mtime=mt, sessions=sess, active=bool(perms),
                           description=f"defaultMode={perms.get('defaultMode', '-')}" if perms else "없음"))
    env = data.get("env") or {}
    items.append(json_item("setting", "env", f"{path}|env", env, scope="global", engine="claude", group="Claude 설정",
                           path=f"{disp} › env", mtime=mt, sessions=sess, active=bool(env),
                           description=("키 " + ", ".join(sorted(env)) + " (값은 숨김)") if env else "없음"))
    if "statusLine" in data:
        sl = data["statusLine"]
        item = json_item("setting", "statusLine", f"{path}|statusLine", sl, scope="global", engine="claude",
                         group="Claude 설정", path=f"{disp} › statusLine", mtime=mt, active=True,
                         description=mask_config(str(sl.get("command", "")))[:200] if isinstance(sl, dict) else "")
        item.warnings.extend(_missing_script_warnings(ctx, json.dumps(sl)))
        items.append(item)
    general = {k: v for k, v in data.items()
               if k not in {"hooks", "permissions", "env", "statusLine", "enabledPlugins", "extraKnownMarketplaces",
                            "skillOverrides", "mcpServers"}}
    items.append(json_item("setting", "기본 설정", f"{path}|general", general, scope="global", engine="claude",
                           group="Claude 설정", path=disp, mtime=mt, sessions=sess, active=True,
                           description=", ".join(f"{k}={v}" for k, v in general.items() if isinstance(v, str | bool))[:200]))
    if "skillOverrides" in data:
        items.append(json_item("setting", "skillOverrides", f"{path}|skillOverrides", data["skillOverrides"],
                               scope="global", engine="claude", group="Claude 설정", path=f"{disp} › skillOverrides",
                               mtime=mt, active=True, description=f"스킬 {len(data['skillOverrides'])}개의 노출 방식 덮어쓰기"))
    local = ctx.claude / "settings.local.json"
    if not local.exists():
        items.append(_missing(ctx, "setting", "settings.local.json", local, scope="global", engine="claude",
                              description="Claude Code 로컬 설정", group="Claude 설정"))
    items += _hook_items(ctx, data.get("hooks"), engine="claude", source_path=path, config_key="hooks", mtime=mt,
                         disabled_all=bool(data.get("disableAllHooks")))
    enabled = data.get("enabledPlugins") if isinstance(data.get("enabledPlugins"), dict) else {}
    installed = _read_json(ctx, ctx.claude / "plugins/installed_plugins.json")
    plugin_info = installed.get("plugins", {}) if isinstance(installed, dict) else {}
    for name, flag in sorted(enabled.items()):
        info = plugin_info.get(name)
        item = json_item("plugin", name, f"{path}|plugin|{name}", {"enabled": flag, "installed": info},
                         scope="global", engine="claude", group="Claude 플러그인", path=f"{disp} › enabledPlugins",
                         mtime=mt, active=bool(flag), sessions=sess if flag else (),
                         active_note="enabledPlugins 값", description=("설치됨" if info else "설치 기록 없음") + f" · enabled={flag}")
        if flag and not info:
            item.warnings.append("활성인데 installed_plugins.json 에 설치 기록이 없다")
        items.append(item)
    if not enabled:
        items.append(_missing(ctx, "plugin", "plugins", ctx.claude / "plugins", scope="global", engine="claude",
                              description="활성화된 플러그인 없음", group="Claude 플러그인"))
    mcp = dict(data.get("mcpServers") or {})
    for source in (ctx.claude / ".claude.json", ctx.user_home / ".claude.json"):
        mcp.update(_mcp_from_json(ctx, source))
    items += _mcp_items(ctx, mcp, engine="claude", source=disp + " · .claude.json", group="Claude MCP",
                        scope="global", mtime=mt, sessions=sess)
    if not mcp:
        items.append(_missing(ctx, "mcp", "Claude 사용자 MCP 서버", ctx.claude / ".claude.json", scope="global",
                              engine="claude", description="사용자 범위로 등록된 MCP 서버 없음", group="Claude MCP"))
    enabled_json = data.get("enabledMcpjsonServers")
    if enabled_json:
        items.append(json_item("mcp", "enabledMcpjsonServers", f"{path}|enabledMcpjsonServers", enabled_json,
                               scope="global", engine="claude", group="Claude MCP", path=f"{disp} › enabledMcpjsonServers",
                               mtime=mt, active=True, description="프로젝트 .mcp.json 에서 승인된 서버 이름"))
    return items


def _read_json(ctx: Ctx, path: Path) -> Any:
    try:
        text, _ = ctx.files.read_text(path)
        return json.loads(text)
    except (Denied, ValueError):
        return {}


def _mcp_from_json(ctx: Ctx, path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        got = ctx.files.read_json_subset(path, ("mcpServers",)).get("mcpServers")
    except Denied:
        return {}
    return got if isinstance(got, dict) else {}


def _mcp_items(ctx: Ctx, servers: dict[str, Any], *, engine: str, source: str, group: str, scope: str,
               mtime: float | None, sessions: tuple[str, ...], toml: bool = False) -> list[Item]:
    items = []
    for name, spec in sorted(servers.items()):
        spec = spec if isinstance(spec, dict) else {"value": spec}
        view = mask_config(spec)
        desc = " ".join(str(x) for x in (view.get("command") or view.get("url") or "", *(view.get("args") or [])[:3]))
        item = json_item("mcp", name, f"{source}|{name}", view, scope=scope, engine=engine, group=group,
                         path=source, mtime=mtime, sessions=sessions, active=spec.get("enabled", True) is not False,
                         description=desc[:200])
        env_keys = sorted((spec.get("env") or spec.get("headers") or {}).keys()) if isinstance(spec.get("env") or spec.get("headers"), dict) else []
        if env_keys:
            item.meta["secret_keys"] = env_keys
        items.append(item)
    return items


# ---------------------------------------------------------------- Codex / Antigravity

def collect_codex_settings(ctx: Ctx) -> list[Item]:
    path = ctx.codex / "config.toml"
    if not path.exists():
        return [_missing(ctx, "setting", "config.toml", path, scope="global", engine="codex",
                         description="Codex 설정")]
    data = ctx.codex_config
    mt = _mtime(path)
    disp = ctx.disp(path)
    sess = ("lead", "worker", "orchestrator")
    projects = data.get("projects") if isinstance(data.get("projects"), dict) else {}
    trimmed = {k: v for k, v in data.items() if k != "projects"}
    if projects:
        trimmed["projects"] = f"신뢰 프로젝트 {len(projects)}개(별도 항목)"
    items = [json_item("setting", "config.toml 전체", f"{path}|all", trimmed, scope="global", engine="codex",
                       group="Codex 설정", description="TOML 을 JSON 으로 보여 준다. env 값은 숨김", path=disp, mtime=mt,
                       sessions=sess, active=True,
                       active_note="따까리가 -c 로 sandbox_mode·approval_policy·developer_instructions 를 덮어쓴다")]
    items.append(json_item("setting", "projects (trust)", f"{path}|projects", projects, scope="global", engine="codex",
                           group="Codex 설정", path=f"{disp} › projects", mtime=mt, active=bool(projects),
                           description=f"신뢰 프로젝트 {len(projects)}개"))
    fallback = data.get("project_doc_fallback_filenames")
    if fallback:
        # 경고가 아니라 "어디서 무엇이 실리나"에 대한 안내라서 설명으로 둔다.
        items[0].description = f"project_doc_fallback_filenames={fallback}: AGENTS.md 가 없으면 이 파일을 프로젝트 지침으로 읽는다"
    mcp = data.get("mcp_servers") if isinstance(data.get("mcp_servers"), dict) else {}
    items += _mcp_items(ctx, mcp, engine="codex", source=f"{disp} › mcp_servers", group="Codex MCP", scope="global",
                        mtime=mt, sessions=sess)
    if not mcp:
        items.append(_missing(ctx, "mcp", "Codex MCP 서버", path, scope="global", engine="codex",
                              description="config.toml 에 mcp_servers 없음", group="Codex MCP"))
    state = (data.get("hooks") or {}).get("state") if isinstance(data.get("hooks"), dict) else {}
    hooks_path = ctx.codex / "hooks.json"
    if hooks_path.exists():
        hooks = _read_json(ctx, hooks_path)
        hooks = hooks.get("hooks", hooks) if isinstance(hooks, dict) else {}

        def enabled(event: str, gi: int, hi: int) -> bool | None:
            snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event).lower()
            entry = (state or {}).get(f"{hooks_path}:{snake}:{gi}:{hi}")
            return entry.get("enabled") if isinstance(entry, dict) and "enabled" in entry else None

        items += _hook_items(ctx, hooks, engine="codex", source_path=hooks_path, config_key="hooks",
                             mtime=_mtime(hooks_path), enabled=enabled)
    else:
        items.append(_missing(ctx, "hook", "hooks.json", hooks_path, scope="global", engine="codex",
                              description="Codex 훅 설정"))
    return items


def collect_gemini_settings(ctx: Ctx) -> list[Item]:
    items: list[Item] = []
    hooks_path = ctx.gemini / "config/hooks.json"
    if hooks_path.exists():
        data = _read_json(ctx, hooks_path)
        mt = _mtime(hooks_path)
        items.append(json_item("hook", "hooks.json", f"{hooks_path}|all", data, scope="global", engine="antigravity",
                               group="Gemini 훅", description=f"최상위 키: {', '.join(data) if isinstance(data, dict) else '-'}",
                               path=ctx.disp(hooks_path), mtime=mt, active=True,
                               active_note="Antigravity 가 읽는 방식은 확인 필요"))
    else:
        items.append(_missing(ctx, "hook", "hooks.json", hooks_path, scope="global", engine="antigravity",
                              description="Gemini/Antigravity 훅 설정"))
    cli = ctx.gemini / "antigravity-cli/settings.json"
    if cli.exists():
        items.append(json_item("setting", "antigravity-cli settings.json", f"{cli}|all", _read_json(ctx, cli),
                               scope="global", engine="antigravity", group="Antigravity 설정", path=ctx.disp(cli),
                               mtime=_mtime(cli), active=True))
    else:
        items.append(_missing(ctx, "setting", "antigravity-cli settings.json", cli, scope="global",
                              engine="antigravity", description="Antigravity CLI 설정"))
    return items


def check_skill_overrides(items: list[Item]) -> None:
    """skillOverrides 가 존재하지 않는 스킬을 가리키면 설정 항목에 경고를 단다."""
    names = {i.path.rsplit("/", 2)[-2] for i in items if i.category == "skill" and i.engine == "claude" and i.path}
    names |= {i.name for i in items if i.category == "skill" and i.engine == "claude"}
    for item in items:
        if item.category == "setting" and item.name == "skillOverrides":
            keys = json.loads(item.body).keys() if item.body else []
            stale = sorted(k for k in keys if k not in names)
            if stale:
                item.warnings.append("없는 스킬을 가리킨다: " + ", ".join(stale))
