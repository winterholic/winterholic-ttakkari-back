"""모든 Run 에 붙는 stdio MCP 서버 `ttakkari`(ADR 0006·0007). 엔진이 띄우고 백엔드 API 를 부른다.

env: TTK_ORCH_TOKEN(세션 한정 토큰), TTK_API_URL(백엔드 주소), TTK_ROLE(agent | orchestrator). 직접 실행하지 않는다.
작업 세션 도구는 orchestrator 일 때만 등록한다(목록에 안 보이게).
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer

ROLE = os.environ.get("TTK_ROLE", "orchestrator")
server = MCPServer(
    name="ttakkari",
    instructions="The user's own Ttakkari app: long-term memory about the user (recall/remember/link), files "
                 "(list_artifacts/share_file), notifications, other conversations, and the user's memos and calendar "
                 "(Asia/Seoul)."
                 + (" As orchestrator, delegate work with spawn_worker; results come back as '[작업 보고]' messages, "
                    "so do not poll." if ROLE == "orchestrator" else ""),
)


async def _call(method: str, path: str, body: dict[str, Any] | None = None, *, prefix: str = "/api/orchestrator",
                params: dict[str, Any] | None = None) -> str:
    api = os.environ.get("TTK_API_URL", "http://127.0.0.1:8787").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('TTK_ORCH_TOKEN', '')}"}
    async with httpx.AsyncClient(base_url=api, headers=headers, timeout=30) as c:
        r = await c.request(method, f"{prefix}{path}", json=body,
                            params={k: v for k, v in (params or {}).items() if v is not None} or None)
    try:
        data = r.json()
    except ValueError:
        data = {"message": r.text[:500]}
    if r.is_error:
        # 오류도 문자열로 돌려줘 모델이 읽고 대응하게 한다.
        return json.dumps({"error": r.status_code, **(data if isinstance(data, dict) else {"detail": data})},
                          ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


# ---- 공통(모든 Run)

async def _agent(method: str, path: str, body: dict[str, Any] | None = None, **params: Any) -> str:
    return await _call(method, path, body, prefix="/api/agent", params=params)


@server.tool()
async def library_catalog() -> str:
    """Browse context library categories and project landmarks before choosing what to recall.
    Returns names, aliases, summaries and verified source paths; no document bodies."""
    return await _agent("GET", "/library")


@server.tool()
async def recall(query: str, k: int = 8) -> str:
    """Search saved facts, entities/relations and indexed documents without an embedding model.
    Use project names or aliases and a few concrete keywords. Hits include source paths and timestamps;
    read the original for detail, and try different keywords when terminology differs."""
    return await _agent("POST", "/recall", {"query": query, "k": k})


@server.tool()
async def remember(content: str, kind: Literal["fact", "preference", "decision", "event", "instruction", "note"] = "fact",
                   entities: list[str] | None = None) -> str:
    """Save one durable fact about the user or their work, written to stand alone. `entities` are names it is
    about (people, projects, tools); they are created if missing. Never store secrets."""
    return await _agent("POST", "/remember", {"content": content, "kind": kind, "entities": entities or []})


@server.tool()
async def get_entity(name: str) -> str:
    """Look up a person/project/tool/topic by name or alias, with its relations and linked memories."""
    return await _agent("GET", f"/entities/{name}")


@server.tool()
async def describe_entity(name: str, type: Literal["person", "project", "org", "place", "tool", "topic", "preference", "other"] = "other",
                          summary: str | None = None, aliases: list[str] | None = None) -> str:
    """Create or update an entity (name, type, one-paragraph summary, aliases)."""
    return await _agent("POST", "/entities", {"name": name, "type": type, "summary": summary, "aliases": aliases or []})


@server.tool()
async def link(subject: str, predicate: str, object: str, note: str | None = None) -> str:
    """Record a relation between two entities, e.g. link('winterholic', 'works_on', 'Ttakkari')."""
    return await _agent("POST", "/link", {"subject": subject, "predicate": predicate, "object": object, "note": note})


@server.tool()
async def forget(memory_id: str) -> str:
    """Archive a memory you saved that turned out wrong or outdated (user-entered memories cannot be removed)."""
    return await _agent("POST", "/forget", {"id": memory_id})


@server.tool()
async def list_artifacts(query: str | None = None, kind: str | None = None, limit: int = 20) -> str:
    """List files in the user's library (results and uploads), newest first, with their paths on disk."""
    return await _agent("GET", "/artifacts", q=query, kind=kind, limit=limit)


@server.tool()
async def share_file(path: str) -> str:
    """Put a file (inside the workspace, permission folders or inbox) into the user's library so they can open
    and download it on their phone."""
    return await _agent("POST", "/share", {"path": path})


@server.tool()
async def notify_user(title: str, body: str = "") -> str:
    """Send a push notification to the user's phone. Only for things they must see now."""
    return await _agent("POST", "/notify", {"title": title, "body": body})


@server.tool()
async def list_sessions(limit: int = 20) -> str:
    """List the user's recent conversations in Ttakkari."""
    return await _agent("GET", "/sessions", limit=limit)


@server.tool()
async def read_session(session_id: str, last: int = 10) -> str:
    """Read the last turns (instruction and result) of another conversation."""
    return await _agent("GET", f"/sessions/{session_id}", last=last)


# ---- 메모·캘린더(ADR 0008). 시각은 모두 Asia/Seoul 이다.

@server.tool()
async def list_memo_categories() -> str:
    """List the user's memo categories in their order. A memo belongs to at most one category (or none)."""
    return await _agent("GET", "/memo-categories")


@server.tool()
async def list_memos(tags: list[str] | None = None, category: str | None = None, limit: int = 30) -> str:
    """List the user's memos (pinned first, then newest). Returns title, category, tags and a short excerpt; use
    get_memo for the full Markdown body. `tags` keeps memos that have all of them; `category` is a category name,
    or "none" for uncategorized memos."""
    return await _agent("GET", "/memos", tag=tags or None, category=category, limit=limit)


@server.tool()
async def search_memos(query: str, tags: list[str] | None = None, category: str | None = None, limit: int = 20) -> str:
    """Search memos by words in the title, body or tags, optionally narrowed by `tags` (all must match) and
    `category` (a name, or "none" for uncategorized)."""
    return await _agent("GET", "/memos", q=query, tag=tags or None, category=category, limit=limit)


@server.tool()
async def get_memo(memo_id: str) -> str:
    """Read one memo with its full Markdown body and current `rev`."""
    return await _agent("GET", f"/memos/{memo_id}")


@server.tool()
async def create_memo(body: str, title: str = "", tags: list[str] | None = None, category: str | None = None,
                      pinned: bool = False) -> str:
    """Save a new memo (Markdown). Use for 'jot this down' requests. Give a short title and 1-3 tags when obvious.
    `category` must be an existing category name (see list_memo_categories); leave it out when none fits."""
    return await _agent("POST", "/memos", {"title": title, "body": body, "tags": tags or [], "category": category,
                                           "pinned": pinned})


@server.tool()
async def update_memo(memo_id: str, append: str | None = None, body: str | None = None, title: str | None = None,
                      tags: list[str] | None = None, category: str | None = None, pinned: bool | None = None,
                      rev: int | None = None) -> str:
    """Edit a memo. On memos the user wrote you may only `append` text and change tags/category/pinned; title and body
    can be replaced only on memos you created. `tags` replaces the whole list. `category` is an existing category
    name, or "" to make it uncategorized. Pass `rev` from get_memo to avoid overwriting a newer edit."""
    patch = {"append": append, "body": body, "title": title, "tags": tags, "category": category, "pinned": pinned,
             "rev": rev}
    return await _agent("PATCH", f"/memos/{memo_id}", {k: v for k, v in patch.items() if v is not None})


@server.tool()
async def delete_memo(memo_id: str) -> str:
    """Archive (soft-delete) a memo you created. Memos the user wrote cannot be deleted by you."""
    return await _agent("DELETE", f"/memos/{memo_id}")


ReminderOpt = Literal["at_start", "10m", "30m", "1h", "1d", "day_9am", "prev_day_9am"]


@server.tool()
async def list_events(start_date: str, end_date: str, query: str | None = None) -> str:
    """List calendar events from start_date to end_date (YYYY-MM-DD, inclusive, at most 400 days), plus Korean public
    holidays in that range. Times are Asia/Seoul wall time: all-day events use dates, timed events use
    YYYY-MM-DDTHH:MM (end of an all-day event is inclusive). `reminder` is the push reminder option or null."""
    return await _agent("GET", "/events", **{"from": start_date, "to": end_date, "q": query})


@server.tool()
async def create_event(title: str, start: str, end: str | None = None, all_day: bool = False,
                       description: str = "", location: str = "", reminder: ReminderOpt | None = None) -> str:
    """Create an event in Asia/Seoul time. All-day: start/end are YYYY-MM-DD (end inclusive, defaults to start).
    Timed: YYYY-MM-DDTHH:MM (end defaults to one hour later). Recurring events are not supported (on hold).
    `reminder` (off by default; set it only when the user asks): timed events take at_start/10m/30m/1h/1d
    (before start); all-day events take day_9am or prev_day_9am (KST). The user gets a phone push at that time."""
    return await _agent("POST", "/events", {"title": title, "start": start, "end": end, "all_day": all_day,
                                            "description": description, "location": location, "reminder": reminder})


@server.tool()
async def update_event(event_id: str, title: str | None = None, start: str | None = None, end: str | None = None,
                       all_day: bool | None = None, description: str | None = None, location: str | None = None,
                       reminder: ReminderOpt | Literal["off"] | None = None) -> str:
    """Change fields of an event. Send only what changes; when changing all_day, send start and end too (and a
    reminder that fits the new kind if one is set). `reminder="off"` turns the reminder off."""
    patch: dict[str, Any] = {k: v for k, v in {"title": title, "start": start, "end": end, "all_day": all_day,
                                                "description": description, "location": location}.items() if v is not None}
    if reminder is not None:
        patch["reminder"] = None if reminder == "off" else reminder
    return await _agent("PATCH", f"/events/{event_id}", patch)


@server.tool()
async def delete_event(event_id: str) -> str:
    """Archive (soft-delete) an event you created. Events the user made cannot be deleted by you; tell them to remove it in the app."""
    return await _agent("DELETE", f"/events/{event_id}")


# ---- 오케스트레이터 전용

def orchestrator_tool(fn):
    return server.tool()(fn) if ROLE == "orchestrator" else fn


@orchestrator_tool
async def spawn_worker(title: str, prompt: str, engine: Literal["auto", "claude", "codex"] = "auto",
                       model: str | None = None,
                       effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None) -> str:
    """Start a new worker session that does a task in parallel. `title` is a short tab label (Korean is fine).
    `prompt` must be self-contained: the worker does not see this conversation. engine 'auto' prefers Claude Code
    and falls back to Codex on usage limits. Returns the worker_id."""
    body: dict[str, Any] = {"title": title, "prompt": prompt, "engine": engine}
    if model:
        body["model"] = model
    if effort:
        body["effort"] = effort
    return await _call("POST", "/workers", body)


@orchestrator_tool
async def message_worker(worker_id: str, prompt: str) -> str:
    """Send a follow-up instruction to an existing worker. It continues the same conversation and is queued if
    the worker is busy."""
    return await _call("POST", f"/workers/{worker_id}/messages", {"prompt": prompt})


@orchestrator_tool
async def list_workers() -> str:
    """List this orchestrator's worker sessions with status and a short last result."""
    return await _call("GET", "/workers")


@orchestrator_tool
async def get_worker(worker_id: str) -> str:
    """Get one worker's status, latest result (up to 4000 chars), error and produced file names."""
    return await _call("GET", f"/workers/{worker_id}")


@orchestrator_tool
async def stop_worker(worker_id: str) -> str:
    """Cancel the worker's running and queued instructions."""
    return await _call("POST", f"/workers/{worker_id}/stop")


if __name__ == "__main__":
    server.run("stdio")
