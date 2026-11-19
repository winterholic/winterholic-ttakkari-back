"""Exercise synthetic v2 credentials over the running local Gateway HTTP boundary."""

from __future__ import annotations

import asyncio
import getpass
import uuid

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

ORIGIN = "http://127.0.0.1:8747"
DB_URL = f"postgresql+asyncpg://{getpass.getuser()}@127.0.0.1:5432/test_cg_v2_public_verify_20261119"
CONTROL_TOKEN = "synthetic-control-only-context-sharing-v2-20261119"
ADMIN = {"Authorization": f"Bearer {CONTROL_TOKEN}"}


async def issue(client: httpx.AsyncClient, slug: str, scopes: list[str]) -> tuple[str, str, str]:
    published = await client.post(
        "/control/v1/snapshots",
        headers=ADMIN,
        json={
            "project_slug": slug,
            "project_name": f"합성 HTTP 검수 {slug}",
            "project_description": "실제 local Gateway HTTP 경계 검증용 합성 설명",
            "source_refs": [f"fixture://{slug}/source"],
            "source_revision": "independent-http-revision",
            "generated_at": "2026-11-19T01:00:00Z",
            "verified_at": "2026-11-19T01:01:00Z",
            "documents": [
                {
                    "id": "overview",
                    "title": "검증 문서",
                    "content": "Synthetic document body",
                    "source_refs": [f"fixture://{slug}/document"],
                    "source_revision": "independent-http-revision",
                    "generated_at": "2026-11-19T01:00:00Z",
                    "verified_at": "2026-11-19T01:01:00Z",
                }
            ],
        },
    )
    assert published.status_code == 201
    project_id, snapshot_id = published.json()["project_id"], published.json()["id"]
    issued = await client.post(
        "/control/v2/credentials",
        headers=ADMIN,
        json={
            "credential_type": "share_link_v2",
            "label": "synthetic HTTP check",
            "ttl_days": 3,
            "projects": {
                project_id: {
                    "scopes": scopes,
                    "snapshot_policy": "pinned",
                    "snapshot_id": snapshot_id,
                }
            },
        },
    )
    assert issued.status_code == 201
    receipt = issued.json()
    return receipt["key"], receipt["key_id"], project_id


def cookie_header(response: httpx.Response) -> tuple[str, str]:
    value = response.headers.get("set-cookie", "")
    lowered = value.lower()
    assert "; secure" in lowered and "; httponly" in lowered
    assert "samesite=strict" in lowered and "path=/api/v1/share" in lowered
    first = value.split(";", 1)[0]
    return first.split("=", 1)[0], first.split("=", 1)[1]


async def main() -> None:
    async with httpx.AsyncClient(base_url=ORIGIN, timeout=10) as client:
        await client.get("/healthz")
        primary, _, primary_project = await issue(client, f"http-primary-{uuid.uuid4().hex[:8]}", ["read", "export"])
        exchange = await client.post(
            "/api/v1/share/exchange",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json={"token": primary},
        )
        assert exchange.status_code == 200
        cookie_name, cookie_value = cookie_header(exchange)
        cookie = {"Cookie": f"{cookie_name}={cookie_value}"}
        assert primary not in exchange.text and cookie_value not in exchange.text
        context = await client.get("/api/v1/share/context", headers=cookie)
        assert context.status_code == 200
        assert context.json()["project"]["id"] == primary_project
        assert context.headers.get_list("cache-control") and all(
            value == "no-store" for value in context.headers.get_list("cache-control")
        )
        assert context.headers.get_list("referrer-policy") and all(
            value == "no-referrer" for value in context.headers.get_list("referrer-policy")
        )
        assert context.headers.get_list("content-security-policy")
        assert primary not in context.text and cookie_value not in context.text
        export = await client.get("/api/v1/share/export?format=markdown", headers=cookie)
        assert export.status_code == 200 and "Synthetic document body" in export.text
        repeat = await client.post(
            "/api/v1/share/exchange",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json={"token": primary},
        )
        assert repeat.status_code == 200
        cross_site = await client.post(
            "/api/v1/share/exchange",
            headers={"Origin": "https://attacker.invalid", "Content-Type": "application/json"},
            json={"token": primary},
        )
        assert cross_site.status_code == 403 and primary not in cross_site.text

        readonly, _, _ = await issue(client, f"http-readonly-{uuid.uuid4().hex[:8]}", ["read"])
        readonly_exchange = await client.post(
            "/api/v1/share/exchange",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json={"token": readonly},
        )
        readonly_cookie = cookie_header(readonly_exchange)
        denied_export = await client.get(
            "/api/v1/share/export?format=markdown",
            headers={"Cookie": f"{readonly_cookie[0]}={readonly_cookie[1]}"},
        )
        assert denied_export.status_code == 404, (
            f"read-only export returned HTTP {denied_export.status_code}: {denied_export.text}"
        )
        assert readonly not in denied_export.text and readonly_cookie[1] not in denied_export.text

        # Pause blocks a live share cookie; resume restores it without issuing a replacement key.
        pause = await client.patch("/control/v1/site", headers=ADMIN, json={"enabled": False})
        assert pause.status_code == 200
        paused_context = await client.get("/api/v1/share/context", headers=cookie)
        assert paused_context.status_code == 503
        resume = await client.patch("/control/v1/site", headers=ADMIN, json={"enabled": True})
        assert resume.status_code == 200
        resumed_context = await client.get("/api/v1/share/context", headers=cookie)
        assert resumed_context.status_code == 200

        logged_out = await client.post("/api/v1/share/logout", headers=cookie)
        assert logged_out.status_code == 200
        assert "max-age=0" in logged_out.headers.get("set-cookie", "").lower()
        assert (await client.get("/api/v1/share/context", headers=cookie)).status_code == 401

        # Expiry is accelerated only on this script's newly issued synthetic fixture row.
        expired, expired_id, _ = await issue(client, f"http-expired-{uuid.uuid4().hex[:8]}", ["read"])
        engine = create_async_engine(DB_URL)
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE gateway_api_keys SET expires_at = now() - interval '1 second' WHERE id = :id"),
                {"id": expired_id},
            )
        await engine.dispose()
        expired_response = await client.post(
            "/api/v1/share/exchange",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json={"token": expired},
        )
        assert expired_response.status_code == 401 and expired not in expired_response.text

        revoked_link, revoked_link_id, _ = await issue(client, f"http-revoked-{uuid.uuid4().hex[:8]}", ["read"])
        revoked = await client.post(f"/control/v1/keys/{revoked_link_id}/revoke", headers=ADMIN)
        assert revoked.status_code == 200
        revoked_exchange = await client.post(
            "/api/v1/share/exchange",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json={"token": revoked_link},
        )
        assert revoked_exchange.status_code == 401 and revoked_link not in revoked_exchange.text

        project_link, _, project_id = await issue(client, f"http-project-revoked-{uuid.uuid4().hex[:8]}", ["read"])
        project_revoked = await client.post(f"/control/v1/projects/{project_id}/revoke", headers=ADMIN)
        assert project_revoked.status_code == 200
        project_exchange = await client.post(
            "/api/v1/share/exchange",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json={"token": project_link},
        )
        assert project_exchange.status_code == 401 and project_link not in project_exchange.text

        print(
            "HTTP PASS: exchange=200 repeat=200 cookie=Secure,HttpOnly,SameSite=Strict,Path "
            "context=200 export=200/read-only=404 cross-site=403 pause=503/resume=200 "
            "logout=200/session=401 expired=401 link-revoked=401 project-revoked=401"
        )


if __name__ == "__main__":
    asyncio.run(main())
