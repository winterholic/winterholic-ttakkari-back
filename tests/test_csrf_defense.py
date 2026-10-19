from __future__ import annotations

import pytest
from httpx import AsyncClient

from ttakkari.api.auth import COOKIE

PASSWORD = "correct-horse-battery"
VALID_ORIGIN = "http://localhost:5173"
EVIL_ORIGIN = "https://malicious-attacker.com"


@pytest.mark.asyncio
async def test_refresh_and_logout_csrf_defense(client: AsyncClient):
    # Obtain valid session cookie first
    resp = await client.post("/api/auth/login", json={"password": PASSWORD})
    assert resp.status_code == 200
    cookie = resp.cookies.get(COOKIE)
    assert cookie is not None

    # 1. Evil Origin attempting refresh -> 403 Forbidden
    client.cookies.set(COOKIE, cookie)
    r_evil_origin = await client.post(
        "/api/auth/refresh", headers={"Origin": EVIL_ORIGIN, "Sec-Fetch-Site": "cross-site"}
    )
    assert r_evil_origin.status_code == 403
    assert "허용되지 않은 요청 출처" in r_evil_origin.json()["message"]

    # 2. Cross-site Sec-Fetch-Site attempting refresh -> 403 Forbidden
    r_cross_site = await client.post("/api/auth/refresh", headers={"Sec-Fetch-Site": "cross-site"})
    assert r_cross_site.status_code == 403
    assert "교차 사이트 요청" in r_cross_site.json()["message"]

    # 3. Evil Origin attempting logout -> 403 Forbidden
    r_evil_logout = await client.post(
        "/api/auth/logout", headers={"Origin": EVIL_ORIGIN, "Sec-Fetch-Site": "cross-site"}
    )
    assert r_evil_logout.status_code == 403

    # 4. Valid Origin (whitelisted front domain) -> 200 OK (Control test)
    r_valid_origin = await client.post("/api/auth/refresh", headers={"Origin": VALID_ORIGIN})
    assert r_valid_origin.status_code == 200
    new_cookie = r_valid_origin.cookies.get(COOKIE)
    assert new_cookie is not None

    # 5. Non-browser API client without Origin / Sec-Fetch-Site -> 200 OK (Control test)
    client.cookies.set(COOKIE, new_cookie)
    r_api_client = await client.post("/api/auth/refresh")
    assert r_api_client.status_code == 200


@pytest.mark.asyncio
async def test_allowlisted_origin_with_cross_site_fetch_metadata_is_supported(client: AsyncClient):
    resp = await client.post("/api/auth/login", json={"password": PASSWORD})
    cookie = resp.cookies.get(COOKIE)
    assert resp.status_code == 200
    assert cookie is not None

    # Supported deployments may place the allowlisted UI on a distinct site from the API.
    client.cookies.set(COOKIE, cookie)
    refreshed = await client.post(
        "/api/auth/refresh",
        headers={"Origin": VALID_ORIGIN, "Sec-Fetch-Site": "cross-site"},
    )
    assert refreshed.status_code == 200
