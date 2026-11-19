from __future__ import annotations

import httpx
import pytest

from context_gateway.apps import create_control_app
from context_gateway.config import Settings


@pytest.mark.asyncio
async def test_publish_operation_id_returns_same_receipt_and_rejects_changed_payload(
    postgres_url, tmp_path
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-control", 200)
    app = create_control_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client,
    ):
        headers = {"Authorization": "Bearer synthetic-control"}
        payload = {
            "operation_id": "5012b248-ed38-4630-8c10-a504e9c4ac14",
            "project_slug": "idempotent-fixture",
            "project_name": "Idempotent Fixture",
            "source_refs": ["fixture://idempotent"],
            "source_revision": "rev-1",
            "generated_at": "2026-11-19T00:00:00Z",
            "documents": [{"id": "doc", "title": "Doc", "content": "one"}],
        }
        first = await client.post("/control/v1/snapshots", headers=headers, json=payload)
        retry = await client.post("/control/v1/snapshots", headers=headers, json=payload)
        revoked = await client.post(
            f"/control/v1/snapshots/{first.json()['id']}/revoke", headers=headers
        )
        retry_after_revoke = await client.post(
            "/control/v1/snapshots", headers=headers, json=payload
        )
        changed = await client.post(
            "/control/v1/snapshots",
            headers=headers,
            json={**payload, "source_revision": "rev-2"},
        )

    assert first.status_code == 201, first.text
    assert retry.status_code == 201 and retry.json() == first.json()
    assert revoked.status_code == 200
    assert retry_after_revoke.status_code == 201 and retry_after_revoke.json() == first.json()
    assert changed.status_code == 409
