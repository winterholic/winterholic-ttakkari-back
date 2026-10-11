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
                 "(list_artifacts/share_file), notifications and other conversations."
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
async def recall(query: str, k: int = 8) -> str:
    """Search long-term memory about the user: saved facts/preferences/decisions, people/projects (with relations),
    and indexed personal documents (work logs, notes). Use a short natural-language query in any language."""
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
