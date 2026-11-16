from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.config import Settings


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
                "documents": [
                    {"id": "brief", "title": "Brief", "content": content}
                ],
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
                mcp_context = await sdk.call_tool(
                    "get_project_context", {"project_id": project_id}
                )
                mcp_search = await sdk.call_tool(
                    "search_project_context",
                    {"project_id": project_id, "query": "synthetic"},
                )

        assert not mcp_context.is_error and not mcp_search.is_error
        assert mcp_context.structured_content["documents"] == rest_context.json()["documents"]
        assert mcp_context.structured_content["snapshot"] == rest_context.json()["snapshot"]
        assert mcp_search.structured_content["results"] == rest_search.json()["results"]

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
        read_only_headers = {
            "Authorization": f"Bearer {read_only_exchange.json()['access_token']}"
        }
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
