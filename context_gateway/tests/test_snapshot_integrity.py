from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest

from context_gateway.models import Snapshot
from context_gateway.publishing import IntegrityError, canonical_json
from context_gateway.snapshot_service import read_document, verify_manifest


def _snapshot(tmp_path: Path) -> Snapshot:
    root = tmp_path / "snapshot"
    (root / "documents").mkdir(parents=True)
    content = b"synthetic verified fixture"
    manifest = {
        "snapshot_id": str(uuid4()),
        "project_slug": "integrity-fixture",
        "source_refs": ["fixture://source"],
        "source_revision": "fixture-rev",
        "generated_at": "2026-11-16T00:00:00+00:00",
        "verified_at": "2026-11-16T00:00:00+00:00",
        "documents": [
            {
                "id": "doc",
                "title": "Fixture",
                "content_type": "text/markdown",
                "extension": "md",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }
    (root / "manifest.json").write_bytes(canonical_json(manifest))
    (root / "documents" / "doc.md").write_bytes(content)
    record = Snapshot(
        id=uuid4(),
        project_id=uuid4(),
        source_refs=manifest["source_refs"],
        source_revision=manifest["source_revision"],
        generated_at="2026-11-16T00:00:00+00:00",
        verified_at="2026-11-16T00:00:00+00:00",
        manifest=manifest,
        manifest_sha256=hashlib.sha256(canonical_json(manifest)).hexdigest(),
        storage_path=str(root),
    )
    return record


def test_manifest_and_document_hashes_are_verified_before_read(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    assert verify_manifest(snapshot)["source_revision"] == "fixture-rev"
    assert read_document(snapshot, "doc")["content"] == "synthetic verified fixture"

    (Path(snapshot.storage_path) / "documents" / "doc.md").write_text("tampered", encoding="utf-8")
    with pytest.raises(IntegrityError, match="document hash mismatch"):
        read_document(snapshot, "doc")


def test_manifest_tampering_is_detected(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path)
    (Path(snapshot.storage_path) / "manifest.json").write_text("{}", encoding="utf-8")
    with pytest.raises(IntegrityError, match="manifest hash mismatch"):
        verify_manifest(snapshot)
