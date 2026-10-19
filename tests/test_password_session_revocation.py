from __future__ import annotations

import asyncio

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
    assert any(
        header.startswith(f"{COOKIE}=") and "Max-Age=0" in header
        for header in r_after.headers.get_list("set-cookie")
    )

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


def test_limiter_bounds_memory_for_many_distinct_source_ips():
    limiter.reset()
    for index in range(5000):
        limiter.fail(f"198.51.{index // 256}.{index % 256}")

    assert len(limiter._fails) <= limiter.MAX_TRACKED_IPS
    assert not hasattr(limiter, "_global")


@pytest.mark.asyncio
async def test_concurrent_refresh_of_same_cookie_has_only_one_winner(client: AsyncClient):
    login = await client.post("/api/auth/login", json={"password": OLD_PASSWORD})
    cookie = login.cookies.get(COOKIE)
    assert login.status_code == 200
    assert cookie is not None

    async def refresh_once():
        return await client.post("/api/auth/refresh", headers={"Cookie": f"{COOKIE}={cookie}"})

    first, second = await asyncio.gather(refresh_once(), refresh_once())
    assert sorted([first.status_code, second.status_code]) == [200, 401]


@pytest.mark.asyncio
async def test_password_change_racing_refresh_leaves_no_live_refresh_token(client: AsyncClient):
    login = await client.post("/api/auth/login", json={"password": OLD_PASSWORD})
    cookie = login.cookies.get(COOKIE)
    assert login.status_code == 200
    assert cookie is not None

    async def refresh_once():
        return await client.post("/api/auth/refresh", headers={"Cookie": f"{COOKIE}={cookie}"})

    refresh_response, _ = await asyncio.gather(
        refresh_once(), _upsert_password(hash_password(NEW_PASSWORD))
    )
    assert refresh_response.status_code in {200, 401}
    async with sessionmaker()() as db:
        tokens = list(await db.scalars(select(RefreshToken).where(RefreshToken.revoked_at.is_(None))))
        assert tokens == []


@pytest.mark.asyncio
async def test_password_change_keeps_existing_access_bearer_until_its_expiry(client: AsyncClient):
    login = await client.post("/api/auth/login", json={"password": OLD_PASSWORD})
    assert login.status_code == 200
    access_token = login.json()["access_token"]

    await _upsert_password(hash_password(NEW_PASSWORD))
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {access_token}"})

    # Access JWT is stateless and expires independently (15 minutes by default).
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_distributed_ip_concurrent_login_bounds_verification_calls_concurrency_and_recovers(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, new_session
):
    from ttakkari.api import auth as auth_api
    from ttakkari.config import get_settings
    from ttakkari.orchestrator.runtime import issue_token
    from ttakkari.security.passwords import verify_password as real_verify_password

    limiter.reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_proxy_ip_header", "CF-Connecting-IP")

    # Prepare valid refresh cookie and agent session token before the attack (control group)
    pre_login = await client.post(
        "/api/auth/login",
        json={"password": OLD_PASSWORD},
        headers={"CF-Connecting-IP": "192.0.2.1"},
    )
    assert pre_login.status_code == 200
    valid_refresh_cookie = pre_login.cookies.get(COOKIE)
    valid_access_token = pre_login.json()["access_token"]
    _, session_info = await new_session()
    agent_token = issue_token(session_info["id"])

    limiter.reset()

    verify_calls = 0
    in_progress = 0
    max_concurrency = 0

    def instrumented_verify(password_hash: str, password: str) -> bool:
        nonlocal verify_calls, in_progress, max_concurrency
        import time as _time

        verify_calls += 1
        in_progress += 1
        max_concurrency = max(max_concurrency, in_progress)
        try:
            # Small deterministic work window so concurrent arrivals overlap predictably
            _time.sleep(0.03)
            return real_verify_password(password_hash, password)
        finally:
            in_progress -= 1

    monkeypatch.setattr(auth_api, "verify_password", instrumented_verify)

    # 1. Concurrent wave from 24 distinct source IPs (each only 1 attempt, bypassing per-IP 5-fail limit)
    async def attempt_wrong(idx: int):
        return await client.post(
            "/api/auth/login",
            json={"password": f"wrong-distributed-{idx}"},
            headers={"CF-Connecting-IP": f"198.51.100.{idx + 1}"},
        )

    wave1 = await asyncio.gather(*(attempt_wrong(i) for i in range(24)))
    statuses1 = [r.status_code for r in wave1]

    # Excess concurrent requests must be shed with 429 before invoking verify_password
    assert 429 in statuses1
    assert set(statuses1) <= {401, 429}
    assert max_concurrency == 1
    assert verify_calls <= settings.login_max_inflight

    # 2. Sequential wave from another 16 distinct IPs hits global verification token budget
    wave2 = []
    for i in range(24, 40):
        wave2.append(await attempt_wrong(i))
    statuses2 = [r.status_code for r in wave2]
    assert 429 in statuses2
    assert verify_calls <= settings.login_verify_burst
    for r in (*wave1, *wave2):
        if r.status_code == 429:
            assert int(r.headers["retry-after"]) >= 1

    # 3. Control group: refresh, bearer API, and agent API remain unaffected during login saturation
    r_refresh = await client.post(
        "/api/auth/refresh",
        headers={"Cookie": f"{COOKIE}={valid_refresh_cookie}"},
    )
    assert r_refresh.status_code == 200
    r_me = await client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {valid_access_token}"},
    )
    assert r_me.status_code == 200
    r_agent = await client.get(
        "/api/agent/sessions",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r_agent.status_code == 200

    # 4. Recovery: after brief token refill window (no multi-minute global lockout), legitimate login succeeds
    await asyncio.sleep(1.0 / settings.login_verify_rate_per_second + 0.05)
    calls_before_recovery = verify_calls
    recovered = await client.post(
        "/api/auth/login",
        json={"password": OLD_PASSWORD},
        headers={"CF-Connecting-IP": "192.0.2.250"},
    )
    assert recovered.status_code == 200
    assert verify_calls == calls_before_recovery + 1


@pytest.mark.asyncio
async def test_normal_valid_logins_do_not_exhaust_verification_budget(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    from ttakkari.config import get_settings

    limiter.reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "trust_proxy_ip_header", "CF-Connecting-IP")

    # Repeated valid logins exceeding login_verify_burst must all succeed without artificial 429
    for i in range(settings.login_verify_burst + 4):
        resp = await client.post(
            "/api/auth/login",
            json={"password": OLD_PASSWORD},
            headers={"CF-Connecting-IP": f"192.0.2.{i + 10}"},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_trusted_proxy_ip_boundary_and_spoof_resistance(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    import httpx

    from ttakkari.api import auth as auth_api
    from ttakkari.config import get_settings
    from ttakkari.main import app
    from ttakkari.security.passwords import verify_password as real_verify_password

    limiter.reset()
    settings = get_settings()

    verify_calls = 0

    def counting_verify(password_hash: str, password: str) -> bool:
        nonlocal verify_calls
        verify_calls += 1
        return real_verify_password(password_hash, password)

    monkeypatch.setattr(auth_api, "verify_password", counting_verify)

    # Case 1: trust_proxy_ip_header is None -> spoofed forwarding headers are ignored
    monkeypatch.setattr(settings, "trust_proxy_ip_header", None)
    for i in range(5):
        r = await client.post(
            "/api/auth/login",
            json={"password": "wrong-password-spoof"},
            headers={
                "CF-Connecting-IP": f"198.51.100.{i + 1}",
                "X-Forwarded-For": f"203.0.113.{i + 1}",
            },
        )
        assert r.status_code == 401
    assert verify_calls == 5

    # 6th attempt from same peer with a fresh spoofed header must still be locked (429, no verify call)
    r6 = await client.post(
        "/api/auth/login",
        json={"password": "wrong-password-spoof"},
        headers={"CF-Connecting-IP": "198.51.100.250", "X-Forwarded-For": "203.0.113.250"},
    )
    assert r6.status_code == 429
    assert verify_calls == 5

    # Case 2: trust_proxy_ip_header is set, but peer is NOT a trusted loopback proxy -> spoofed header ignored
    limiter.reset()
    verify_calls = 0
    monkeypatch.setattr(settings, "trust_proxy_ip_header", "CF-Connecting-IP")

    untrusted_transport = httpx.ASGITransport(app=app, client=("203.0.113.77", 54321))
    async with httpx.AsyncClient(transport=untrusted_transport, base_url="http://test") as direct_client:
        for i in range(5):
            r = await direct_client.post(
                "/api/auth/login",
                json={"password": "wrong-password-direct"},
                headers={"CF-Connecting-IP": f"198.51.100.{i + 50}"},
            )
            assert r.status_code == 401
        assert verify_calls == 5

        # 6th attempt with another spoofed CF-Connecting-IP from untrusted peer is locked by socket IP
        r_direct_6 = await direct_client.post(
            "/api/auth/login",
            json={"password": "wrong-password-direct"},
            headers={"CF-Connecting-IP": "198.51.100.199"},
        )
        assert r_direct_6.status_code == 429
        assert verify_calls == 5

    # Meanwhile a legitimate user via trusted loopback proxy with a different CF-Connecting-IP succeeds
    r_legit = await client.post(
        "/api/auth/login",
        json={"password": OLD_PASSWORD},
        headers={"CF-Connecting-IP": "198.51.100.200"},
    )
    assert r_legit.status_code == 200
    assert verify_calls == 6
