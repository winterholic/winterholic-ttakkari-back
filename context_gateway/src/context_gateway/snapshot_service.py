from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Project, Snapshot
from .publishing import IntegrityError, canonical_json


async def authorized_project(session: AsyncSession, project_id: UUID) -> Project | None:
    return await session.scalar(select(Project).where(Project.id == project_id))


async def get_snapshot(
    session: AsyncSession,
    project: Project,
    snapshot_id: UUID | None = None,
) -> Snapshot | None:
    target_id = snapshot_id or project.latest_snapshot_id
    if target_id is None:
        return None
    return await session.scalar(
        select(Snapshot).where(
            Snapshot.id == target_id,
            Snapshot.project_id == project.id,
            Snapshot.revoked_at.is_(None),
        )
    )


def verify_manifest(snapshot: Snapshot) -> dict[str, Any]:
    manifest_path = Path(snapshot.storage_path) / "manifest.json"
    try:
        disk_bytes = manifest_path.read_bytes()
        manifest = json.loads(disk_bytes)
    except (OSError, json.JSONDecodeError) as exc:
        raise IntegrityError("snapshot manifest unavailable") from exc
    calculated = hashlib.sha256(canonical_json(snapshot.manifest)).hexdigest()
    if (
        calculated != snapshot.manifest_sha256
        or disk_bytes != canonical_json(snapshot.manifest)
        or manifest != snapshot.manifest
    ):
        raise IntegrityError("snapshot manifest hash mismatch")
    return manifest


def read_document(snapshot: Snapshot, document_id: str) -> dict[str, Any] | None:
    manifest = verify_manifest(snapshot)
    meta = next((doc for doc in manifest["documents"] if doc["id"] == document_id), None)
    if meta is None:
        return None
    doc_path = Path(snapshot.storage_path) / "documents" / f"{document_id}.{meta['extension']}"
    try:
        content_bytes = doc_path.read_bytes()
    except OSError as exc:
        raise IntegrityError("snapshot document unavailable") from exc
    if hashlib.sha256(content_bytes).hexdigest() != meta["sha256"]:
        raise IntegrityError(f"document hash mismatch: {document_id}")
    return {**meta, "content": content_bytes.decode("utf-8")}


def read_all_documents(snapshot: Snapshot) -> list[dict[str, Any]]:
    manifest = verify_manifest(snapshot)
    documents: list[dict[str, Any]] = []
    for metadata in manifest["documents"]:
        document = read_document(snapshot, metadata["id"])
        if document is not None:
            documents.append(document)
    return documents
