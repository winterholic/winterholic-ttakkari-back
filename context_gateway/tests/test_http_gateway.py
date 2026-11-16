from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime, timedelta
from io import BytesIO
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.auth import hash_credential
from context_gateway.config import Settings
from context_gateway.models import APIKey, Project, Session, Snapshot


@pytest.fixture
def settings(postgres_url, tmp_path) -> Settings:
    return Settings(postgres_url, tmp_path / "snapshots", "synthetic-admin-token", 200)


async def _client(app, base_url: str = "http://test"):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url=base_url)


@pytest.mark.asyncio
async def test_publish_exchange_rest_export_revoke_and_key_secrecy(settings) -> None:
    control = create_control_app(settings)
    public = create_public_app(settings)
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        await _client(control) as admin,
        await _client(public) as api,
    ):
        payload = {
            "project_slug": "synthetic-alpha",
            "project_name": "Synthetic Alpha",
            "project_description": "fixture only",
            "source_refs": ["fixture://source-a"],
            "source_revision": "fixture-rev-001",
            "generated_at": "2026-11-16T00:00:00Z",
            "verified_at": "2026-11-16T00:05:00Z",
            "documents": [
                {
                    "id": "brief",
                    "title": "Synthetic Brief",
                    "content": "Synthetic red apple. Synthetic blue lake.",
                    "sha256": hashlib.sha256(
                        b"Synthetic red apple. Synthetic blue lake."
                    ).hexdigest(),
                },
                {
                    "id": "notes",
                    "title": "Synthetic Notes",
                    "content": "another fixture",
                    "content_type": "text/plain",
                },
            ],
        }
        published = await admin.post(
            "/control/v1/snapshots",
            headers={"Authorization": "Bearer synthetic-admin-token"},
            json=payload,
        )
        assert published.status_code == 201, published.text
        snapshot_id = published.json()["id"]
        async with public.state.session_factory() as db:
            project = await db.scalar(select(Project).where(Project.slug == "synthetic-alpha"))
            assert project is not None
            project_id = str(project.id)
        assert published.json()["project_id"] == project_id
        other = await admin.post(
            "/control/v1/snapshots",
            headers={"Authorization": "Bearer synthetic-admin-token"},
            json={**payload, "project_slug": "synthetic-beta", "project_name": "Synthetic Beta"},
        )
        assert other.status_code == 201, other.text
        async with public.state.session_factory() as db:
            other_project = await db.scalar(select(Project).where(Project.slug == "synthetic-beta"))
            assert other_project is not None
            other_project_id = str(other_project.id)

        issued = await admin.post(
            "/control/v1/keys",
            headers={"Authorization": "Bearer synthetic-admin-token"},
            json={
                "label": "synthetic-client",
                "projects": {project_id: {"scopes": ["read", "search", "export"]}},
            },
        )
        assert issued.status_code == 201, issued.text
        key = issued.json()["key"]
        key_id = issued.json()["key_id"]
        async with public.state.session_factory() as db:
            saved_key = await db.get(APIKey, UUID(key_id))
            assert saved_key is not None and key not in saved_key.credential_hash

        assert (await api.get("/api/v1/projects")).status_code == 401
        exchange = await api.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {key}"}
        )
        assert exchange.status_code == 200, exchange.text
        session_token = exchange.json()["access_token"]
        assert (
            await api.post("/api/v1/auth/exchange", headers={"Authorization": f"Bearer {key}"})
        ).status_code == 401
        headers = {"Authorization": f"Bearer {session_token}"}

        projects = await api.get("/api/v1/projects", headers=headers)
        assert projects.status_code == 200 and len(projects.json()) == 1
        context = await api.get(
            f"/api/v1/projects/{project_id}/context",
            headers={**headers, "Accept": "application/json"},
            params={"snapshot_id": snapshot_id},
        )
        assert (
            context.status_code == 200
            and context.json()["snapshot"]["source_revision"] == "fixture-rev-001"
        )
        markdown = await api.get(
            f"/api/v1/projects/{project_id}/context", headers={**headers, "Accept": "text/markdown"}
        )
        assert markdown.status_code == 200 and markdown.headers["content-type"].startswith(
            "text/markdown"
        )
        document = await api.get(f"/api/v1/projects/{project_id}/documents/brief", headers=headers)
        assert (
            document.status_code == 200
            and document.json()["sha256"]
            == hashlib.sha256(b"Synthetic red apple. Synthetic blue lake.").hexdigest()
        )
        search = await api.get(
            f"/api/v1/projects/{project_id}/search", headers=headers, params={"q": "synthetic"}
        )
        assert search.status_code == 200 and search.json()["results"][0]["matches"] == 2
        exported_json = await api.get(
            f"/api/v1/projects/{project_id}/export", headers=headers, params={"format": "json"}
        )
        assert exported_json.status_code == 200 and len(exported_json.json()["documents"]) == 2
        exported_md = await api.get(
            f"/api/v1/projects/{project_id}/export", headers=headers, params={"format": "markdown"}
        )
        assert exported_md.headers["content-type"].startswith("text/markdown")
        exported_zip = await api.get(
            f"/api/v1/projects/{project_id}/export", headers=headers, params={"format": "zip"}
        )
        with zipfile.ZipFile(BytesIO(exported_zip.content)) as archive:
            assert {"manifest.json", "documents/brief.md"} <= set(archive.namelist())

        denied = await api.get(f"/api/v1/projects/{UUID(int=0)}/context", headers=headers)
        assert denied.status_code == 404 and "synthetic-alpha" not in denied.text
        denied_other_project = await api.get(
            f"/api/v1/projects/{other_project_id}", headers=headers
        )
        assert (
            denied_other_project.status_code == 404
            and "Synthetic Beta" not in denied_other_project.text
        )
        query_auth = await api.get("/api/v1/projects", params={"api_key": session_token})
        assert query_auth.status_code == 401
        assert key not in json.dumps(published.json()) + json.dumps(projects.json()) + json.dumps(
            context.json()
        ) + json.dumps(search.json())

        async with public.state.session_factory() as db:
            session_row = await db.scalar(
                select(Session).where(Session.credential_hash == hash_credential(session_token))
            )
            assert session_row is not None
            session_row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await db.commit()
        assert (await api.get("/api/v1/projects", headers=headers)).status_code == 401

        doc_path = settings.snapshot_dir / project_id / snapshot_id / "documents" / "brief.md"
        doc_path.chmod(0o644)
        doc_path.write_text("tampered synthetic fixture", encoding="utf-8")
        async with public.state.session_factory() as db:
            session_row = await db.scalar(
                select(Session).where(Session.credential_hash == hash_credential(session_token))
            )
            assert session_row is not None
            session_row.expires_at = datetime.now(UTC) + timedelta(minutes=5)
            await db.commit()
        corrupted = await api.get(f"/api/v1/projects/{project_id}/documents/brief", headers=headers)
        assert corrupted.status_code == 409 and "tampered synthetic" not in corrupted.text
        doc_path.write_text("Synthetic red apple. Synthetic blue lake.", encoding="utf-8")
        doc_path.chmod(0o444)
        revoked_snapshot = await admin.post(
            f"/control/v1/snapshots/{snapshot_id}/revoke",
            headers={"Authorization": "Bearer synthetic-admin-token"},
        )
        assert revoked_snapshot.status_code == 200
        assert (
            await api.get(f"/api/v1/projects/{project_id}/context", headers=headers)
        ).status_code == 404

        revoked = await admin.post(
            f"/control/v1/keys/{key_id}/revoke",
            headers={"Authorization": "Bearer synthetic-admin-token"},
        )
        assert revoked.status_code == 200
        assert (await api.get("/api/v1/projects", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_publisher_rejects_hash_mismatch_without_publishing(settings) -> None:
    control = create_control_app(settings)
    async with control.router.lifespan_context(control), await _client(control) as admin:
        response = await admin.post(
            "/control/v1/snapshots",
            headers={"Authorization": "Bearer synthetic-admin-token"},
            json={
                "project_slug": "synthetic-bad",
                "project_name": "Synthetic Bad",
                "source_refs": ["fixture://source"],
                "source_revision": "fixture-rev",
                "generated_at": "2026-11-16T00:00:00Z",
                "verified_at": "2026-11-16T00:00:00Z",
                "documents": [
                    {"id": "doc", "title": "bad", "content": "payload", "sha256": "0" * 64}
                ],
            },
        )
        assert response.status_code == 409
        async with control.state.session_factory() as db:
            assert await db.scalar(select(Project).where(Project.slug == "synthetic-bad")) is None
            assert await db.scalar(select(Snapshot.id)) is None


@pytest.mark.asyncio
async def test_mcp_streamable_http_uses_real_python_sdk_client(settings) -> None:
    import httpx2
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client

    control = create_control_app(settings)
    public = create_public_app(settings)
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        await _client(control) as admin,
    ):
        published = await admin.post(
            "/control/v1/snapshots",
            headers={"Authorization": "Bearer synthetic-admin-token"},
            json={
                "project_slug": "mcp-fixture",
                "project_name": "MCP Fixture",
                "source_refs": ["fixture://mcp"],
                "source_revision": "rev-mcp",
                "generated_at": "2026-11-16T00:00:00Z",
                "verified_at": "2026-11-16T00:00:00Z",
                "documents": [
                    {
                        "id": "guide",
                        "title": "Guide",
                        "content": "the synthetic compass points north",
                    }
                ],
            },
        )
        assert published.status_code == 201, published.text
        async with public.state.session_factory() as db:
            project = await db.scalar(select(Project).where(Project.slug == "mcp-fixture"))
            assert project is not None
            project_id = str(project.id)
        issued = await admin.post(
            "/control/v1/keys",
            headers={"Authorization": "Bearer synthetic-admin-token"},
            json={"label": "mcp-fixture", "projects": {project_id: {"scopes": ["read", "search"]}}},
        )
        assert issued.status_code == 201
        key = issued.json()["key"]
        async with await _client(public) as api:
            exchange = await api.post(
                "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {key}"}
            )
        session_token = exchange.json()["access_token"]

        http_client = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {session_token}"},
            transport=httpx2.ASGITransport(app=public),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="http://localhost"
        ) as no_auth:
            unauthenticated_mcp = await no_auth.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
            )
            assert unauthenticated_mcp.status_code == 401
        async with http_client:
            transport = streamable_http_client("http://localhost/mcp", http_client=http_client)
            async with Client(transport) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "list_projects",
                    "get_project_context",
                    "search_project_context",
                    "get_document",
                }
                assert all(
                    tool.annotations and tool.annotations.read_only_hint for tool in tools.tools
                )
                projects = await client.call_tool("list_projects", {})
                assert "MCP Fixture" in str(projects)
                found = await client.call_tool(
                    "search_project_context", {"project_id": project_id, "query": "compass"}
                )
                assert "north" in str(found)
                doc = await client.call_tool(
                    "get_document", {"project_id": project_id, "document_id": "guide"}
                )
                assert "synthetic compass" in str(doc)
                context = await client.call_tool("get_project_context", {"project_id": project_id})
                assert "rev-mcp" in str(context)
