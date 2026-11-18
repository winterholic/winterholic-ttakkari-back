"""자연어(MCP) 경계: 명시 요청이 있을 때 지정 프로젝트만 게시·관리한다."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from context_sharing_support import FakeGateway, agent_headers, make_project
from sqlalchemy import text

from ttakkari.api import context_sharing as api
from ttakkari.api.assistant import TOOLS as assistant_tools
from ttakkari.context_sharing.gateway import GatewayControl
from ttakkari.db import sessionmaker
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


async def test_draft_and_publish_tools_take_optional_work_scope_confirmation_and_nothing_else():
    registry = await tools()
    draft = registry["context_share_draft_create"].input_schema
    assert set(draft["properties"]) == {"entity_id", "document_ids", "title", "summary", "confirm_work_scope"}
    assert set(draft["required"]) == {"entity_id", "document_ids"}
    assert draft["properties"]["confirm_work_scope"]["default"] is False
    publish = registry["context_share_publish"].input_schema
    assert set(publish["properties"]) == {"entity_id", "confirm_work_scope"}
    assert set(publish["required"]) == {"entity_id"}
    assert publish["properties"]["confirm_work_scope"]["default"] is False
    for name in ("context_share_draft_create", "context_share_publish"):
        assert "confirm_work_scope=true" in registry[name].description


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
    await ms.server.call_tool("context_share_draft_create", {"entity_id": pid, "document_ids": [did],
                                                             "confirm_work_scope": True})
    await ms.server.call_tool("context_share_publish", {"entity_id": pid, "confirm_work_scope": True})
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
                    ("POST", "/drafts", "/api/agent/context-sharing"),
                    ("POST", "/publish", "/api/agent/context-sharing"),
                    ("GET", "/keys", "/api/agent/context-sharing"),
                    ("POST", "/keys", "/api/agent/context-sharing"),
                    ("POST", f"/keys/{did}/revoke", "/api/agent/context-sharing"),
                    ("GET", "/site", "/api/agent/context-sharing"),
                    ("PATCH", "/site", "/api/agent/context-sharing")]
    assert call.await_args_list[4].args[2] == {"entity_id": pid, "document_ids": [did], "title": "t", "summary": ""}
    assert call.await_args_list[5].args[2] == {"entity_id": pid}
    assert call.await_args_list[6].args[2] == {"entity_id": pid, "document_ids": [did], "summary": "",
                                               "confirm_work_scope": True}
    assert call.await_args_list[7].args[2] == {"entity_id": pid, "confirm_work_scope": True}
    assert call.await_args_list[12].args[2] == {"enabled": False}


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


def _via_asgi(monkeypatch, headers):
    transport = httpx.ASGITransport(app=app)

    async def via_asgi(method, path, body=None, *, prefix="/api/orchestrator", params=None, timeout=30):
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
            response = await client.request(method, f"{prefix}{path}", json=body, params=params)
        return json.dumps(response.json(), ensure_ascii=False) if response.status_code < 400 else json.dumps(
            {"error": response.status_code, **response.json()}, ensure_ascii=False)

    monkeypatch.setattr(ms, "_call", via_asgi)


async def _result(name, args):
    return json.loads((await ms.server.call_tool(name, args)).content[0].text)


async def _audit(action):
    async with sessionmaker()() as db:
        rows = (await db.execute(text(
            "SELECT actor, outcome, detail FROM audit_logs WHERE action = :a ORDER BY id"), {"a": action})).all()
    return [(r[0], r[1], r[2]) for r in rows]


async def test_publish_tool_rejects_high_risk_and_unconfirmed_work_scope_sources(monkeypatch):
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake, public_url="https://ctx.example.test")
    try:
        risky = await make_project("Risky", {"a.md": "# config\nhttp://localhost:8787\n"})
        work = await make_project("Work", {"a.md": "# work context\ninternal summary\n"}, scope="work")
        _via_asgi(monkeypatch, await agent_headers())
        risky_result = await _result("context_share_publish", {"entity_id": risky["entity_id"]})
        assert risky_result["code"] == "sensitive_content_blocked"
        for args in ({"entity_id": work["entity_id"]}, {"entity_id": work["entity_id"], "confirm_work_scope": False}):
            denied = await _result("context_share_publish", args)
            assert denied["code"] == "work_scope_confirmation_required" and denied["error"] == 403
        denied_draft = await _result("context_share_draft_create", {
            "entity_id": work["entity_id"], "document_ids": [work["docs"]["a.md"]]})
        assert denied_draft["code"] == "work_scope_confirmation_required"
        assert fake.snapshots == {}
        assert [d["scope"] for _, _, d in await _audit("context_share.draft_create")] == ["personal"]
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)


async def test_work_scope_publish_with_confirmation_issues_key_and_audits_agent_confirmation(monkeypatch):
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake, public_url="https://ctx.example.test")
    try:
        work = await make_project("Work", {"a.md": "# work context\ninternal summary\n"}, scope="work")
        _via_asgi(monkeypatch, await agent_headers())
        published = await _result("context_share_publish", {"entity_id": work["entity_id"], "confirm_work_scope": True})
        assert published["draft"]["status"] == "published" and published["draft"]["scope"] == "work"
        assert published["draft"]["created_by"] == "agent" and len(fake.snapshots) == 1
        slug = next(iter(fake.projects))
        issued = await _result("context_share_key_issue", {"project_slug": slug, "label": "agent"})
        assert issued["key"] and issued["single_use"] is True and issued["ttl_seconds"] == 3600
        drafts = await _audit("context_share.draft_create")
        publishes = await _audit("context_share.publish")
        assert [(a, o, d["scope"], d["confirm_work_scope"]) for a, o, d in drafts] == [("agent", "ok", "work", True)]
        assert [(a, o, d["scope"], d["confirm_work_scope"]) for a, o, d in publishes] == [("agent", "ok", "work", True)]
        assert [a for a, _, _ in await _audit("context_share.key_issue")] == ["agent"]
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)


async def test_work_scope_draft_tool_with_confirmation_creates_agent_draft_without_gateway(monkeypatch):
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake)
    try:
        work = await make_project("Work", {"a.md": "# work context\n"}, scope="work")
        _via_asgi(monkeypatch, await agent_headers())
        draft = await _result("context_share_draft_create", {
            "entity_id": work["entity_id"], "document_ids": [work["docs"]["a.md"]], "confirm_work_scope": True})
        assert draft["scope"] == "work" and draft["created_by"] == "agent" and draft["status"] == "draft"
        assert fake.calls == []
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)


async def test_confirmed_work_scope_keeps_scanner_and_document_limits(monkeypatch):
    fake = FakeGateway()
    app.dependency_overrides[api.get_gateway] = lambda: GatewayControl(fake, public_url="https://ctx.example.test")
    try:
        risky = await make_project("WorkRisky", {"a.md": "# config\nhttp://localhost:8787\n"}, scope="work")
        many = await make_project("WorkMany", {f"d{i}.md": f"# doc {i}\n" for i in range(51)}, scope="work")
        big = await make_project("WorkBig", {"a.md": "x" * (200_000 + 1)}, scope="work")
        _via_asgi(monkeypatch, await agent_headers())
        confirmed = {"confirm_work_scope": True}
        assert (await _result("context_share_publish", {"entity_id": risky["entity_id"], **confirmed}))["code"] == "sensitive_content_blocked"
        assert (await _result("context_share_publish", {"entity_id": many["entity_id"], **confirmed}))["code"] == "too_many_documents"
        assert (await _result("context_share_publish", {"entity_id": big["entity_id"], **confirmed}))["code"] == "draft_too_large"
        assert fake.snapshots == {}
        assert all(d["confirm_work_scope"] for _, _, d in await _audit("context_share.draft_create"))
    finally:
        app.dependency_overrides.pop(api.get_gateway, None)


async def test_publish_tool_description_requires_direct_user_request_and_ignores_untrusted_content():
    description = (await tools())["context_share_publish"].description
    assert "directly asks" in description
    assert "untrusted text" in description
