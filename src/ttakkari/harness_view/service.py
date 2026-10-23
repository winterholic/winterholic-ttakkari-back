"""하네스 스냅샷 수집·캐시·검색. 읽기 전용이며 어떤 파일도 쓰거나 실행하지 않는다.

파일 읽기는 스레드에서 돌리고, DB 에서는 계정·앱 설정·고정 기억만 읽는다. 클라이언트 입력은 필터 문자열과
항목 id 뿐이고 경로는 받지 않는다.
"""
from __future__ import annotations

import asyncio
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import HOME, get_settings
from ttakkari.context.store import pinned_brief
from ttakkari.harness_view import layers as layers_mod
from ttakkari.harness_view import sources, ttakkari_side
from ttakkari.harness_view.model import CATEGORIES, ENGINES, SCOPES, Item
from ttakkari.harness_view.safe import mask_config
from ttakkari.models import AppSetting, EngineAccount

TTL_SECONDS = 20.0
PAGE_MAX = 60
BODY_DEFAULT = 12_000
BODY_MAX = 40_000
WARNING_KINDS = (("broken_ref", ("깨진 참조", "훅 스크립트 없음", "installed_plugins", "없는 스킬")),
                 ("duplicate", ("중복 이름",)),
                 ("too_long", ("줄로 길다", "자로 길다", "32KiB", "로드되지 않는다", "주입되지 않는다")))


def user_home() -> Path:
    """시험에서 바꿀 수 있게 함수로 둔다. 운영에서는 로그인 사용자의 홈이다."""
    return HOME


def project_roots() -> list[Path]:
    return [HOME / "development"]


@dataclass
class Snapshot:
    built_at: float
    items: list[Item]
    by_id: dict[str, Item] = field(default_factory=dict)
    warnings: list[dict[str, Any]] = field(default_factory=list)


_cache: Snapshot | None = None
_lock = asyncio.Lock()


def reset_cache() -> None:
    global _cache
    _cache = None


def classify(message: str) -> str:
    for kind, needles in WARNING_KINDS:
        if any(n in message for n in needles):
            return kind
    return "other"


def collect_warnings(items: list[Item]) -> list[dict[str, Any]]:
    """경고 목록. 중복 이름은 이름마다 한 줄로 묶어 개수를 붙인다."""
    out: list[dict[str, Any]] = []
    dups: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in items:
        for message in item.warnings:
            kind = classify(message)
            row = {"item_id": item.id, "name": item.name, "category": item.category, "engine": item.engine,
                   "kind": kind, "message": message, "count": 1}
            if kind == "duplicate":
                key = (item.category, item.engine, item.name.lower())
                if key in dups:
                    dups[key]["count"] += 1
                    continue
                row["message"] = "중복 이름"
                dups[key] = row
            out.append(row)
    for row in dups.values():
        if row["count"] > 1:
            row["message"] = f"중복 이름 {row['count']}곳"
    return out


async def _db_context(db: AsyncSession) -> tuple[list[dict[str, Any]], dict[str, Any], str]:
    accounts: list[dict[str, Any]] = []
    for acc in await db.scalars(select(EngineAccount).order_by(EngineAccount.priority, EngineAccount.name)):
        view = {"name": acc.name, "engine": acc.engine, "pool": acc.pool, "enabled": acc.enabled,
                "priority": acc.priority, "max_used_percent": acc.max_used_percent,
                "config_dir": str(acc.config_dir) if acc.config_dir else None,
                "model": getattr(acc, "model", None)}
        accounts.append({"id": str(acc.id), "name": acc.name, "engine": acc.engine, "view": view})
    rows = {row.key: row.value for row in await db.scalars(select(AppSetting))}
    return accounts, {k: v for k, v in rows.items() if k == "qwen_worker_activation"}, await pinned_brief(db, scope="all")


def _build(ctx: sources.Ctx, tools: list[Any], only: frozenset[str], roots: list[Path]) -> list[Item]:
    sources.load_claude_settings(ctx)
    sources.load_codex_config(ctx)
    items: list[Item] = []
    items += sources.collect_claude_instructions(ctx)
    items += sources.collect_codex_instructions(ctx)
    items += sources.collect_gemini_instructions(ctx)
    items += sources.collect_projects(ctx, roots)
    items += sources.collect_claude_settings(ctx)
    items += sources.collect_codex_settings(ctx)
    items += sources.collect_gemini_settings(ctx)
    items += sources.collect_skills(ctx)
    items += sources.collect_claude_agents_commands_rules(ctx)
    items += sources.collect_memory(ctx)
    items += ttakkari_side.collect_prompts(ctx)
    if ctx.pinned_brief:
        items.append(sources.text_item(
            "prompt", "고정 기억(핀)", "db|pinned_brief", ctx.pinned_brief, scope="ttakkari", engine="ttakkari",
            group="DB", source="db", path="DB 맥락 도서관 pinned_brief",
            description="모든 비접수 Run 에 합쳐지는 사용자 고정 기억(마스킹됨)", sessions=("lead", "worker", "orchestrator")))
    items += ttakkari_side.collect_guard(ctx)
    items += ttakkari_side.collect_policy(ctx)
    items += ttakkari_side.collect_capabilities(ctx)
    items += ttakkari_side.collect_ttakkari_tools(tools, only)
    sources.check_refs(ctx, items)
    sources.check_skill_overrides(items)
    for item in items:
        item.finalize()
    return items


async def snapshot(db: AsyncSession, *, refresh: bool = False) -> Snapshot:
    global _cache
    async with _lock:
        if not refresh and _cache is not None and time.monotonic() - _cache.built_at < TTL_SECONDS:
            return _cache
        settings = get_settings()
        accounts, app_settings, brief = await _db_context(db)
        from ttakkari.orchestrator import mcp_server
        listed = await mcp_server.server.list_tools()
        tools = list(listed[0] if isinstance(listed, tuple) else listed)
        home = user_home()
        roots = project_roots()
        files = sources.build_safe_files(home, settings.home, roots)
        ctx = sources.Ctx(user_home=home, ttakkari_home=settings.home, files=files, settings=settings,
                          now=time.time(), accounts=accounts, app_settings=app_settings, pinned_brief=brief)
        only = ttakkari_side.orchestrator_only_names()
        items = await asyncio.to_thread(_build, ctx, tools, only, roots)
        snap = Snapshot(built_at=time.monotonic(), items=items, by_id={i.id: i for i in items})
        snap.warnings = collect_warnings(items)
        _cache = snap
        return snap


def _count(items: list[Item], key) -> dict[str, int]:
    return dict(Counter(key(i) for i in items))


def summary(snap: Snapshot) -> dict[str, Any]:
    items = snap.items
    categories = []
    for cid, label in CATEGORIES.items():
        subset = [i for i in items if i.category == cid]
        categories.append({
            "id": cid, "label": label, "total": len(subset), "present": sum(1 for i in subset if i.present),
            "missing": sum(1 for i in subset if not i.present),
            "inactive": sum(1 for i in subset if i.present and i.active is False),
            "warnings": sum(1 for i in subset if i.warnings)})
    kinds = Counter(w["kind"] for w in snap.warnings)
    return {
        "generated_at": time.time() - (time.monotonic() - snap.built_at),
        "total": len(items), "present": sum(1 for i in items if i.present),
        "categories": categories,
        "engines": [{"id": e, "total": n} for e, n in sorted(_count(items, lambda i: i.engine).items(),
                                                             key=lambda kv: ENGINES.index(kv[0]) if kv[0] in ENGINES else 99)],
        "scopes": [{"id": s, "total": _count(items, lambda i: i.scope).get(s, 0)} for s in SCOPES],
        "warning_total": len(snap.warnings),
        "warning_kinds": {k: kinds.get(k, 0) for k in ("broken_ref", "duplicate", "too_long", "other")},
        "warnings": snap.warnings[:40],
        "session_kinds": list(layers_mod.SESSION_KINDS), "layer_engines": list(layers_mod.LAYER_ENGINES),
    }


def _snippet(text: str, needle: str, width: int = 70) -> str:
    low = text.lower()
    at = low.find(needle)
    if at < 0:
        return ""
    start = max(0, at - width)
    out = text[start:at + len(needle) + width].replace("\n", " ")
    return ("…" if start else "") + out + ("…" if at + len(needle) + width < len(text) else "")


def search(snap: Snapshot, *, q: str = "", categories: set[str] | None = None, engine: str | None = None,
           scope: str | None = None, active: bool | None = None, warned: bool = False, session: str | None = None,
           present: bool | None = None, group: str | None = None, limit: int = 30, cursor: int = 0) -> dict[str, Any]:
    tokens = [t for t in q.lower().split() if t][:6]
    hits: list[tuple[int, int, Item, str]] = []
    cat_order = list(CATEGORIES)
    for item in snap.items:
        if categories and item.category not in categories:
            continue
        if engine and item.engine != engine:
            continue
        if scope and item.scope != scope:
            continue
        if active is not None and item.active is not active:
            continue
        if warned and not item.warnings:
            continue
        if session and session not in item.sessions:
            continue
        if present is not None and item.present is not present:
            continue
        if group and item.group != group:
            continue
        score = 0
        if tokens:
            if not all(t in item._search for t in tokens):
                continue
            name, desc = item.name.lower(), item.description.lower()
            score = sum(30 if t in name else 10 if t in desc else 3 if t in (item.path or "").lower() else 1
                        for t in tokens) + (20 if name == q.lower().strip() else 0)
        hits.append((-score, cat_order.index(item.category), item, ""))
    hits.sort(key=lambda h: (h[0], h[1], h[2].name.lower()))
    page = hits[cursor:cursor + limit]
    rows = []
    for _, _, item, _ in page:
        row = item.summary()
        if tokens:
            where = [w for w, text in (("name", item.name), ("description", item.description), ("path", item.path or ""))
                     if any(t in text.lower() for t in tokens)]
            if any(t in item.body.lower() for t in tokens):
                where.append("body")
                row["snippet"] = _snippet(item.body, next(t for t in tokens if t in item.body.lower()))
            row["matched_in"] = where
        rows.append(row)
    nxt = cursor + len(page)
    return {"total": len(hits), "items": rows, "next_cursor": nxt if nxt < len(hits) else None,
            "facets": {"groups": sorted({h[2].group for h in hits if h[2].group})[:60]}}


def detail(snap: Snapshot, item_id: str, *, offset: int = 0, length: int = BODY_DEFAULT) -> dict[str, Any] | None:
    item = snap.by_id.get(item_id)
    if item is None:
        return None
    length = max(500, min(length, BODY_MAX))
    chunk = item.body[offset:offset + length]
    end = offset + len(chunk)
    row = item.summary()
    row.update({
        "body": chunk, "offset": offset, "next_offset": end if end < len(item.body) else None,
        "truncated_source": item.truncated, "real_path": item.real_path,
        "frontmatter": item.meta.get("frontmatter"), "meta": mask_config({k: v for k, v in item.meta.items() if k != "frontmatter"}),
    })
    return row


async def layers(db: AsyncSession, snap: Snapshot, *, session: str, engine: str, pool: str) -> dict[str, Any]:
    return await layers_mod.build(db, snap.items, session=session, engine=engine, pool=pool, settings=get_settings())
