import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from context_gateway.apps import SnapshotDocumentInput, SnapshotPublishRequest
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
    document = SnapshotDocumentInput.model_validate(
        {
            "id": "overview",
            "title": "Overview",
            "content": "Synthetic only",
            "source_refs": ["note:synthetic-overview", "file-ref:fixture-2"],
        }
    )

    assert document.model_dump()["source_refs"] == [
        "note:synthetic-overview",
        "file-ref:fixture-2",
    ]


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
            }
        ],
    }

    snapshot = await publish_snapshot(session, tmp_path, payload)
    manifest = json.loads((Path(snapshot.storage_path) / "manifest.json").read_text())

    assert snapshot.source_revision == "known-source-revision-7"
    assert manifest["verified_at"] is None
    assert manifest["source_revision"] == "known-source-revision-7"
    assert manifest["documents"][0]["source_refs"] == ["note:synthetic-overview"]
