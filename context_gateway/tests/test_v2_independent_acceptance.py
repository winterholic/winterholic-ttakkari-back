from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.auth import hash_credential
from context_gateway.config import Settings
from context_gateway.models import APIKey, Project, Snapshot


def _snapshot_payload(slug: str, revision: str, content: str) -> dict:
    return {
        "project_slug": slug,
        "project_name": f"Project {slug}",
        "project_description": "Synthetic acceptance fixture",
        "source_refs": [f"fixture://{slug}"],
        "source_revision": revision,
        "generated_at": "2026-11-19T00:00:00Z",
        "documents": [{"id": "doc", "title": "Fixture", "content": content}],
    }


@pytest.mark.asyncio
async def test_ttl_matrix_and_default_are_receipted_and_raw_secrets_are_hash_only(
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
        published = await admin.post(
            "/control/v1/snapshots",
            headers=headers,
            json=_snapshot_payload("ttl-matrix", "rev-1", "synthetic body"),
        )
        assert published.status_code == 201, published.text
        project_id = published.json()["project_id"]
        snapshot_id = published.json()["id"]

        receipts: list[dict] = []
        for credential_type in ("reusable_api_v2", "share_link_v2"):
            for ttl_days in (1, 3, 7, 30):
                response = await admin.post(
                    "/control/v2/credentials",
                    headers=headers,
                    json={
                        "credential_type": credential_type,
                        "label": f"{credential_type}-{ttl_days}",
                        "ttl_days": ttl_days,
                        "projects": {
                            project_id: {
                                "scopes": ["read"],
                                "snapshot_policy": "pinned",
                                "snapshot_id": snapshot_id,
                            }
                        },
                    },
                )
                assert response.status_code == 201, response.text
                receipt = response.json()
                assert receipt["ttl_days"] == ttl_days
                created = datetime.fromisoformat(receipt["created_at"])
                expires = datetime.fromisoformat(receipt["expires_at"])
                assert abs((expires - created) - timedelta(days=ttl_days)) < timedelta(seconds=3)
                assert receipt["credential_type"] == credential_type
                if credential_type == "share_link_v2":
                    # The private control adapter needs the one-time raw token to
                    # construct its fragment URL; only the Gateway receipt carries it.
                    assert receipt.get("key") and "url" not in receipt
                receipts.append(receipt)

        default = await admin.post(
            "/control/v2/credentials",
            headers=headers,
            json={
                "credential_type": "share_link_v2",
                "label": "default link TTL",
                "projects": {
                    project_id: {
                        "scopes": ["read"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": snapshot_id,
                    }
                },
            },
        )
        assert default.status_code == 201, default.text
        assert default.json()["ttl_days"] == 3
        receipts.append(default.json())

        status = await admin.post(
            "/control/v2/credentials/status",
            headers=headers,
            json={"key_ids": [receipt["key_id"] for receipt in receipts]},
        )
        assert status.status_code == 200, status.text
        assert len(status.json()["credentials"]) == len(receipts)
        for receipt in receipts:
            assert receipt["key"] not in status.text
            if receipt.get("url"):
                assert receipt["url"] not in status.text
            async with control.state.session_factory() as db:
                key = await db.get(APIKey, receipt["key_id"])
                assert key is not None
                assert key.credential_hash == hash_credential(receipt["key"])
                assert receipt["key"] not in key.credential_hash


@pytest.mark.asyncio
async def test_share_sessions_recheck_expiry_revocation_and_snapshot_project_withdrawal(
    postgres_url, tmp_path
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    control = create_control_app(settings)
    public = create_public_app(settings)
    cases = ("expired", "revoked", "snapshot_revoked", "project_revoked")
    async with (
        control.router.lifespan_context(control),
        public.router.lifespan_context(public),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control), base_url="http://control"
        ) as admin,
    ):
        admin_headers = {"Authorization": "Bearer synthetic-control"}
        for case in cases:
            slug = f"withdrawal-{case}-{uuid4().hex[:8]}"
            published = await admin.post(
                "/control/v1/snapshots",
                headers=admin_headers,
                json=_snapshot_payload(slug, "rev-1", f"body for {case}"),
            )
            assert published.status_code == 201, published.text
            project_id, snapshot_id = published.json()["project_id"], published.json()["id"]
            issued = await admin.post(
                "/control/v2/credentials",
                headers=admin_headers,
                json={
                    "credential_type": "share_link_v2",
                    "label": case,
                    "projects": {
                        project_id: {
                            "scopes": ["read"],
                            "snapshot_policy": "pinned",
                            "snapshot_id": snapshot_id,
                        }
                    },
                },
            )
            assert issued.status_code == 201, issued.text
            browser = httpx.AsyncClient(
                transport=httpx.ASGITransport(app=public), base_url="https://public"
            )
            try:
                exchange = await browser.post(
                    "/api/v1/share/exchange",
                    json={"token": issued.json()["key"]},
                    headers={"Origin": "https://public", "Content-Type": "application/json"},
                )
                assert exchange.status_code == 200, exchange.text
                cookie = exchange.headers["set-cookie"].lower()
                assert "secure" in cookie and "httponly" in cookie
                assert "samesite=strict" in cookie
                assert "path=/api/v1/share" in cookie
                assert (await browser.get("/api/v1/share/context")).status_code == 200

                if case == "expired":
                    async with public.state.session_factory() as db:
                        key = await db.get(APIKey, issued.json()["key_id"])
                        key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
                        await db.commit()
                    expected_code = "credential_expired"
                elif case == "revoked":
                    revoked = await admin.post(
                        f"/control/v1/keys/{issued.json()['key_id']}/revoke",
                        headers=admin_headers,
                    )
                    assert revoked.status_code == 200, revoked.text
                    expected_code = "credential_revoked"
                elif case == "snapshot_revoked":
                    revoked = await admin.post(
                        f"/control/v1/snapshots/{snapshot_id}/revoke", headers=admin_headers
                    )
                    assert revoked.status_code == 200, revoked.text
                    expected_code = None
                else:
                    revoked = await admin.post(
                        f"/control/v1/projects/{project_id}/revoke", headers=admin_headers
                    )
                    assert revoked.status_code == 200, revoked.text
                    expected_code = None

                denied = await browser.get("/api/v1/share/context")
                assert denied.status_code in (401, 404), (case, denied.status_code, denied.text)
                if expected_code is not None:
                    assert denied.json().get("detail") == expected_code
                assert issued.json()["key"] not in denied.text
            finally:
                await browser.aclose()


@pytest.mark.asyncio
async def test_each_api_scope_has_matching_rest_and_mcp_allow_deny_matrix(
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
            json=_snapshot_payload("scope-matrix", "rev-1", "matrix search export payload"),
        )
        assert published.status_code == 201, published.text
        project_id, snapshot_id = published.json()["project_id"], published.json()["id"]
        routes = {
            "read": ("context", f"/api/v1/projects/{project_id}/context"),
            "search": ("search", f"/api/v1/projects/{project_id}/search?q=matrix"),
            "export": ("export", f"/api/v1/projects/{project_id}/export?format=markdown"),
        }
        for granted_scope in ("read", "search", "export"):
            issued = await admin.post(
                "/control/v2/credentials",
                headers=admin_headers,
                json={
                    "credential_type": "reusable_api_v2",
                    "label": f"only-{granted_scope}",
                    "projects": {
                        project_id: {
                            "scopes": [granted_scope],
                            "snapshot_policy": "pinned",
                            "snapshot_id": snapshot_id,
                        }
                    },
                },
            )
            assert issued.status_code == 201, issued.text
            raw_headers = {"Authorization": f"Bearer {issued.json()['key']}"}
            first_exchange = await rest.post("/api/v1/auth/exchange", headers=raw_headers)
            second_exchange = await rest.post("/api/v1/auth/exchange", headers=raw_headers)
            assert first_exchange.status_code == second_exchange.status_code == 200
            session_headers = {"Authorization": f"Bearer {first_exchange.json()['access_token']}"}

            expected = {scope: (200 if scope == granted_scope else 404) for scope in routes}
            raw_results = {}
            session_results = {}
            for scope, (_, path) in routes.items():
                raw_results[scope] = await rest.get(path, headers=raw_headers)
                session_results[scope] = await rest.get(path, headers=session_headers)
            assert {scope: result.status_code for scope, result in raw_results.items()} == expected
            assert {
                scope: result.status_code for scope, result in session_results.items()
            } == expected

            async with httpx.AsyncClient(
                headers=raw_headers, transport=httpx.ASGITransport(app=public)
            ) as mcp_http:
                transport = streamable_http_client("http://localhost/mcp", http_client=mcp_http)
                async with Client(transport) as sdk:
                    outcomes = {
                        "read": await sdk.call_tool(
                            "get_project_context", {"project_id": project_id}
                        ),
                        "search": await sdk.call_tool(
                            "search_project_context",
                            {"project_id": project_id, "query": "matrix"},
                        ),
                        "export": await sdk.call_tool(
                            "export_project_context", {"project_id": project_id}
                        ),
                    }
            assert {scope: not result.is_error for scope, result in outcomes.items()} == {
                scope: granted_scope == scope for scope in routes
            }


@pytest.mark.asyncio
async def test_publish_db_commit_failure_removes_staged_snapshot_and_retry_is_clean(
    postgres_url, tmp_path, monkeypatch
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    control = create_control_app(settings)
    async with (
        control.router.lifespan_context(control),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control, raise_app_exceptions=False),
            base_url="http://control",
        ) as admin,
    ):
        headers = {"Authorization": "Bearer synthetic-control"}
        original = await admin.post(
            "/control/v1/snapshots",
            headers=headers,
            json=_snapshot_payload("partial-publish", "rev-1", "committed body"),
        )
        assert original.status_code == 201, original.text
        project_id = original.json()["project_id"]
        original_snapshot = original.json()["id"]
        retry_payload = {
            **_snapshot_payload("partial-publish", "rev-2", "must rollback"),
            "operation_id": str(uuid4()),
        }

        async def fail_commit(_self: AsyncSession) -> None:
            raise RuntimeError("synthetic post-rename database commit failure")

        monkeypatch.setattr(AsyncSession, "commit", fail_commit)
        failed = await admin.post("/control/v1/snapshots", headers=headers, json=retry_payload)
        monkeypatch.undo()
        assert failed.status_code == 500

        async with control.state.session_factory() as db:
            project = await db.get(Project, project_id)
            assert project is not None and str(project.latest_snapshot_id) == original_snapshot
            rows = list(await db.scalars(select(Snapshot).where(Snapshot.project_id == project_id)))
            assert [str(row.id) for row in rows] == [original_snapshot]
        project_dir = tmp_path / "snapshots" / project_id
        assert sorted(path.name for path in project_dir.iterdir()) == [original_snapshot]

        retried = await admin.post("/control/v1/snapshots", headers=headers, json=retry_payload)
        assert retried.status_code == 201, retried.text
        assert retried.json()["id"] != original_snapshot
        assert retried.json()["version"] == 2
