"""오케스트레이터 Run 에 붙는 stdio MCP 서버(ADR 0006). 엔진이 띄우고, 백엔드 /api/orchestrator 를 부른다.

env: TTK_ORCH_TOKEN(세션 한정 토큰), TTK_API_URL(백엔드 주소). 직접 실행하지 않는다.
"""

from __future__ import annotations

import json
import os
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer

server = MCPServer(
    name="ttakkari",
    instructions="Winterholic Ttakkari worker sessions. Delegate work with spawn_worker; results come back as "
                 "'[작업 보고]' messages, so do not poll.",
)


async def _call(method: str, path: str, body: dict[str, Any] | None = None) -> str:
    base = os.environ.get("TTK_API_URL", "http://127.0.0.1:8787").rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ.get('TTK_ORCH_TOKEN', '')}"}
    async with httpx.AsyncClient(base_url=base, headers=headers, timeout=30) as c:
        r = await c.request(method, f"/api/orchestrator{path}", json=body)
    try:
        data = r.json()
    except ValueError:
        data = {"message": r.text[:500]}
    if r.is_error:
        # 오류도 문자열로 돌려줘 모델이 읽고 대응하게 한다.
        return json.dumps({"error": r.status_code, **(data if isinstance(data, dict) else {"detail": data})},
                          ensure_ascii=False)
    return json.dumps(data, ensure_ascii=False)


@server.tool()
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


@server.tool()
async def message_worker(worker_id: str, prompt: str) -> str:
    """Send a follow-up instruction to an existing worker. It continues the same conversation and is queued if
    the worker is busy."""
    return await _call("POST", f"/workers/{worker_id}/messages", {"prompt": prompt})


@server.tool()
async def list_workers() -> str:
    """List this orchestrator's worker sessions with status and a short last result."""
    return await _call("GET", "/workers")


@server.tool()
async def get_worker(worker_id: str) -> str:
    """Get one worker's status, latest result (up to 4000 chars), error and produced file names."""
    return await _call("GET", f"/workers/{worker_id}")


@server.tool()
async def stop_worker(worker_id: str) -> str:
    """Cancel the worker's running and queued instructions."""
    return await _call("POST", f"/workers/{worker_id}/stop")


if __name__ == "__main__":
    server.run("stdio")
