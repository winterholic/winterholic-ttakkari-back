from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select

from context_gateway.apps import create_control_app, create_public_app
from context_gateway.config import Settings
from context_gateway.models import APIKey, KeyACL, Snapshot


@pytest.mark.asyncio
async def test_v2_control_issues_reusable_key_bound_to_pinned_snapshot(
    postgres_url, tmp_path
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    app = create_control_app(settings)
    public = create_public_app(settings)
    async with (
        app.router.lifespan_context(app),
        public.router.lifespan_context(public),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
        httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://test") as api,
    ):
        headers = {"Authorization": "Bearer synthetic-control"}
        published = await client.post(
            "/control/v1/snapshots",
            headers=headers,
            json={
                "project_slug": "v2-pinned-fixture",
                "project_name": "V2 Fixture",
                "source_refs": ["fixture://v2"],
                "source_revision": "rev-1",
                "generated_at": "2026-11-19T00:00:00Z",
                "documents": [{"id": "doc", "title": "Doc", "content": "fixture"}],
            },
        )
        assert published.status_code == 201, published.text
        project_id = UUID(published.json()["project_id"])
        snapshot_id = UUID(published.json()["id"])
        next_payload = {
            "project_slug": "v2-pinned-fixture",
            "project_name": "V2 Fixture",
            "source_refs": ["fixture://v2"],
            "source_revision": "rev-2",
            "generated_at": "2026-11-19T00:01:00Z",
            "documents": [{"id": "doc", "title": "Doc", "content": "new fixture"}],
        }
        newest = await client.post("/control/v1/snapshots", headers=headers, json=next_payload)
        assert newest.status_code == 201, newest.text

        issued = await client.post(
            "/control/v2/credentials",
            headers=headers,
            json={
                "credential_type": "reusable_api_v2",
                "label": "fixture agent",
                "projects": {
                    str(project_id): {
                        "scopes": ["read", "search"],
                        "snapshot_policy": "pinned",
                        "snapshot_id": str(snapshot_id),
                    }
                },
            },
        )
        assert issued.status_code == 201, issued.text
        raw_key = issued.json()["key"]
        status = await client.post(
            "/control/v2/credentials/status",
            headers=headers,
            json={"key_ids": [issued.json()["key_id"]]},
        )
        assert status.status_code == 200
        assert status.json()["credentials"][0]["credential_type"] == "reusable_api_v2"
        assert raw_key not in status.text
        exchanged1 = await api.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"}
        )
        exchanged2 = await api.post(
            "/api/v1/auth/exchange", headers={"Authorization": f"Bearer {raw_key}"}
        )
        assert exchanged1.status_code == exchanged2.status_code == 200
        direct = await api.get("/api/v1/projects", headers={"Authorization": f"Bearer {raw_key}"})
        assert direct.status_code == 200 and [item["id"] for item in direct.json()] == [
            str(project_id)
        ]
        pinned = await api.get(
            f"/api/v1/projects/{project_id}/context",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert pinned.status_code == 200 and pinned.json()["snapshot"]["id"] == str(snapshot_id)
        bypass = await api.get(
            f"/api/v1/projects/{project_id}/context",
            headers={"Authorization": f"Bearer {raw_key}"},
            params={"snapshot_id": newest.json()["id"]},
        )
        assert bypass.status_code == 404
        latest_issued = await client.post(
            "/control/v2/credentials",
            headers=headers,
            json={
                "credential_type": "reusable_api_v2",
                "label": "latest agent",
                "ttl_days": 1,
                "projects": {
                    str(project_id): {
                        "scopes": ["read"],
                        "snapshot_policy": "latest",
                    }
                },
            },
        )
        assert latest_issued.status_code == 201, latest_issued.text
        assert latest_issued.json()["ttl_days"] == 1
        assert latest_issued.json()["projects"][str(project_id)]["snapshot_id"] is None
        latest_context = await api.get(
            f"/api/v1/projects/{project_id}/context",
            headers={"Authorization": f"Bearer {latest_issued.json()['key']}"},
        )
        assert latest_context.status_code == 200
        assert latest_context.json()["snapshot"]["id"] == newest.json()["id"]
        old_latest = await api.get(
            f"/api/v1/projects/{project_id}/context",
            headers={"Authorization": f"Bearer {latest_issued.json()['key']}"},
            params={"snapshot_id": str(snapshot_id)},
        )
        assert old_latest.status_code == 404
        used_status = await client.post(
            "/control/v2/credentials/status",
            headers=headers,
            json={"key_ids": [issued.json()["key_id"]]},
        )
        assert used_status.status_code == 200
        assert used_status.json()["credentials"][0]["last_used_at"] is not None
        arbitrary = await api.get(
            "/api/v1/projects/00000000-0000-0000-0000-000000000001/context",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert arbitrary.status_code == 404

    assert issued.status_code == 201, issued.text
    receipt = issued.json()
    assert receipt["credential_type"] == "reusable_api_v2"
    assert receipt["ttl_days"] == 3
    assert receipt["projects"] == {
        str(project_id): {
            "scopes": ["read", "search"],
            "snapshot_policy": "pinned",
            "snapshot_id": str(snapshot_id),
        }
    }
    async with app.state.session_factory() as db:
        key = await db.get(APIKey, UUID(receipt["key_id"]))
        acl = await db.get(KeyACL, (key.id, project_id))
        assert key is not None and receipt["key"] not in key.credential_hash
        assert key.expires_at > datetime.now(UTC)
        assert acl is not None
        assert getattr(acl, "snapshot_id", None) == snapshot_id
        assert await db.scalar(select(Snapshot.id).where(Snapshot.id == snapshot_id)) == snapshot_id
