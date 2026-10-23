"""프로젝트별 하네스 수집(ADR 0070). 프로젝트 루트의 지침·엔진 설정 폴더를 프로젝트 단위로 묶는다.

프로젝트 키는 경로에서만 정한다: `projects/<이름>`·`hyperez/<이름>` 은 두 단계, 그 밖은 첫 폴더. `hyperez` 아래는 회사 범위다.
읽기는 SafeFiles 화이트리스트(깊이별 패턴)만 쓰고, git worktree(.git 파일)와 의존성 폴더는 건너뛴다.
"""
from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ttakkari.harness_view.model import Item
from ttakkari.harness_view.safe import Denied
from ttakkari.harness_view.sources import (
    SKIP_DIRS,
    Ctx,
    _hook_items,
    _mcp_items,
    _mtime,
    file_item,
    json_item,
    length_warnings,
    mark_aliases,
)

PROJECT_SCAN_DIRS = 4000
SCAN_DEPTH = 3  # 개발 루트 아래 몇 단계까지 보는가(0 = 개발 루트 자체)
CONTAINERS = ("projects", "hyperez")
COMPANY_CONTAINERS = ("hyperez",)
SESSIONS = ("lead", "worker", "orchestrator")

# (폴더 안 상대 패턴, 엔진, category). 패턴은 SafeFiles 에도 같은 모양으로 등록된다.
FILE_SPECS: tuple[tuple[str, str, str], ...] = (
    ("CLAUDE.md", "claude", "instruction"),
    (".claude/CLAUDE.md", "claude", "instruction"),
    ("AGENTS.md", "codex", "instruction"),
    ("GEMINI.md", "antigravity", "instruction"),
    (".claude/rules/*.md", "claude", "rule"),
    (".agents/rules/*.md", "shared", "rule"),
    (".agents/rules/*.mdc", "shared", "rule"),
    (".claude/agents/*.md", "claude", "agent"),
    (".claude/commands/*.md", "claude", "agent"),
    (".codex/agents/*.toml", "codex", "agent"),
    (".claude/skills/*/SKILL.md", "claude", "skill"),
    (".agents/skills/*/SKILL.md", "shared", "skill"),
    (".agents/memory/*.md", "shared", "memory"),
    (".claude/settings.json", "claude", "setting"),
    (".claude/settings.local.json", "claude", "setting"),
    (".codex/config.toml", "codex", "setting"),
    (".codex/hooks.json", "codex", "hook"),
    (".gemini/settings.json", "antigravity", "setting"),
    (".mcp.json", "claude", "mcp"),
)


def read_patterns() -> tuple[str, ...]:
    """프로젝트 루트 Root 에 등록할 읽기 패턴. 폴더 경계를 지키려고 깊이마다 `*/` 를 하나씩 붙여 늘린다."""
    out: list[str] = []
    for depth in range(SCAN_DEPTH + 1):
        prefix = "*/" * depth
        out += [prefix + spec for spec, _, _ in FILE_SPECS]
    return tuple(out)


@dataclass(frozen=True)
class Located:
    key: str  # projects/aip
    sub: str  # 프로젝트 안 하위 경로("" 이면 루트)
    pool: str  # personal | company


def locate(root: Path, directory: Path) -> Located:
    parts = directory.relative_to(root).parts
    if not parts:
        return Located(root.name, "", "personal")
    pool = "company" if parts[0] in COMPANY_CONTAINERS else "personal"
    if parts[0] in CONTAINERS and len(parts) > 1:
        return Located("/".join(parts[:2]), "/".join(parts[2:]), pool)
    return Located(parts[0], "/".join(parts[1:]), pool)


def _tag(item: Item, where: Located) -> Item:
    item.scope = "project"
    item.project, item.pool = where.key, where.pool
    item.hint_group, item.hint_layer = "projects", "nested" if where.sub else "root"
    item.group = f"{where.key}/{where.sub}" if where.sub else where.key
    item.sessions = SESSIONS
    return item


def _json_file(ctx: Ctx, path: Path, category: str, engine: str, where: Located, *, name: str, description: str) -> list[Item]:
    try:
        text, _ = ctx.files.read_text(path)
        doc: Any = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text or "{}")
    except (Denied, ValueError):
        item = Item(category=category, name=name, key=str(path), scope="project", engine=engine, path=ctx.disp(path),
                    present=False, active=False, active_note="읽지 못함", kind="text", description=description)
        item.warnings.append("읽기·해석 실패(허용 목록 밖이거나 형식 오류)")
        return [_tag(item, where)]
    if not isinstance(doc, dict):
        doc = {"value": doc}
    mt = _mtime(path)
    disp = ctx.disp(path)
    items: list[Item] = []
    if path.name == ".mcp.json":
        servers = doc.get("mcpServers") if isinstance(doc.get("mcpServers"), dict) else {}
        items += _mcp_items(ctx, servers, engine=engine, source=disp, group=where.key, scope="project", mtime=mt,
                            sessions=SESSIONS)
        if not servers:
            items.append(json_item(category, name, f"{path}|empty", doc, scope="project", engine=engine,
                                   description="MCP 서버가 비어 있다", path=disp, mtime=mt, active=False))
    elif category == "hook":
        items = _hook_items(ctx, doc.get("hooks", doc), engine=engine, source_path=path, config_key="hooks", mtime=mt)
        if not items:
            items.append(json_item(category, name, f"{path}|all", doc, scope="project", engine=engine,
                                   description="등록된 훅 없음", path=disp, mtime=mt, active=False))
    else:
        summary = ", ".join(list(doc)[:12])
        items.append(json_item(category, name, f"{path}|all", doc, scope="project", engine=engine,
                               description=f"{description} · 키: {summary}" if summary else description, path=disp,
                               mtime=mt, active=True))
        if engine == "claude" and isinstance(doc.get("hooks"), dict):
            items += _hook_items(ctx, doc["hooks"], engine=engine, source_path=path, config_key="hooks", mtime=mt)
    return [_tag(i, where) for i in items]


def _scan_dir(ctx: Ctx, directory: Path, where: Located) -> list[Item]:
    out: list[Item] = []
    for pattern, engine, category in FILE_SPECS:
        for path in _candidates(directory, pattern):
            if category in {"instruction", "rule", "agent", "skill", "memory"} and path.suffix != ".toml":
                label = path.parent.name if path.name == "SKILL.md" else path.name
                item = file_item(ctx, path, category, name=label, scope="project", engine=engine,
                                 description=_describe(category, engine, path), frontmatter=category in {"skill", "agent"})
                item.active, item.active_note = True, "작업 디렉터리가 이 프로젝트일 때 로드(엔진 동작은 확인 필요)"
                if category == "instruction":
                    length_warnings(item, doc="claude" if engine == "claude" else "codex" if engine == "codex" else "")
                out.append(_tag(item, where))
            elif path.suffix == ".toml" and category == "agent":
                item = file_item(ctx, path, category, name=path.stem, scope="project", engine=engine, kind="text",
                                 frontmatter=False, description="Codex 프로젝트 에이전트")
                item.active = True
                out.append(_tag(item, where))
            else:
                out += _json_file(ctx, path, category, engine, where, name=path.name,
                                  description={"setting": "프로젝트 설정", "hook": "프로젝트 훅", "mcp": "프로젝트 MCP"}[category])
    return out


def _candidates(directory: Path, pattern: str) -> list[Path]:
    found = sorted(directory.glob(pattern)) if "*" in pattern else [directory / pattern]
    return [p for p in found if p.is_file()]


def _describe(category: str, engine: str, path: Path) -> str:
    return {"instruction": "프로젝트 지침", "rule": "프로젝트 규칙", "agent": "프로젝트 에이전트·커맨드",
            "skill": "프로젝트 스킬", "memory": "프로젝트 메모리"}[category]


def collect_projects(ctx: Ctx, roots: list[Path], skip: tuple[Path, ...] = ()) -> list[Item]:
    """개발 루트 아래 프로젝트별 지침·설정. skip 은 다른 그룹이 따로 다루는 폴더(Qwen 하네스 저장소)다."""
    out: list[Item] = []
    skipped = {os.path.realpath(s) for s in skip}
    budget = [PROJECT_SCAN_DIRS]
    for root in roots:
        if not root.is_dir():
            continue
        stack: list[tuple[Path, int]] = [(root, 0)]
        while stack and budget[0] > 0:
            current, depth = stack.pop()
            budget[0] -= 1
            if os.path.realpath(current) in skipped:
                continue
            if depth > 0 and (current / ".git").is_file():
                continue  # git worktree 는 원본 프로젝트와 같은 파일이라 건너뛴다
            where = locate(root, current)
            if depth > 0 and not where.sub and not (depth == 1 and current.name in CONTAINERS):
                ctx.projects_seen[where.key] = where.pool
            out += _scan_dir(ctx, current, where)
            if depth < SCAN_DEPTH:
                try:
                    subs = sorted(e.path for e in os.scandir(current)
                                  if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS
                                  and not e.name.startswith("."))
                except OSError:
                    subs = []
                stack.extend((Path(s), depth + 1) for s in reversed(subs))
    mark_aliases(out)
    return out
