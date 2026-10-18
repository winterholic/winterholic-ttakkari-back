import contextlib
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ttakkari.api import remote_desktop as api
from ttakkari.db import get_db
from ttakkari.models import AppSetting, Credential
from ttakkari.remote_desktop import PermitStore
from ttakkari.security.auth import limiter, require_user
from ttakkari.security.passwords import hash_password


@pytest.fixture
def world(monkeypatch):
    class Db:
        commits = 0
        async def get(self, model, key):
            if model is Credential:
                return SimpleNamespace(password_hash=hashed)
            assert model is AppSetting
            return None
        def add(self, obj):
            pass
        async def commit(self):
            self.commits += 1
    hashed = hash_password('remote-test-password')
    db = Db()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[require_user] = lambda: 'owner'
    monkeypatch.setattr(api, 'get_settings', lambda: SimpleNamespace(cors_origins=['https://phone']))
    monkeypatch.setattr(api, 'permits', PermitStore())
    monkeypatch.setattr(api, 'active', {})
    monkeypatch.setattr(api, 'pending', 0)
    @contextlib.asynccontextmanager
    async def session():
        yield db
    monkeypatch.setattr(api, 'sessionmaker', lambda: session)
    limiter.reset()
    return app, db


async def test_auth_origin_password_reconnect_and_no_cache(world):
    app, _db = world
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='https://test') as c:
        app.dependency_overrides.pop(require_user)
        assert (await c.get('/api/remote-desktop/devices')).status_code == 401
        app.dependency_overrides[require_user] = lambda: 'owner'
        r = await c.get('/api/remote-desktop/devices')
        assert r.json()['devices'][0]['id'] == 'mac-studio'
        assert r.headers['cache-control'] == 'no-store'
        path = '/api/remote-desktop/devices/mac-studio/connect'
        assert (await c.post(path, json={'password':'remote-test-password'})).status_code == 403
        headers = {'Origin':'https://phone'}
        assert (await c.post(path, headers=headers, json={'password':'wrong'})).status_code == 403
        r = await c.post(path, headers=headers, json={'password':'remote-test-password'})
        assert r.status_code == 200
        grant = r.json()['grant']
        assert r.headers['cache-control'] == 'no-store'
        assert 'ticket=' not in r.json()['path']
        r = await c.post(path, headers=headers, json={'grant':grant})
        assert r.status_code == 200
        assert (await c.post('/api/remote-desktop/disconnect', json={'grant':grant})).status_code == 204
        assert (await c.post(path, headers=headers, json={'grant':grant})).status_code == 403


def test_ws_origin_invalid_and_replayed_ticket_rejected_without_dial(world, monkeypatch):
    app, _ = world
    async def no_dial(*args, **kwargs):
        pytest.fail('unauthorized WebSocket must not dial the Mac')
    monkeypatch.setattr(api.asyncio, 'open_connection', no_dial)
    with TestClient(app) as c:
        with pytest.raises(WebSocketDisconnect), c.websocket_connect('/api/remote-desktop/socket', headers={'Origin':'https://evil'}):
            pass
        with c.websocket_connect('/api/remote-desktop/socket', headers={'Origin':'https://phone'}) as ws:
            ws.send_json({'ticket':'invalid', 'grant':'invalid'})
            with pytest.raises(WebSocketDisconnect):
                ws.receive_text()
        assert api.pending == 0


def test_closed_mac_reports_setup_error_and_consumes_ticket(world, monkeypatch):
    app, _ = world
    async def refused(*args, **kwargs):
        raise ConnectionRefusedError()
    monkeypatch.setattr(api.asyncio, 'open_connection', refused)
    grant = api.permits.authorize('mac-studio')
    ticket = api.permits.ticket(grant, 'mac-studio', 'https://phone')
    with TestClient(app) as c:
        for expected in [1013, 1008]:
            with c.websocket_connect('/api/remote-desktop/socket', headers={'Origin':'https://phone'}) as ws:
                ws.send_json({'ticket':ticket, 'grant':grant})
                with pytest.raises(WebSocketDisconnect) as exc:
                    ws.receive_text()
                assert exc.value.code == expected
    assert api.pending == 0


def test_binary_ws_bridge_and_explicit_disconnect(world, monkeypatch):
    app, _ = world
    import asyncio

    from ttakkari.remote_desktop import relay as real_relay
    async def dial(*args, **kwargs):
        reader = asyncio.StreamReader()
        reader.feed_data(b'RFB 003.008\n')
        class Writer:
            def write(self, data):
                assert data == b'hello'
                reader.feed_data(b'echo')
            async def drain(self):
                pass
            def close(self):
                reader.feed_eof()
            async def wait_closed(self):
                pass
        return reader, Writer()
    monkeypatch.setattr(api.asyncio, 'open_connection', dial)
    monkeypatch.setattr(api, 'relay', real_relay)
    grant = api.permits.authorize('mac-studio')
    ticket = api.permits.ticket(grant, 'mac-studio', 'https://phone')
    with TestClient(app) as c, c.websocket_connect('/api/remote-desktop/socket', headers={'Origin':'https://phone'}) as ws:
        ws.send_json({'ticket':ticket, 'grant':grant})
        assert ws.receive_text() == 'ready'
        assert ws.receive_bytes() == b'RFB 003.008\n'
        ws.send_bytes(b'hello')
        assert ws.receive_bytes() == b'echo'
        assert c.post('/api/remote-desktop/disconnect', json={'grant':grant}).status_code == 204
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()
    assert api.active == {} and api.pending == 0


@pytest.mark.parametrize('frame', ['[]', '{"ticket":[],"grant":[1]}', '{', 'x' * 513, b'binary'])
def test_malformed_frames_cannot_leak_connection_slots(world, frame):
    app, _ = world
    with TestClient(app) as c, c.websocket_connect('/api/remote-desktop/socket', headers={'Origin':'https://phone'}) as ws:
        if isinstance(frame, bytes):
            ws.send_bytes(frame)
        else:
            ws.send_text(frame)
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()
    assert api.pending == 0 and api.active == {}
