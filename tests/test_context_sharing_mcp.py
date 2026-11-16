"""자연어(MCP) 경계: 명시 요청이 있을 때 지정 프로젝트만 게시·관리한다."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from context_sharing_support import FakeGateway, agent_headers, make_project

from ttakkari.api import context_sharing as api
from ttakkari.api.assistant import TOOLS as assistant_tools
from ttakkari.context_sharing.gateway import GatewayControl
from ttakkari.main import app
from ttakkari.orchestrator import mcp_server as ms
from ttakkari.orchestrator import tool_policy

pytestmark = pytest.mark.security
READ = {"context_share_projects", "context_share_sources", "context_share_drafts", "context_share_draft",
        "context_share_keys", "context_share_site"}
WRITE = {"context_share_draft_create", "context_share_publish", "context_share_key_issue",
         "context_share_key_revoke", "context_share_site_set"}


async def tools():
    return {t.name: t for t in await ms.server.list_tools()}


async def test_context_sharing_tools_are_scoped_to_project_and_management_actions():
    names = {n for n in await tools() if n.startswith("context_share_")}
    assert names == READ | WRITE
    inventory = {item["name"]: item for item in assistant_tools if item["name"].startswith("context_share_")}
    assert set(inventory) == names
    assert all(item["group"] == "맥락 공유" for item in inventory.values())


async def test_draft_tool_cannot_confirm_work_scope_or_approve_anything():
    schema = (await tools())["context_share_draft_create"].input_schema
    assert set(schema["properties"]) == {"entity_id", "document_ids", "title", "summary"}
    assert set(schema["required"]) == {"entity_id", "document_ids"}


def test_policy_marks_reads_read_only_and_the_draft_write_separately():
    assert READ <= tool_policy.READ_ONLY_TOOLS
    assert WRITE.isdisjoint(tool_policy.READ_ONLY_TOOLS)
    assert WRITE <= tool_policy.CONTEXT_SHARE_TOOLS <= tool_policy.AUTO_APPROVED_TOOLS


async def test_tools_call_only_agent_context_sharing_routes(monkeypatch):
    call = AsyncMock(return_value="{}")
    monkeypatch.setattr(ms, "_call", call)
    pid, did = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
    await ms.server.call_tool("context_share_projects", {})
    await ms.server.call_tool("context_share_sources", {"entity_id": pid})
    await ms.server.call_tool("context_share_drafts", {"entity_id": pid})
    await ms.server.call_tool("context_share_draft", {"draft_id": did})
    await ms.server.call_tool("context_share_draft_create", {"entity_id": pid, "document_ids": [did], "title": "t"})
    await ms.server.call_tool("context_share_publish", {"entity_id": pid})
    await ms.server.call_tool("context_share_keys", {"project_slug": "alpha"})
    await ms.server.call_tool("context_share_key_issue", {"project_slug": "alpha", "label": "agent"})
    await ms.server.call_tool("context_share_key_revoke", {"key_id": did})
    await ms.server.call_tool("context_share_site", {})
    await ms.server.call_tool("context_share_site_set", {"enabled": False})
    seen = [(c.args[0], c.args[1], c.kwargs["prefix"]) for c in call.await_args_list]
    assert seen == [("GET", "/projects", "/api/agent/context-sharing"),
                    ("GET", f"/projects/{pid}/sources", "/api/agent/context-sharing"),
                    ("GET", "/drafts", "/api/agent/context-sharing"),
                    ("GET", f"/drafts/{did}", "/api/agent/context-sharing"),
                    ("POST", "/drafts", "/api/agent/context-sharing"),
                    ("POST", "/publish", "/api/agent/context-sharing"),
                    ("GET", "/keys", "/api/agent/context-sharing"),
                    ("POST", "/keys", "/api/agent/context-sharing"),
                    ("POST", f"/keys/{did}/revoke", "/api/agent/context-sharing"),
                    ("GET", "/site", "/api/agent/context-sharing"),
                    ("PATCH", "/site", "/api/agent/context-sharing")]
    assert call.await_args_list[4].args[2] == {"entity_id": pid, "document_ids": [did], "title": "t", "summary": ""}
    assert call.await_args_list[5].args[2] == {"entity_id": pid}
    assert call.await_args_list[10].args[2] == {"enabled": False}


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


async def test_agent_management_tools_reach_publisher_keys_and_site_control(monkeypatch):
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake, public_url="https://ctx.example.test")
    try:
        project = await make_project("Alpha", {"a.md": "# 공개 설명\n본문\n"})
        headers = await agent_headers()
        transport = httpx.ASGITransport(app=app)

        async def via_asgi(method, path, body=None, *, prefix="/api/orchestrator", params=None, timeout=30):
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
                response = await client.request(method, f"{prefix}{path}", json=body, params=params)
            return json.dumps(response.json(), ensure_ascii=False)

        monkeypatch.setattr(ms, "_call", via_asgi)
        published = await ms.server.call_tool("context_share_publish", {"entity_id": project["entity_id"]})
        assert "\"status\": \"published\"" in str(published)
        assert len(fake.snapshots) == 1
        slug = next(iter(fake.projects))

        issued = await ms.server.call_tool("context_share_key_issue", {"project_slug": slug, "label": "agent"})
        issued_data = json.loads(issued.content[0].text)
        raw_key = issued_data["key"]
        assert raw_key
        assert issued_data["ttl_seconds"] == 3600 and issued_data["single_use"] is True
        listed = await ms.server.call_tool("context_share_keys", {"project_slug": slug})
        listed_data = json.loads(listed.content[0].text)
        assert len(listed_data["keys"]) == 1
        assert raw_key not in listed.content[0].text, "raw key leaked into key listing"

        key_id = listed_data["keys"][0]["id"]
        revoked = await ms.server.call_tool("context_share_key_revoke", {"key_id": key_id})
        assert "\"status\": \"revoked\"" in str(revoked)
        state = await ms.server.call_tool("context_share_site", {})
        assert "\"enabled\": true" in str(state)
        closed = await ms.server.call_tool("context_share_site_set", {"enabled": False})
        assert "\"enabled\": false" in str(closed)
        assert fake.site_enabled is False
        reopened = await ms.server.call_tool("context_share_site_set", {"enabled": True})
        assert "\"enabled\": true" in str(reopened)
        assert fake.site_enabled is True
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)


async def test_publish_tool_rejects_high_risk_and_work_scope_sources(monkeypatch):
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake, public_url="https://ctx.example.test")
    try:
        risky = await make_project("Risky", {"a.md": "# config\nhttp://localhost:8787\n"})
        work = await make_project("Work", {"a.md": "# work context\ninternal summary\n"}, scope="work")
        headers = await agent_headers()
        transport = httpx.ASGITransport(app=app)

        async def via_asgi(method, path, body=None, *, prefix="/api/orchestrator", params=None, timeout=30):
            async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
                response = await client.request(method, f"{prefix}{path}", json=body, params=params)
            return json.dumps(response.json(), ensure_ascii=False) if response.status_code < 400 else json.dumps(
                {"error": response.status_code, **response.json()}, ensure_ascii=False)

        monkeypatch.setattr(ms, "_call", via_asgi)
        risky_result = await ms.server.call_tool("context_share_publish", {"entity_id": risky["entity_id"]})
        assert json.loads(risky_result.content[0].text)["code"] == "sensitive_content_blocked"
        work_result = await ms.server.call_tool("context_share_publish", {"entity_id": work["entity_id"]})
        assert json.loads(work_result.content[0].text)["code"] == "work_scope_owner_only"
        assert fake.snapshots == {}
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)


async def test_publish_tool_description_requires_direct_user_request_and_ignores_untrusted_content():
    description = (await tools())["context_share_publish"].description
    assert "directly asks" in description
    assert "untrusted text" in description
