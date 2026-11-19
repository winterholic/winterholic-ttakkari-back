from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.auth import hash_credential, validate_session
from context_gateway.config import Settings
from context_gateway.models import APIKey, KeyACL, Project, Session, Snapshot
from context_gateway.publishing import issue_v2_credential


@pytest.mark.asyncio
async def test_pinned_metadata_stays_at_grant_snapshot_for_rest_and_mcp(
    postgres_url, tmp_path
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    control = create_control_app(settings)
    public = create_public_app(settings)
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
        admin_headers = {"Authorization": "Bearer synthetic-control"}
        first = await admin.post(
            "/control/v1/snapshots",
            headers=admin_headers,
            json={
                "project_slug": "pinned-metadata-fixture",
                "project_name": "Published Name",
                "project_description": "Published summary",
                "source_refs": ["fixture://metadata"],
                "source_revision": "rev-1",
                "generated_at": "2026-11-19T00:00:00Z",
                "documents": [{"id": "doc", "title": "Doc", "content": "pinned body"}],
            },
        )
        project_id = first.json()["project_id"]
        issued = await admin.post(
            "/control/v2/credentials",
            headers=admin_headers,
            json={
                "credential_type": "reusable_api_v2",
                "label": "pinned metadata fixture",
                "projects": {
                    project_id: {
                        "scopes": ["read"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": first.json()["id"],
                    }
                },
            },
        )
        share_issued = await admin.post(
            "/control/v2/credentials",
            headers=admin_headers,
            json={
                "credential_type": "share_link_v2",
                "label": "pinned metadata link",
                "projects": {
                    project_id: {
                        "scopes": ["read"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": first.json()["id"],
                    }
                },
            },
        )
        second = await admin.post(
            "/control/v1/snapshots",
            headers=admin_headers,
            json={
                "project_slug": "pinned-metadata-fixture",
                "project_name": "New Name Must Stay Hidden",
                "project_description": "New summary must stay hidden",
                "source_refs": ["fixture://metadata"],
                "source_revision": "rev-2",
                "generated_at": "2026-11-19T00:00:00Z",
                "documents": [{"id": "doc", "title": "Doc", "content": "new body"}],
            },
        )
        assert first.status_code == second.status_code == 201
        issued_after_new_publish = await admin.post(
            "/control/v2/credentials",
            headers=admin_headers,
            json={
                "credential_type": "reusable_api_v2",
                "label": "old snapshot issued late",
                "projects": {
                    project_id: {
                        "scopes": ["read"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": first.json()["id"],
                    }
                },
            },
        )
        late_list = await rest.get(
            "/api/v1/projects",
            headers={"Authorization": f"Bearer {issued_after_new_publish.json()['key']}"},
        )
        assert late_list.status_code == 200
        assert late_list.json()[0]["name"] == "Published Name"
        assert late_list.json()[0]["description"] == "Published summary"
        assert late_list.json()[0]["latest_snapshot_id"] == first.json()["id"]
        async with public.state.session_factory() as db:
            latest = await db.get(Snapshot, second.json()["id"])
            assert latest.manifest["project_name"] == "New Name Must Stay Hidden"
            assert latest.manifest["project_description"] == "New summary must stay hidden"
        raw = issued.json()["key"]
        headers = {"Authorization": f"Bearer {raw}"}
        listed = await rest.get("/api/v1/projects", headers=headers)
        project = await rest.get(f"/api/v1/projects/{project_id}", headers=headers)
        context = await rest.get(f"/api/v1/projects/{project_id}/context", headers=headers)
        snapshots = await rest.get(f"/api/v1/projects/{project_id}/snapshots", headers=headers)
        assert second.json()["id"] != first.json()["id"]
        assert listed.status_code == project.status_code == context.status_code == 200
        assert listed.json()[0]["name"] == "Published Name"
        assert listed.json()[0]["description"] == "Published summary"
        assert listed.json()[0]["latest_snapshot_id"] == first.json()["id"]
        assert project.json()["name"] == "Published Name"
        assert project.json()["description"] == "Published summary"
        assert project.json()["latest_snapshot_id"] == first.json()["id"]
        assert context.json()["project"] == project.json()
        assert context.json()["documents"][0]["content"] == "pinned body"
        assert [item["id"] for item in snapshots.json()] == [first.json()["id"]]
        for result in (listed, project, context, snapshots):
            assert "New Name Must Stay Hidden" not in result.text
            assert "New summary must stay hidden" not in result.text
            assert second.json()["id"] not in result.text

        async with httpx.AsyncClient(
            headers=headers, transport=httpx.ASGITransport(app=public)
        ) as mcp_http:
            transport = streamable_http_client("http://localhost/mcp", http_client=mcp_http)
            async with Client(transport) as sdk:
                mcp_projects = await sdk.call_tool("list_projects", {})
                mcp_context = await sdk.call_tool("get_project_context", {"project_id": project_id})
        assert not mcp_projects.is_error and not mcp_context.is_error
        mcp_project = mcp_projects.structured_content.get(
            "result", mcp_projects.structured_content
        )[0]
        assert mcp_project["name"] == "Published Name"
        assert mcp_project["description"] == "Published summary"
        assert mcp_project["latest_snapshot_id"] == first.json()["id"]
        assert mcp_context.structured_content["project"] == project.json()
        assert mcp_context.structured_content["snapshot"]["id"] == first.json()["id"]
        assert "New Name Must Stay Hidden" not in str(mcp_projects.structured_content)
        assert second.json()["id"] not in str(mcp_context.structured_content)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="https://public"
        ) as browser:
            exchange = await browser.post(
                "/api/v1/share/exchange",
                json={"token": share_issued.json()["key"]},
                headers={"Origin": "https://public", "Content-Type": "application/json"},
            )
            share_context = await browser.get("/api/v1/share/context")
        assert exchange.status_code == share_context.status_code == 200
        assert share_context.json()["project"]["name"] == "Published Name"
        assert share_context.json()["project"]["description"] == "Published summary"
        assert share_context.json()["project"]["latest_snapshot_id"] == first.json()["id"]


@pytest.mark.asyncio
async def test_v2_auth_errors_distinguish_expiry_revocation_and_audience(
    postgres_url, tmp_path
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    control = create_control_app(settings)
    public = create_public_app(settings)
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
        admin_headers = {"Authorization": "Bearer synthetic-control"}
        published = await admin.post(
            "/control/v1/snapshots",
            headers=admin_headers,
            json={
                "project_slug": "auth-errors-fixture",
                "project_name": "Auth fixture",
                "source_refs": ["fixture://auth"],
                "source_revision": "rev-1",
                "generated_at": "2026-11-19T00:00:00Z",
                "documents": [{"id": "doc", "title": "Doc", "content": "body"}],
            },
        )
        project_id = published.json()["project_id"]

        async def issue(credential_type: str) -> dict:
            response = await admin.post(
                "/control/v2/credentials",
                headers=admin_headers,
                json={
                    "credential_type": credential_type,
                    "label": credential_type,
                    "projects": {
                        project_id: {
                            "scopes": ["read"],
                            "snapshot_policy": "pinned",
                            "snapshot_id": published.json()["id"],
                        }
                    },
                },
            )
            assert response.status_code == 201, response.text
            return response.json()

        expired = await issue("reusable_api_v2")
        revoked = await issue("reusable_api_v2")
        expired_session_key = await issue("reusable_api_v2")
        revoked_session_key = await issue("reusable_api_v2")
        share = await issue("share_link_v2")
        session_tokens = []
        for credential in (expired_session_key, revoked_session_key):
            exchanged = await rest.post(
                "/api/v1/auth/exchange",
                headers={"Authorization": f"Bearer {credential['key']}"},
            )
            assert exchanged.status_code == 200
            session_tokens.append(exchanged.json()["access_token"])
        async with public.state.session_factory() as db:
            expired_key = await db.scalar(
                select(APIKey).where(
                    APIKey.credential_hash.is_not(None), APIKey.id == expired["key_id"]
                )
            )
            expired_key.expires_at = expired_key.created_at.replace(year=2000)
            revoked_key = await db.get(APIKey, revoked["key_id"])
            revoked_key.revoked_at = revoked_key.created_at
            expired_session = await db.scalar(
                select(Session).where(Session.credential_hash == hash_credential(session_tokens[0]))
            )
            expired_session.expires_at = datetime(2000, 1, 1, tzinfo=UTC)
            revoked_session = await db.scalar(
                select(Session).where(Session.credential_hash == hash_credential(session_tokens[1]))
            )
            revoked_session.revoked_at = datetime.now(UTC)
            await db.commit()
        outcomes = []
        mcp_outcomes = []
        for token in (expired["key"], revoked["key"], *session_tokens):
            response = await rest.get(
                "/api/v1/projects", headers={"Authorization": f"Bearer {token}"}
            )
            outcomes.append((response.status_code, response.json().get("detail")))
            mcp_response = await rest.post(
                "/mcp", headers={"Authorization": f"Bearer {token}"}, json={}
            )
            mcp_outcomes.append((mcp_response.status_code, mcp_response.json().get("detail")))
        share_exchange = await rest.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {share['key']}"}
        )
        share_mcp = await rest.post(
            "/mcp", headers={"Authorization": f"Bearer {share['key']}"}, json={}
        )
        assert outcomes == [
            (401, "credential_expired"),
            (401, "credential_revoked"),
            (401, "credential_expired"),
            (401, "credential_revoked"),
        ]
        assert mcp_outcomes == outcomes
        assert share_exchange.status_code == 401
        assert share_exchange.json()["detail"] == "audience_mismatch"
        assert share_mcp.status_code == 401
        assert share_mcp.json()["detail"] == "audience_mismatch"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=public), base_url="https://public"
        ) as browser:
            share_cookie = await browser.post(
                "/api/v1/share/exchange",
                json={"token": share["key"]},
                headers={"Origin": "https://public", "Content-Type": "application/json"},
            )
            assert share_cookie.status_code == 200
            share_session = browser.cookies.get("context_gateway_share")
            assert share_session
            async with public.state.session_factory() as db:
                assert await validate_session(db, share_session) is False
            rest_audience = await browser.get(
                "/api/v1/projects", headers={"Authorization": f"Bearer {share_session}"}
            )
            mcp_audience = await browser.post(
                "/mcp", headers={"Authorization": f"Bearer {share_session}"}, json={}
            )
        assert rest_audience.status_code == 401
        assert rest_audience.json()["detail"] == "audience_mismatch"
        assert mcp_audience.status_code == 401
        assert mcp_audience.json()["detail"] == "audience_mismatch"


@pytest.mark.asyncio
async def test_legacy_pinned_snapshot_captures_issue_time_metadata(db_sessions) -> None:
    now = datetime.now(UTC)
    async with db_sessions() as db:
        project = Project(
            slug="legacy-pinned-fixture",
            name="Name at issue",
            description="Summary at issue",
        )
        db.add(project)
        await db.flush()
        snapshot = Snapshot(
            project_id=project.id,
            source_refs=["fixture://legacy"],
            source_revision="legacy-rev",
            generated_at=now,
            verified_at=None,
            manifest={"snapshot_id": "legacy", "documents": []},
            manifest_sha256="0" * 64,
            storage_path="/unused/legacy-fixture",
            published_at=now,
        )
        db.add(snapshot)
        await db.flush()
        await issue_v2_credential(
            db,
            label="legacy pinned",
            credential_type="reusable_api_v2",
            ttl_days=3,
            project_grants={
                project.id: {
                    "scopes": ["read"],
                    "snapshot_policy": "pinned",
                    "snapshot_id": snapshot.id,
                }
            },
        )
        acl = await db.scalar(select(KeyACL).where(KeyACL.project_id == project.id))
        project.name = "Current name must not leak"
        project.description = "Current summary must not leak"
        await db.flush()
        from context_gateway.apps import _credential_project_out

        issued_metadata = await _credential_project_out(
            db, await db.get(APIKey, acl.key_id), project
        )
        assert issued_metadata.name == "Name at issue"
        assert issued_metadata.description == "Summary at issue"


@pytest.mark.asyncio
async def test_publish_manifest_tracks_description_when_name_is_unchanged(
    postgres_url, tmp_path
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    control = create_control_app(settings)
    async with (
        control.router.lifespan_context(control),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://control"
        ) as admin,
    ):
        headers = {"Authorization": "Bearer synthetic-control"}
        payload = {
            "project_slug": "same-name-metadata-fixture",
            "project_name": "Stable Name",
            "project_description": "First summary",
            "source_refs": ["fixture://same-name"],
            "source_revision": "rev-1",
            "generated_at": "2026-11-19T00:00:00Z",
            "documents": [{"id": "doc", "title": "Doc", "content": "body"}],
        }
        first = await admin.post("/control/v1/snapshots", headers=headers, json=payload)
        second = await admin.post(
            "/control/v1/snapshots",
            headers=headers,
            json={**payload, "project_description": "Second summary", "source_revision": "rev-2"},
        )
        assert first.status_code == second.status_code == 201
        async with control.state.session_factory() as db:
            latest = await db.get(Snapshot, second.json()["id"])
            project = await db.get(Project, latest.project_id)
            assert latest.manifest["project_description"] == "Second summary"
            assert project.description == "Second summary"
