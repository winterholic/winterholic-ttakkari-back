from __future__ import annotations

import pytest
from conftest import PASSWORD
from sqlalchemy import select, text

from ttakkari.db import sessionmaker
from ttakkari.models import AuditLog, RefreshToken

LOGIN = "/api/auth/login"


async def _login(client, password=PASSWORD):
    return await client.post(LOGIN, json={"password": password})


async def test_login_success_returns_token_and_cookie(client):
    r = await _login(client)
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer" and body["access_token"] and body["expires_in"] > 0
    assert "ttk_refresh" in r.cookies
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["authenticated"] is True


async def test_login_wrong_password_401_and_audited(client):
    r = await _login(client, "wrong-password-xx")
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(AuditLog).where(AuditLog.action == "auth.login")))
    assert [x.outcome for x in rows] == ["denied"]


async def test_login_locks_after_five_failures(client):
    for _ in range(5):
        assert (await _login(client, "wrong-password-xx")).status_code == 401
    r = await _login(client, "wrong-password-xx")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0
    # 잠긴 동안은 맞는 비밀번호도 거부된다.
    assert (await _login(client)).status_code == 429


async def test_success_resets_failure_counter(client):
    for _ in range(4):
        await _login(client, "wrong-password-xx")
    assert (await _login(client)).status_code == 200
    for _ in range(4):
        assert (await _login(client, "wrong-password-xx")).status_code == 401


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/workspaces"), ("GET", "/api/sessions"), ("GET", "/api/artifacts"),
    ("GET", "/api/auth/me"), ("GET", "/api/system/info"), ("POST", "/api/system/stop-all"),
    ("POST", "/api/auth/logout-all"),
])
async def test_protected_without_token_is_401(client, method, path):
    r = await client.request(method, path)
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"
    assert r.headers["www-authenticate"] == "Bearer"


async def test_protected_with_garbage_token_is_401(client):
    r = await client.get("/api/workspaces", headers={"Authorization": "Bearer not.a.jwt"})
    assert r.status_code == 401


async def test_health_is_public(client):
    assert (await client.get("/api/health")).json() == {"ok": True}


async def test_refresh_rotates_cookie(client):
    login = await _login(client)
    old = login.cookies["ttk_refresh"]
    r = await client.post("/api/auth/refresh", headers={"Cookie": f"ttk_refresh={old}"})
    assert r.status_code == 200
    new = r.cookies["ttk_refresh"]
    assert new and new != old
    assert r.json()["access_token"]
    async with sessionmaker()() as db:
        rows = list(await db.scalars(select(RefreshToken)))
    assert len(rows) == 2 and len({x.family_id for x in rows}) == 1
    assert sum(x.revoked_at is None for x in rows) == 1


async def test_refresh_without_cookie_401(client):
    client.cookies.clear()
    assert (await client.post("/api/auth/refresh")).status_code == 401


async def test_refresh_reuse_revokes_whole_family(client):
    login = await _login(client)
    first = login.cookies["ttk_refresh"]
    second = (await client.post("/api/auth/refresh", headers={"Cookie": f"ttk_refresh={first}"})).cookies["ttk_refresh"]
    # 이미 교체된 첫 토큰 재사용 = 탈취 신호
    r = await client.post("/api/auth/refresh", headers={"Cookie": f"ttk_refresh={first}"})
    assert r.status_code == 401
    # 정상이던 새 토큰도 같은 계열이라 같이 죽는다.
    r = await client.post("/api/auth/refresh", headers={"Cookie": f"ttk_refresh={second}"})
    assert r.status_code == 401
    async with sessionmaker()() as db:
        assert all(x.revoked_at is not None for x in await db.scalars(select(RefreshToken)))
        reuse = list(await db.scalars(select(AuditLog).where(AuditLog.action == "auth.refresh_reuse")))
    # 폐기된 두 토큰을 각각 한 번씩 썼으므로 reuse 감사 기록이 두 건이다.
    assert len(reuse) == 2


async def test_logout_revokes_family_and_clears_cookie(client):
    login = await _login(client)
    raw = login.cookies["ttk_refresh"]
    r = await client.post("/api/auth/logout", headers={"Cookie": f"ttk_refresh={raw}"})
    assert r.status_code == 204
    assert "ttk_refresh" in r.headers.get("set-cookie", "")
    assert (await client.post("/api/auth/refresh", headers={"Cookie": f"ttk_refresh={raw}"})).status_code == 401


async def test_logout_without_cookie_is_ok(client):
    client.cookies.clear()
    assert (await client.post("/api/auth/logout")).status_code == 204


async def test_logout_all_revokes_every_family(client, auth):
    a = (await _login(client)).cookies["ttk_refresh"]
    b = (await _login(client)).cookies["ttk_refresh"]
    assert (await client.post("/api/auth/logout-all", headers=auth)).status_code == 204
    for raw in (a, b):
        assert (await client.post("/api/auth/refresh", headers={"Cookie": f"ttk_refresh={raw}"})).status_code == 401


async def test_login_503_when_password_not_set(client):
    async with sessionmaker()() as db:
        await db.execute(text("DELETE FROM credentials"))
        await db.commit()
    r = await _login(client)
    assert r.status_code == 503
    assert "set-password" in r.json()["message"]


async def test_login_validation_error_shape(client):
    r = await client.post(LOGIN, json={})
    assert r.status_code == 422
    assert r.json()["code"] == "validation"
