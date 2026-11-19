from __future__ import annotations

import httpx
import pytest

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.config import Settings


@pytest.mark.asyncio
async def test_share_link_exchanges_to_cookie_and_reads_only_granted_context(
    postgres_url, tmp_path, caplog
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    control = create_control_app(settings)
    public = create_public_app(settings)
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="https://test"
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="https://test"
        ) as browser,
    ):
        headers = {"Authorization": "Bearer synthetic-control"}
        published = await admin.post(
            "/control/v1/snapshots",
            headers=headers,
            json={
                "project_slug": "share-fixture",
                "project_name": "Share Fixture",
                "source_refs": ["fixture://share"],
                "source_revision": "share-rev",
                "generated_at": "2026-11-19T00:00:00Z",
                "documents": [{"id": "doc", "title": "Doc", "content": "shared body"}],
            },
        )
        assert published.status_code == 201, published.text
        project_id = published.json()["project_id"]
        issued = await admin.post(
            "/control/v2/credentials",
            headers=headers,
            json={
                "credential_type": "share_link_v2",
                "label": "browser fixture",
                "projects": {
                    project_id: {
                        "scopes": ["read"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": published.json()["id"],
                    }
                },
            },
        )
        assert issued.status_code == 201, issued.text
        token = issued.json()["key"]
        assert "url" not in issued.json()
        malformed = await browser.post(
            "/api/v1/share/exchange",
            json={"token": [token]},
            headers={"Origin": "https://test", "Content-Type": "application/json"},
        )
        assert malformed.status_code == 401 and token not in malformed.text
        ordinary_exchange = await browser.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {token}"}
        )
        assert ordinary_exchange.status_code == 401 and token not in ordinary_exchange.text
        rejected_origin = await browser.post(
            "/api/v1/share/exchange",
            json={"token": token},
            headers={"Origin": "https://attacker.invalid", "Content-Type": "application/json"},
        )
        assert rejected_origin.status_code == 403 and token not in rejected_origin.text
        exchange = await browser.post(
            "/api/v1/share/exchange",
            json={"token": token},
            headers={"Origin": "https://test", "Content-Type": "application/json"},
        )
        assert exchange.status_code == 200, exchange.text
        assert token not in exchange.text
        assert token not in str(exchange.url)
        assert exchange.headers["set-cookie"].lower().find("httponly") >= 0
        assert exchange.headers["set-cookie"].lower().find("secure") >= 0
        repeat = await browser.post(
            "/api/v1/share/exchange",
            json={"token": token},
            headers={"Origin": "https://test", "Content-Type": "application/json"},
        )
        assert repeat.status_code == 200 and token not in repeat.text
        context = await browser.get("/api/v1/share/context")
        assert context.status_code == 200, context.text
        assert context.json()["documents"][0]["content"] == "shared body"
        assert context.headers["cache-control"] == "no-store"
        assert context.headers["referrer-policy"] == "no-referrer"
        raw_bearer = await browser.get(
            "/api/v1/projects", headers={"Authorization": f"Bearer {token}"}
        )
        assert raw_bearer.status_code == 401 and token not in raw_bearer.text
        assert (await browser.get("/api/v1/share/export?format=markdown")).status_code == 404
        assert (await browser.get("/api/v1/projects")).status_code == 401
        paused = await admin.patch("/control/v1/site", headers=headers, json={"enabled": False})
        assert paused.status_code == 200
        assert (await browser.get("/api/v1/share/context")).status_code == 503
        resumed = await admin.patch("/control/v1/site", headers=headers, json={"enabled": True})
        assert resumed.status_code == 200
        revoked = await admin.post(
            f"/control/v1/keys/{issued.json()['key_id']}/revoke", headers=headers
        )
        assert revoked.status_code == 200
        assert (await browser.get("/api/v1/share/context")).status_code == 401
        assert (await browser.post("/api/v1/share/logout")).status_code == 200
        assert (await browser.get("/api/v1/share/context")).status_code == 401
        assert token not in caplog.text
