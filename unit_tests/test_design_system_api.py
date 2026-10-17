import json
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from test_design_system import repo  # noqa: F401


@pytest.fixture
def api(repo, monkeypatch):  # noqa: F811
    from ttakkari.api import design_system as api
    from ttakkari.design_system import Browser
    app = FastAPI()
    app.include_router(api.router)
    app.include_router(api.agent_router)
    monkeypatch.setattr(api, "browser", lambda: Browser(repo))
    return app, api


async def test_owner_auth_required_and_bounded_queries(api):
    app, module = api
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        assert (await c.get('/api/design-systems')).status_code == 401
        app.dependency_overrides[module.user_access] = lambda: None
        r = await c.get('/api/design-systems')
        assert r.status_code == 200 and r.json()['items'][0]['id'] == 'ttakkari'
        r = await c.get('/api/design-systems/ttakkari/items', params={'q': '사람', 'kind': 'token'})
        assert r.status_code == 200 and r.json()['total'] == 1
        item = r.json()['items'][0]['id']
        d = await c.get('/api/design-systems/ttakkari/item', params={'item': item})
        assert d.json()['light']['resolved'] == '#123456'
        assert (await c.get('/api/design-systems/ttakkari/items?limit=10000')).status_code == 422
        assert (await c.get('/api/design-systems/ttakkari/item', params={'item': 'file:/etc/passwd'})).status_code == 404


async def test_agent_session_scope_blocks_work(api):
    app, _module = api
    from ttakkari.db import get_db
    from ttakkari.orchestrator.runtime import require_orchestrator
    sid = uuid.uuid4()
    class Db:
        async def get(self, model, key):
            assert key == sid
            return SimpleNamespace(pool="work")
    app.dependency_overrides[require_orchestrator] = lambda: sid
    app.dependency_overrides[get_db] = lambda: Db()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get('/api/agent/design-systems')
        assert r.status_code == 403


async def test_mcp_routes_queries_and_has_read_only_annotations(monkeypatch):
    from ttakkari.orchestrator import mcp_server as m
    from ttakkari.orchestrator.tool_policy import READ_ONLY_TOOLS
    calls = []
    async def call(method, path, body=None, **kwargs):
        calls.append((method, path, kwargs))
        return json.dumps({'revision': 'a' * 40, 'items': []})
    monkeypatch.setattr(m, '_call', call)
    await m.design_system_catalog()
    await m.design_system_search('ttakkari', query='button', kind='component', cursor=2)
    await m.design_system_item('ttakkari', 'token:color.action', offset=0)
    assert [x[1] for x in calls] == ['', '/ttakkari/items', '/ttakkari/item']
    assert calls[1][2]['params']['q'] == 'button'
    assert all(x[2]['prefix'] == '/api/agent/design-systems' for x in calls)
    for name in ('design_system_catalog', 'design_system_search', 'design_system_item'):
        assert name in READ_ONLY_TOOLS
    tools = {t.name: t for t in await m.server.list_tools()}
    assert tools['design_system_item'].annotations.read_only_hint is True
