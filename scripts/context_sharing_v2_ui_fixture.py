"""UI-oriented real Private/Gateway acceptance test using one isolated prefixed DB.

Run with TTAKKARI_TEST_DB pointing at test_cg_v2_ui_verify_20261119 and
PYTHONPATH=src:tests pytest scripts/context_sharing_v2_ui_fixture.py -q.
The core alias below keeps the real Gateway ASGI apps in that same test DB;
it never creates/opens a second database. All project documents are synthetic.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx
import pytest
from fastapi.responses import JSONResponse

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
sys.path.insert(0, str(TESTS))
pytest_plugins = ["conftest"]

import test_context_sharing_real_gateway as real_gateway_tests
from context_sharing_support import make_project
from sqlalchemy import text
from test_context_sharing_real_gateway import (
    core,  # noqa: F401
    real_gateway,  # noqa: F401
)

from ttakkari.context_sharing.gateway import HttpGatewayTransport


class _SamePrefixedDatabase:
    """Alias the Gateway fixture's requested DB name to the one approved DB."""

    def __init__(self, url: str):
        self.url = url

    def create_database(self, name: str) -> str:
        assert name.startswith("gateway_"), name
        return self.url


@pytest.fixture(autouse=True)
def _gateway_uses_only_the_prefixed_test_db():
    conftest = sys.modules.get("conftest")
    assert conftest is not None, "tests/conftest.py must be loaded by pytest"
    db_url = conftest.TEST_DB
    assert "test_cg_v2_ui_verify_20261119" in db_url
    real_gateway_tests.CLUSTER = _SamePrefixedDatabase(db_url)
    yield
    real_gateway_tests.CLUSTER = None


@pytest.mark.asyncio
async def test_context_sharing_management_journey_against_real_private_and_gateway(
    client, auth, real_gateway  # noqa: F811
):
    """Issue, list, repeat REST use, then prove parent key/session revocation."""
    from test_context_sharing_real_gateway import BASE, core_rows, publish_real

    project = await make_project(
        "UI Synthetic Acceptance",
        {"README.md": "# UI synthetic fixture\nNo user or production content.\n"},
    )
    draft = (await client.post(f"{BASE}/drafts", headers=auth, json={
        "entity_id": project["entity_id"], "document_ids": list(project["docs"].values()),
    })).json()
    draft = (await client.patch(f"{BASE}/drafts/{draft['id']}", headers=auth, json={
        "documents": [{"id": d["id"], "owner_verified": True} for d in draft["documents"]],
    })).json()
    published = await publish_real(client, auth, draft)
    assert published.status_code == 200, published.text

    issued = await client.post(f"{BASE}/keys", headers=auth, json={
        "project_slug": draft["slug"], "credential_type": "reusable_api_v2",
        "scopes": ["read"], "snapshot_policy": "pinned",
    })
    assert issued.status_code == 201, issued.text
    receipt = issued.json()
    raw_key = receipt["key"]
    assert receipt["ttl_seconds"] == 3 * 24 * 60 * 60
    assert receipt["connect"]["project_id"]
    project_id = receipt["connect"]["project_id"]
    assert (await core_rows(real_gateway["url"],
        f"SELECT id::text FROM gateway_projects WHERE slug = '{draft['slug']}'")) == [(project_id,)]

    listed = (await client.get(f"{BASE}/keys", headers=auth, params={"project_slug": draft["slug"]})).json()["keys"]
    meta = next(k for k in listed if k["id"] == receipt["id"])
    assert meta["credential_type"] == "reusable_api_v2"
    assert meta["snapshot_policy"] == "pinned" and meta["scopes"] == ["read"]
    assert meta["usage_status"] in {"available", "unavailable"}
    assert raw_key not in json.dumps(listed)

    public = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_gateway["public"]), base_url="http://public"
    )
    async with public:
        direct_results = [await public.get(
            f"/api/v1/projects/{project_id}/context",
            headers={"Authorization": f"Bearer {raw_key}"},
        ) for _ in range(3)]
        assert [r.status_code for r in direct_results] == [200, 200, 200]
        assert all("UI synthetic fixture" in r.text for r in direct_results)
        exchanged = await public.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert exchanged.status_code == 200, exchanged.text
        old_session = exchanged.json()["access_token"]

        revoked = await client.post(f"{BASE}/keys/{receipt['id']}/revoke", headers=auth)
        assert revoked.status_code == 200 and revoked.json()["status"] in {"revoked", "revoking"}
        for credential in (raw_key, old_session):
            denied = await public.get(
                f"/api/v1/projects/{project_id}/context",
                headers={"Authorization": f"Bearer {credential}"},
            )
            assert denied.status_code == 401, denied.text

    final_meta = (await client.get(
        f"{BASE}/keys", headers=auth, params={"project_slug": draft["slug"]}
    )).json()["keys"]
    final = next(k for k in final_meta if k["id"] == receipt["id"])
    assert final["status"] in {"revoked", "revoking"}

    link_response = await client.post(f"{BASE}/links", headers=auth, json={
        "project_slug": draft["slug"], "label": "synthetic UI link",
    })
    assert link_response.status_code == 201, link_response.text
    link = link_response.json()
    assert link["ttl_seconds"] == 3 * 24 * 60 * 60
    raw_link = link["url"].split("#token=", 1)[1]
    link_list = (await client.get(f"{BASE}/links", headers=auth,
                                  params={"project_slug": draft["slug"]})).json()["links"]
    link_meta = next(row for row in link_list if row["id"] == link["id"])
    assert link_meta["status"] == "active" and link_meta["ttl_seconds"] == 3 * 24 * 60 * 60
    assert "url" not in link_meta and raw_link not in json.dumps(link_list)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_gateway["public"]), base_url="https://public"
    ) as public_link:
        accepted = await public_link.post(
            "/api/v1/share/exchange", json={"token": raw_link},
            headers={"origin": "https://public", "content-type": "application/json"},
        )
        assert accepted.status_code == 200, accepted.text
        context = await public_link.get("/api/v1/share/context")
        assert context.status_code == 200 and "UI synthetic fixture" in context.text
        revoked_link = await client.post(f"{BASE}/links/{link['id']}/revoke", headers=auth)
        assert revoked_link.status_code == 200
        blocked = await public_link.get("/api/v1/share/context")
        assert blocked.status_code == 401, blocked.text


async def _serve_browser_fixture() -> None:
    """Serve the actual Private app for a local browser; auth/shell endpoints are synthetic."""
    import httpx
    import uvicorn
    from fastapi.middleware.cors import CORSMiddleware
    from sqlalchemy.ext.asyncio import create_async_engine
    core_src = REPO / "context_gateway" / "src"
    sys.path.insert(0, str(core_src))
    from conftest import rebuild_test_schema
    from context_gateway.apps import create_control_app, create_public_app
    from context_gateway.config import Settings
    from test_context_sharing_real_gateway import TOKEN, publish_real

    from ttakkari import models
    from ttakkari.api import context_sharing as sharing_api
    from ttakkari.context_sharing.gateway import GatewayControl
    from ttakkari.main import app
    from ttakkari.security.auth import require_user
    conftest = sys.modules["conftest"]
    database_url = conftest.TEST_DB
    assert "test_cg_v2_ui_verify_20261119" in database_url
    engine = create_async_engine(database_url)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(rebuild_test_schema, models.Base.metadata)
    await engine.dispose()

    gateway_settings = Settings(
        database_url=database_url,
        snapshot_dir=Path(tempfile.mkdtemp(prefix="cg-v2-ui-snapshots-", dir="/private/tmp")),
        control_token=TOKEN,
    )
    control = create_control_app(gateway_settings)
    create_public_app(gateway_settings)
    transport = HttpGatewayTransport(
        "http://127.0.0.1:8791", TOKEN, http_transport=httpx.ASGITransport(app=control)
    )
    app.dependency_overrides[sharing_api.get_gateway] = lambda: GatewayControl(
        transport, public_url="https://share.example.test"
    )
    app.dependency_overrides[require_user] = lambda: "owner"
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def synthetic_auth_and_shell(request, call_next):
        from fastapi import Response

        origin = request.headers.get("origin")
        cors_headers = {}
        if origin == "http://localhost:5173":
            cors_headers = {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Headers": "authorization,content-type",
                "Access-Control-Allow-Methods": "GET,POST,PATCH,PUT,DELETE,OPTIONS",
                "Vary": "Origin",
            }
        if request.method == "OPTIONS" and cors_headers:
            return Response(status_code=204, headers=cors_headers)
        if request.url.path == "/api/auth/refresh":
            response = JSONResponse({"access_token": "synthetic-browser-session", "expires_in": 900,
                                     "token_type": "bearer"})
        elif request.url.path == "/api/auth/me":
            response = JSONResponse({"authenticated": True, "password_set": True})
        elif request.url.path == "/api/health":
            response = JSONResponse({"ok": True})
        else:
            response = await call_next(request)
        response.headers.update(cors_headers)
        return response

    async with control.router.lifespan_context(control):
        project = await make_project(
            "UI Browser Synthetic",
            {"README.md": "# Browser synthetic fixture\nNo user or production content.\n"},
        )
        auth = {"Authorization": "Bearer synthetic-browser-session"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://private"
        ) as client:
            draft_response = await client.post("/api/context-sharing/drafts", headers=auth, json={
                "entity_id": project["entity_id"], "document_ids": list(project["docs"].values()),
            })
            assert draft_response.status_code == 201, draft_response.text
            draft = draft_response.json()
            patch = await client.patch(f"/api/context-sharing/drafts/{draft['id']}", headers=auth, json={
                "documents": [{"id": d["id"], "owner_verified": True} for d in draft["documents"]],
            })
            assert patch.status_code == 200, patch.text
            draft = patch.json()
            published = await publish_real(client, auth, draft)
            assert published.status_code == 200, published.text
            print(f"SYNTHETIC_CONTEXT_SHARING_ENTITY_ID={project['entity_id']}", flush=True)
            server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8791, log_level="warning"))
            await server.serve()


if __name__ == "__main__" and "--serve" in sys.argv:
    asyncio.run(_serve_browser_fixture())
