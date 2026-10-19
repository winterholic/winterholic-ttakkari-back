from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from ttakkari.api.auth import COOKIE
from ttakkari.cli import _upsert_password
from ttakkari.db import sessionmaker
from ttakkari.models import RefreshToken
from ttakkari.security.auth import limiter
from ttakkari.security.passwords import hash_password

OLD_PASSWORD = "correct-horse-battery"
NEW_PASSWORD = "brand-new-secure-password-456"


@pytest.mark.asyncio
async def test_password_change_revokes_previous_refresh_sessions(client: AsyncClient):
    # 1. Login with old password -> get refresh cookie
    resp = await client.post("/api/auth/login", json={"password": OLD_PASSWORD})
    assert resp.status_code == 200
    old_cookie = resp.cookies.get(COOKIE)
    assert old_cookie is not None

    # Refresh works with old cookie before password change
    client.cookies.set(COOKIE, old_cookie)
    r_refresh = await client.post("/api/auth/refresh")
    assert r_refresh.status_code == 200
    active_cookie = r_refresh.cookies.get(COOKIE)
    assert active_cookie is not None

    # 2. Change password via _upsert_password
    await _upsert_password(hash_password(NEW_PASSWORD))

    # 3. Previous refresh token MUST be rejected with 401
    client.cookies.set(COOKIE, active_cookie)
    r_after = await client.post("/api/auth/refresh")
    assert r_after.status_code == 401

    # In DB, previous tokens are marked revoked
    async with sessionmaker()() as db:
        tokens = list(await db.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))))
        assert len(tokens) == 0

    # 4. Login with new password works normally
    r_new_login = await client.post("/api/auth/login", json={"password": NEW_PASSWORD})
    assert r_new_login.status_code == 200
    new_cookie = r_new_login.cookies.get(COOKIE)
    assert new_cookie is not None

    # New cookie refreshes successfully (control test)
    client.cookies.set(COOKIE, new_cookie)
    r_new_refresh = await client.post("/api/auth/refresh")
    assert r_new_refresh.status_code == 200


@pytest.mark.asyncio
async def test_limiter_does_not_lock_out_other_ips_globally(client: AsyncClient):
    limiter.reset()

    # Simulate attacker failing many times from evil_ip
    for _ in range(25):
        limiter.fail("198.51.100.99")

    # The evil IP is locked
    assert limiter.check("198.51.100.99") is not None

    # The legitimate user IP (e.g. 192.168.1.10) MUST NOT be locked out (no global NAT lock)
    assert limiter.check("192.168.1.10") is None
