"""자연어(MCP) 경계: 에이전트는 공유 대상 확인과 초안 생성·읽기까지만 한다."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from context_sharing_support import FakeGateway, agent_headers, make_project

from ttakkari.api import context_sharing as api
from ttakkari.context_sharing.gateway import GatewayControl
from ttakkari.main import app
from ttakkari.orchestrator import mcp_server as ms
from ttakkari.orchestrator import tool_policy

pytestmark = pytest.mark.security
READ = {"context_share_projects", "context_share_sources", "context_share_drafts", "context_share_draft"}
WRITE = {"context_share_draft_create"}


async def tools():
    return {t.name: t for t in await ms.server.list_tools()}


async def test_only_inspect_and_draft_tools_exist():
    names = {n for n in await tools() if n.startswith("context_share_")}
    assert names == READ | WRITE
    everything = set(await tools())
    for forbidden in ("publish", "revoke", "issue_key", "key_issue", "approve_digest"):
        assert not [n for n in everything if forbidden in n and "context" in n], forbidden


async def test_draft_tool_cannot_confirm_work_scope_or_approve_anything():
    schema = (await tools())["context_share_draft_create"].input_schema
    assert set(schema["properties"]) == {"entity_id", "document_ids", "title", "summary"}
    assert set(schema["required"]) == {"entity_id", "document_ids"}


def test_policy_marks_reads_read_only_and_the_draft_write_separately():
    assert READ <= tool_policy.READ_ONLY_TOOLS
    assert WRITE.isdisjoint(tool_policy.READ_ONLY_TOOLS)
    assert WRITE <= tool_policy.CONTEXT_SHARE_TOOLS <= tool_policy.AUTO_APPROVED_TOOLS
    assert not [n for n in tool_policy.AUTO_APPROVED_TOOLS if "publish" in n or "revoke" in n]


async def test_tools_call_only_agent_context_sharing_routes(monkeypatch):
    call = AsyncMock(return_value="{}")
    monkeypatch.setattr(ms, "_call", call)
    pid, did = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    await ms.server.call_tool("context_share_projects", {})
    await ms.server.call_tool("context_share_sources", {"entity_id": pid})
    await ms.server.call_tool("context_share_drafts", {"entity_id": pid})
    await ms.server.call_tool("context_share_draft", {"draft_id": did})
    await ms.server.call_tool("context_share_draft_create", {"entity_id": pid, "document_ids": [did], "title": "t"})
    seen = [(c.args[0], c.args[1], c.kwargs["prefix"]) for c in call.await_args_list]
    assert seen == [("GET", "/projects", "/api/agent/context-sharing"),
                    ("GET", f"/projects/{pid}/sources", "/api/agent/context-sharing"),
                    ("GET", "/drafts", "/api/agent/context-sharing"),
                    ("GET", f"/drafts/{did}", "/api/agent/context-sharing"),
                    ("POST", "/drafts", "/api/agent/context-sharing")]
    assert call.await_args_list[-1].args[2] == {"entity_id": pid, "document_ids": [did], "title": "t", "summary": ""}


async def test_agent_draft_through_the_tool_path_reaches_a_draft_but_never_the_gateway(monkeypatch):
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake)
    try:
        project = await make_project("Alpha", {"a.md": "# 공개 설명\n"})
        headers = await agent_headers()
        transport = httpx.ASGITransport(app=app)

        async def via_asgi(method, path, body=None, *, prefix="/api/orchestrator", params=None, timeout=30):
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
                r = await c.request(method, f"{prefix}{path}", json=body, params=params)
            return json.dumps(r.json(), ensure_ascii=False) if r.status_code < 400 else json.dumps({"error": r.status_code, **r.json()})

        monkeypatch.setattr(ms, "_call", via_asgi)
        text = (await ms.server.call_tool("context_share_projects", {}))
        assert "Alpha" in json.dumps(text, default=str, ensure_ascii=False)
        created = await ms.server.call_tool("context_share_draft_create", {
            "entity_id": project["entity_id"], "document_ids": [project["docs"]["a.md"]]})
        assert "created_by" in json.dumps(created, default=str) and "agent" in json.dumps(created, default=str)
        assert fake.calls == []
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)
