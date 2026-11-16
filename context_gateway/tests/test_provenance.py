import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client

from context_gateway.apps import (
    SnapshotDocumentInput,
    SnapshotPublishRequest,
    create_control_app,
    create_public_app,
)
from context_gateway.config import Settings
from context_gateway.models import Project, Snapshot
from context_gateway.publishing import publish_snapshot


def _publish_payload(**overrides):
    payload = {
        "project_slug": "synthetic-provenance",
        "project_name": "Synthetic Provenance",
        "source_refs": ["source:overview"],
        "source_revision": "known-source-revision-7",
        "generated_at": datetime(2026, 11, 16, tzinfo=UTC),
        "documents": [
            {
                "id": "overview",
                "title": "Overview",
                "content": "Synthetic only",
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_verified_at_can_be_unknown_or_explicitly_null() -> None:
    omitted = SnapshotPublishRequest.model_validate(_publish_payload())
    explicit_null = SnapshotPublishRequest.model_validate(_publish_payload(verified_at=None))

    assert omitted.verified_at is None
    assert explicit_null.verified_at is None


def test_document_source_refs_are_accepted_and_retained() -> None:
    generated_at = datetime(2026, 11, 15, tzinfo=UTC)
    verified_at = datetime(2026, 11, 16, tzinfo=UTC)
    document = SnapshotDocumentInput.model_validate(
        {
            "id": "overview",
            "title": "Overview",
            "content": "Synthetic only",
            "source_refs": ["note:synthetic-overview", "file-ref:fixture-2"],
            "source_revision": "doc-revision-3",
            "generated_at": generated_at,
            "verified_at": verified_at,
        }
    )

    dumped = document.model_dump()
    assert dumped["source_refs"] == ["note:synthetic-overview", "file-ref:fixture-2"]
    assert dumped["source_revision"] == "doc-revision-3"
    assert dumped["generated_at"] == generated_at
    assert dumped["verified_at"] == verified_at


def test_omitted_and_null_document_provenance_both_mean_unknown() -> None:
    common = {"id": "unknown", "title": "Unknown", "content": "Synthetic only"}
    omitted = SnapshotDocumentInput.model_validate(common).model_dump()
    explicit_null = SnapshotDocumentInput.model_validate(
        {
            **common,
            "source_revision": None,
            "generated_at": None,
            "verified_at": None,
        }
    ).model_dump()

    assert omitted["source_revision"] is omitted["generated_at"] is omitted["verified_at"] is None
    assert explicit_null == omitted


def test_snapshot_verified_at_database_column_is_nullable() -> None:
    assert Snapshot.__table__.c.verified_at.nullable is True


class PublishingSession:
    def __init__(self) -> None:
        self.project = Project(
            id=uuid4(),
            slug="synthetic-provenance",
            name="Synthetic Provenance",
            description="",
        )

    async def scalar(self, _statement):
        if "count(" in str(_statement).lower():
            return 0
        return self.project

    def add(self, _item) -> None:
        pass

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


async def test_publish_manifest_keeps_document_sources_and_unknown_verification(
    tmp_path: Path,
) -> None:
    session = PublishingSession()
    generated_at = datetime(2026, 11, 16, tzinfo=UTC)
    payload = {
        "project_slug": "synthetic-provenance",
        "project_name": "Synthetic Provenance",
        "source_refs": ["source:overview", "source:appendix"],
        "source_revision": "known-source-revision-7",
        "generated_at": generated_at,
        "verified_at": None,
        "documents": [
            {
                "id": "overview",
                "title": "Overview",
                "content": "Synthetic only",
                "source_refs": ["note:synthetic-overview"],
                "source_revision": "overview-rev-4",
                "generated_at": datetime(2026, 11, 15, tzinfo=UTC),
                "verified_at": datetime(2026, 11, 15, 12, tzinfo=UTC),
            }
        ],
    }

    snapshot = await publish_snapshot(session, tmp_path, payload)
    manifest = json.loads((Path(snapshot.storage_path) / "manifest.json").read_text())

    assert manifest["schema_version"] == 1
    assert manifest["snapshot_id"] == str(snapshot.id)
    assert manifest["project_id"] == str(snapshot.project_id)
    assert manifest["version"] == 1
    assert manifest["created_at"] == snapshot.published_at.isoformat()
    assert manifest["status"] == "published"
    assert snapshot.source_revision == "known-source-revision-7"
    assert manifest["verified_at"] is None
    assert manifest["source_revision"] == "known-source-revision-7"
    assert manifest["documents"][0]["path"] == "documents/overview.md"
    assert manifest["documents"][0]["source_refs"] == ["note:synthetic-overview"]
    assert manifest["documents"][0]["source_revision"] == "overview-rev-4"
    assert manifest["documents"][0]["generated_at"] == "2026-11-15T00:00:00+00:00"
    assert manifest["documents"][0]["verified_at"] == "2026-11-15T12:00:00+00:00"
    assert manifest["documents"][0]["sha256"] == hashlib.sha256(b"Synthetic only").hexdigest()


@pytest.mark.asyncio
async def test_document_provenance_survives_rest_context_document_export_and_mcp(
    postgres_url: str, tmp_path: Path
) -> None:
    settings = Settings(postgres_url, tmp_path / "snapshots", "synthetic-provenance-token", 200)
    control = create_control_app(settings)
    public = create_public_app(settings)
    expected_docs = [
        {
            "id": "verified",
            "title": "Verified",
            "content": "Synthetic A",
            "content_type": "text/markdown",
            "source_refs": ["note:doc-a"],
            "source_revision": "doc-a-rev-11",
            "generated_at": "2026-11-14T00:00:00+00:00",
            "verified_at": "2026-11-15T00:00:00+00:00",
        },
        {
            "id": "unverified",
            "title": "Unverified",
            "content": "Synthetic B",
            "content_type": "text/markdown",
            "source_refs": ["note:doc-b"],
            "source_revision": "doc-b-rev-22",
            "generated_at": "2026-11-16T00:00:00+00:00",
            "verified_at": None,
        },
    ]
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
            headers={"Authorization": "Bearer synthetic-provenance-token"},
            json={
                "project_slug": f"document-provenance-{uuid4().hex[:8]}",
                "project_name": "Synthetic Document Provenance",
                "source_refs": ["fixture://snapshot"],
                "source_revision": "snapshot-rev-1",
                "generated_at": "2026-11-16T00:01:00Z",
                "verified_at": None,
                "documents": expected_docs,
            },
        )
        assert published.status_code == 201, published.text
        project_id = published.json()["project_id"]
        issued = await admin.post(
            "/control/v1/keys",
            headers={"Authorization": "Bearer synthetic-provenance-token"},
            json={
                "label": "synthetic provenance",
                "projects": {project_id: {"scopes": ["read", "export"]}},
            },
        )
        assert issued.status_code == 201, issued.text
        exchanged = await rest.post(
            "/api/v1/auth/exchange",
            headers={"Authorization": f"Bearer {issued.json()['key']}"},
        )
        assert exchanged.status_code == 200, exchanged.text
        headers = {"Authorization": f"Bearer {exchanged.json()['access_token']}"}
        context = await rest.get(f"/api/v1/projects/{project_id}/context", headers=headers)
        assert context.status_code == 200, context.text
        context_docs = context.json()["documents"]
        assert [
            {key: document[key] for key in expected_docs[0]} for document in context_docs
        ] == expected_docs

        document_response = await rest.get(
            f"/api/v1/projects/{project_id}/documents/verified", headers=headers
        )
        assert document_response.status_code == 200, document_response.text
        assert {key: document_response.json()[key] for key in expected_docs[0]} == expected_docs[0]

        exported = await rest.get(
            f"/api/v1/projects/{project_id}/export",
            headers=headers,
            params={"format": "json"},
        )
        assert exported.status_code == 200, exported.text
        assert [
            {key: document[key] for key in expected_docs[0]}
            for document in exported.json()["documents"]
        ] == expected_docs

        sdk_http = httpx.AsyncClient(
            headers=headers,
            transport=httpx.ASGITransport(app=public),
        )
        async with sdk_http:
            transport = streamable_http_client("http://localhost/mcp", http_client=sdk_http)
            async with Client(transport) as sdk:
                mcp_context = await sdk.call_tool("get_project_context", {"project_id": project_id})
        assert not mcp_context.is_error
        assert [
            {key: document[key] for key in expected_docs[0]}
            for document in mcp_context.structured_content["documents"]
        ] == expected_docs
