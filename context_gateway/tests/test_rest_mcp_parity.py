from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from context_gateway.apps import _initialize_schema, create_control_app, create_public_app
from context_gateway.auth import hash_credential
from context_gateway.config import Settings
from context_gateway.models import APIKey, Session, SiteControlAudit


@pytest.fixture
def settings(postgres_url, tmp_path) -> Settings:
    return Settings(postgres_url, tmp_path / "snapshots", "synthetic-parity-token", 200)


@pytest.mark.asyncio
async def test_rest_and_mcp_sdk_return_identical_context_and_search_data(settings) -> None:
    import httpx2

    control = create_control_app(settings)
    public = create_public_app(settings)
    slug = f"synthetic-parity-{uuid4().hex[:8]}"
    content = "Synthetic violet comet. Synthetic amber forest."
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://control"
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="http://public"
        ) as rest,
    ):
        published = await admin.post(
            "/control/v1/snapshots",
            headers={"Authorization": "Bearer synthetic-parity-token"},
            json={
                "project_slug": slug,
                "project_name": "Synthetic Parity",
                "source_refs": ["fixture://parity"],
                "source_revision": "parity-rev-001",
                "generated_at": "2026-11-16T00:00:00Z",
                "verified_at": "2026-11-16T00:00:00Z",
                "documents": [{"id": "brief", "title": "Brief", "content": content}],
            },
        )
        assert published.status_code == 201, published.text
        project_id = published.json()["project_id"]
        key_response = await admin.post(
            "/control/v1/keys",
            headers={"Authorization": "Bearer synthetic-parity-token"},
            json={
                "label": "synthetic parity",
                "projects": {project_id: {"scopes": ["read", "search"]}},
            },
        )
        assert key_response.status_code == 201, key_response.text
        exchanged = await rest.post(
            "/api/v1/auth/exchange",
            headers={"Authorization": f"Bearer {key_response.json()['key']}"},
        )
        assert exchanged.status_code == 200, exchanged.text
        token = exchanged.json()["access_token"]
        rest_headers = {"Authorization": f"Bearer {token}"}
        rest_context = await rest.get(
            f"/api/v1/projects/{project_id}/context", headers=rest_headers
        )
        rest_search = await rest.get(
            f"/api/v1/projects/{project_id}/search",
            headers=rest_headers,
            params={"q": "synthetic"},
        )
        assert rest_context.status_code == rest_search.status_code == 200

        sdk_http = httpx2.AsyncClient(
            headers=rest_headers,
            transport=httpx2.ASGITransport(app=public),
        )
        async with sdk_http:
            transport = streamable_http_client("http://localhost/mcp", http_client=sdk_http)
            async with Client(transport) as sdk:
                mcp_context = await sdk.call_tool("get_project_context", {"project_id": project_id})
                mcp_search = await sdk.call_tool(
                    "search_project_context",
                    {"project_id": project_id, "query": "synthetic"},
                )

        assert not mcp_context.is_error and not mcp_search.is_error
        assert mcp_context.structured_content["documents"] == rest_context.json()["documents"]
        assert mcp_context.structured_content["snapshot"] == rest_context.json()["snapshot"]
        assert mcp_search.structured_content["results"] == rest_search.json()["results"]

        reusable = await admin.post(
            "/control/v2/credentials",
            headers={"Authorization": "Bearer synthetic-parity-token"},
            json={
                "credential_type": "reusable_api_v2",
                "label": "synthetic reusable parity",
                "projects": {
                    project_id: {
                        "scopes": ["read", "search", "export"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": published.json()["id"],
                    }
                },
            },
        )
        assert reusable.status_code == 201, reusable.text
        raw_headers = {"Authorization": f"Bearer {reusable.json()['key']}"}
        raw_context = await rest.get(f"/api/v1/projects/{project_id}/context", headers=raw_headers)
        raw_search = await rest.get(
            f"/api/v1/projects/{project_id}/search", headers=raw_headers, params={"q": "synthetic"}
        )
        assert raw_context.status_code == raw_search.status_code == 200
        async with httpx2.AsyncClient(
            headers=raw_headers, transport=httpx2.ASGITransport(app=public)
        ) as raw_mcp_http:
            transport = streamable_http_client("http://localhost/mcp", http_client=raw_mcp_http)
            async with Client(transport) as sdk:
                raw_mcp_context = await sdk.call_tool(
                    "get_project_context", {"project_id": project_id}
                )
                raw_mcp_export = await sdk.call_tool(
                    "export_project_context", {"project_id": project_id}
                )
        assert not raw_mcp_context.is_error and not raw_mcp_export.is_error
        assert raw_mcp_context.structured_content["documents"] == raw_context.json()["documents"]

        read_only_key = await admin.post(
            "/control/v1/keys",
            headers={"Authorization": "Bearer synthetic-parity-token"},
            json={"label": "synthetic read-only", "projects": {project_id: {"scopes": ["read"]}}},
        )
        assert read_only_key.status_code == 201, read_only_key.text
        read_only_exchange = await rest.post(
            "/api/v1/auth/exchange",
            headers={"Authorization": f"Bearer {read_only_key.json()['key']}"},
        )
        assert read_only_exchange.status_code == 200, read_only_exchange.text
        read_only_headers = {"Authorization": f"Bearer {read_only_exchange.json()['access_token']}"}
        denied_search = await rest.get(
            f"/api/v1/projects/{project_id}/search",
            headers=read_only_headers,
            params={"q": "synthetic"},
        )
        assert denied_search.status_code == 404

        read_only_http = httpx2.AsyncClient(
            headers=read_only_headers,
            transport=httpx2.ASGITransport(app=public),
        )
        async with read_only_http:
            transport = streamable_http_client("http://localhost/mcp", http_client=read_only_http)
            async with Client(transport) as sdk:
                denied_mcp_search = await sdk.call_tool(
                    "search_project_context",
                    {"project_id": project_id, "query": "synthetic"},
                )
        assert denied_mcp_search.is_error

        concurrent_key = await admin.post(
            "/control/v1/keys",
            headers={"Authorization": "Bearer synthetic-parity-token"},
            json={"label": "synthetic concurrent", "projects": {project_id: {"scopes": ["read"]}}},
        )
        assert concurrent_key.status_code == 201, concurrent_key.text

        async def exchange_concurrently():
            return await rest.post(
                "/api/v1/auth/exchange",
                headers={"Authorization": f"Bearer {concurrent_key.json()['key']}"},
            )

        exchanges = await asyncio.gather(exchange_concurrently(), exchange_concurrently())
        assert sorted(response.status_code for response in exchanges) == [200, 401]


@pytest.mark.asyncio
async def test_site_pause_blocks_exchange_existing_rest_mcp_and_export_then_resumes_without_ttl_reset(
    settings, db_sessions
) -> None:
    control = create_control_app(settings)
    public = create_public_app(settings)
    slug = f"synthetic-pause-{uuid4().hex[:8]}"
    control_headers = {"Authorization": "Bearer synthetic-parity-token"}
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://control"
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="http://public"
        ) as rest,
    ):
        before = await rest.get("/api/v1/site")
        assert before.status_code == 200 and before.json() == {"enabled": True}
        live_status = await admin.get("/control/v1/site", headers=control_headers)
        assert live_status.status_code == 200 and live_status.json() == {"enabled": True}
        published = await admin.post(
            "/control/v1/snapshots",
            headers=control_headers,
            json={
                "project_slug": slug,
                "project_name": "Synthetic Pause",
                "source_refs": ["fixture://pause"],
                "source_revision": "pause-rev",
                "generated_at": "2026-11-16T00:00:00Z",
                "verified_at": "2026-11-16T00:00:00Z",
                "documents": [{"id": "brief", "title": "Brief", "content": "synthetic data"}],
            },
        )
        assert published.status_code == 201, published.text
        project_id = published.json()["project_id"]
        issued = await admin.post(
            "/control/v1/keys",
            headers=control_headers,
            json={"label": "synthetic", "projects": {project_id: {"scopes": ["read", "export"]}}},
        )
        assert issued.status_code == 201, issued.text
        expires_at = issued.json()["expires_at"]
        unexchanged = await admin.post(
            "/control/v1/keys",
            headers=control_headers,
            json={"label": "synthetic unexchanged", "projects": {project_id: {"scopes": ["read"]}}},
        )
        assert unexchanged.status_code == 201, unexchanged.text
        unexchanged_expires_at = unexchanged.json()["expires_at"]
        async with control.state.session_factory() as db:
            stored_key_expiry = await db.scalar(
                select(APIKey.expires_at).where(
                    APIKey.credential_hash == hash_credential(unexchanged.json()["key"])
                )
            )
        exchanged = await rest.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {issued.json()['key']}"}
        )
        assert exchanged.status_code == 200, exchanged.text
        session_token = exchanged.json()["access_token"]
        session_expires_at = exchanged.json()["expires_at"]
        session_headers = {"Authorization": f"Bearer {session_token}"}
        assert (
            await rest.get(f"/api/v1/projects/{project_id}", headers=session_headers)
        ).status_code == 200
        original_snapshots = await rest.get(
            f"/api/v1/projects/{project_id}/snapshots", headers=session_headers
        )
        assert original_snapshots.status_code == 200
        original_snapshot_ids = [row["id"] for row in original_snapshots.json()]

        async with httpx.AsyncClient(
            headers=session_headers, transport=httpx.ASGITransport(app=public)
        ) as mcp_http:
            transport = streamable_http_client("http://localhost/mcp", http_client=mcp_http)
            async with Client(transport) as sdk:
                assert (await sdk.list_tools()).tools
                paused = await admin.patch(
                    "/control/v1/site", headers=control_headers, json={"enabled": False}
                )
                assert paused.status_code == 200 and paused.json() == {"enabled": False}
                assert (await rest.get("/api/v1/site")).json() == {"enabled": False}
                denied = [
                    await rest.get(f"/api/v1/projects/{project_id}", headers=session_headers),
                    await rest.get(
                        f"/api/v1/projects/{project_id}/export?format=json", headers=session_headers
                    ),
                    await rest.post(
                        "/api/v1/auth/exchange",
                        headers={"Authorization": f"Bearer {issued.json()['key']}"},
                    ),
                    await rest.post(
                        "/api/v1/auth/exchange",
                        headers={"Authorization": f"Bearer {unexchanged.json()['key']}"},
                    ),
                ]
                assert [response.status_code for response in denied] == [503, 503, 503, 503]
                assert all(
                    response.json() == {"detail": "site_temporarily_unavailable"}
                    for response in denied
                )
                with pytest.raises(MCPError):
                    await sdk.call_tool("list_projects", {})

                resumed = await admin.patch(
                    "/control/v1/site", headers=control_headers, json={"enabled": True}
                )
                assert resumed.status_code == 200 and resumed.json() == {"enabled": True}
                assert (
                    await rest.get(f"/api/v1/projects/{project_id}", headers=session_headers)
                ).status_code == 200
                assert (
                    await rest.get(
                        f"/api/v1/projects/{project_id}/export?format=json", headers=session_headers
                    )
                ).status_code == 200
                assert (
                    await rest.post(
                        "/api/v1/auth/exchange",
                        headers={"Authorization": f"Bearer {issued.json()['key']}"},
                    )
                ).status_code == 401
                assert issued.json()["expires_at"] == expires_at
                resumed_unexchanged = await rest.post(
                    "/api/v1/auth/exchange",
                    headers={"Authorization": f"Bearer {unexchanged.json()['key']}"},
                )
                assert resumed_unexchanged.status_code == 200, resumed_unexchanged.text
                assert unexchanged.json()["expires_at"] == unexchanged_expires_at
                async with control.state.session_factory() as db:
                    assert (
                        await db.scalar(
                            select(APIKey.expires_at).where(
                                APIKey.credential_hash == hash_credential(unexchanged.json()["key"])
                            )
                        )
                        == stored_key_expiry
                    )
                resumed_snapshots = await rest.get(
                    f"/api/v1/projects/{project_id}/snapshots", headers=session_headers
                )
                assert [row["id"] for row in resumed_snapshots.json()] == original_snapshot_ids
    async with db_sessions() as db:
        events = list((await db.scalars(select(SiteControlAudit.event))).all())
        assert events.count("site_status_read") == 1
        assert events.count("site_status_changed") == 2
        stored_session_expiry = await db.scalar(
            select(Session.expires_at).where(
                Session.credential_hash == hash_credential(session_token)
            )
        )
        assert stored_session_expiry.isoformat().replace(
            "+00:00", "Z"
        ) == session_expires_at.replace("+00:00", "Z")


@pytest.mark.asyncio
async def test_site_control_requires_control_token_and_public_role_cannot_change_site(
    settings,
) -> None:
    control = create_control_app(settings)
    async with (
        control.router.lifespan_context(control),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://control"
        ) as client,
    ):
        assert (await client.get("/control/v1/site")).status_code == 401
        assert (await client.patch("/control/v1/site", json={"enabled": False})).status_code == 401


@pytest.mark.asyncio
async def test_missing_site_state_stays_closed_after_restart_until_explicit_resume(
    settings,
) -> None:
    control = create_control_app(settings)
    public = create_public_app(settings)
    auth = {"Authorization": "Bearer synthetic-parity-token"}
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://control"
        ) as admin,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="http://public"
        ) as rest,
    ):
        async with control.state.session_factory() as db:
            assert (
                await db.scalar(text("SELECT current_database()"))
                == make_url(settings.database_url).database
            )
        assert (await rest.get("/api/v1/site")).json() == {"enabled": True}
        async with control.state.session_factory() as db:
            await db.execute(text("DELETE FROM gateway_site_control WHERE id = 1"))
            await db.commit()

        status = await rest.get("/api/v1/site")
        assert status.json() == {"enabled": False}
        assert status.headers["cache-control"] == "no-store"
        denied = await rest.get("/api/v1/projects/not-a-project", headers=auth)
        assert denied.status_code == 503
        assert denied.json() == {"detail": "site_temporarily_unavailable"}
        assert denied.headers["cache-control"] == "no-store"

        await _initialize_schema(control.state.session_factory)
        after_restart = await rest.get("/api/v1/site")
        assert after_restart.json() == {"enabled": False}

        resumed = await admin.patch("/control/v1/site", headers=auth, json={"enabled": True})
        assert resumed.status_code == 200 and resumed.json() == {"enabled": True}
        resumed_status = await rest.get("/api/v1/site")
        assert resumed_status.json() == {"enabled": True}
        assert resumed_status.headers["cache-control"] == "no-store"
        assert (await rest.get("/api/v1/projects/not-a-project", headers=auth)).status_code != 503


@pytest.mark.asyncio
async def test_public_database_role_can_read_site_switch_but_cannot_write_or_read_audit(
    postgres_url, tmp_path
):
    from context_gateway.apps import _initialize_schema
    from context_gateway.database import make_session_factory

    role = "context_gateway_public"
    engine = create_async_engine(postgres_url)
    created_role = False
    try:
        async with engine.begin() as connection:
            exists = await connection.scalar(
                text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :name)"), {"name": role}
            )
            if not exists:
                await connection.execute(text(f'CREATE ROLE "{role}" NOLOGIN'))
                created_role = True
            await connection.execute(text(f'GRANT USAGE ON SCHEMA public TO "{role}"'))
        settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-site-token")
        factory = make_session_factory(settings)
        await _initialize_schema(factory)
        async with engine.connect() as connection:
            transaction = await connection.begin()
            try:
                await connection.execute(text(f'SET LOCAL ROLE "{role}"'))
                assert (
                    await connection.scalar(
                        text("SELECT enabled FROM gateway_site_control WHERE id = 1")
                    )
                    is True
                )
                for column in ("last_used_at", "window_started_at", "window_count"):
                    assert await connection.scalar(
                        text(
                            "SELECT has_column_privilege(current_user, 'gateway_api_keys', :column, 'UPDATE')"
                        ),
                        {"column": column},
                    )
                assert not await connection.scalar(
                    text(
                        "SELECT has_column_privilege(current_user, 'gateway_api_keys', 'credential_type', 'UPDATE')"
                    )
                )
                async with connection.begin_nested():
                    with pytest.raises(DBAPIError):
                        await connection.execute(
                            text("UPDATE gateway_site_control SET enabled = FALSE WHERE id = 1")
                        )
                    await connection.get_nested_transaction().rollback()
                async with connection.begin_nested():
                    with pytest.raises(DBAPIError):
                        await connection.execute(
                            text("SELECT enabled FROM gateway_site_control_audit")
                        )
                    await connection.get_nested_transaction().rollback()
            finally:
                await transaction.rollback()
        await factory.kw["bind"].dispose()
    finally:
        if created_role:
            async with engine.begin() as connection:
                await connection.execute(text(f'DROP OWNED BY "{role}"'))
                await connection.execute(text(f'DROP ROLE "{role}"'))
        await engine.dispose()
