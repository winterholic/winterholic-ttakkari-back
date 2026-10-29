import json

import pytest


@pytest.mark.asyncio
async def test_kb_mcp_exposes_summary_and_forwards_bounded_read_options(monkeypatch):
    from ttakkari.orchestrator import mcp_server

    calls = []

    async def fake_agent(method, path, body=None, **params):
        calls.append((method, path, body, params))
        return json.dumps({"ok": True})

    monkeypatch.setattr(mcp_server, "_agent", fake_agent)

    tools = {tool.name: tool for tool in await mcp_server.server.list_tools()}
    assert {"kb_summary", "kb_observations", "kb_search", "kb_document"} <= tools.keys()
    assert tools["kb_observations"].annotations.read_only_hint is True

    await mcp_server.kb_summary()
    await mcp_server.kb_search("revision audit", limit=7, mode="lexical", offset=3,
                               document_id="doc-1", current_only=False)
    await mcp_server.kb_document("doc-1", revision_id="rev-2", passage_limit=12, passage_offset=4)
    await mcp_server.kb_observations(query="revision audit", limit=8, offset=2)

    assert calls == [
        ("GET", "/kb/summary", None, {}),
        ("GET", "/kb/search", None, {
            "q": "revision audit", "limit": 7, "mode": "lexical", "offset": 3,
            "document_id": "doc-1", "current_only": False,
        }),
        ("GET", "/kb/documents/doc-1", None, {
            "revision_id": "rev-2", "passage_limit": 12, "passage_offset": 4,
        }),
        ("GET", "/kb/observations", None, {"q": "revision audit", "limit": 8, "offset": 2}),
    ]
