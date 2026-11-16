from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import APIKey, AuditEvent, KeyACL, Project, Snapshot

DOC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")


class PublishError(ValueError):
    pass


class IntegrityError(PublishError):
    pass


def _remove_snapshot_tree(root: Path) -> None:
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        try:
            path.chmod(0o755 if path.is_dir() else 0o644)
        except OSError:
            pass
    try:
        root.chmod(0o755)
    except OSError:
        pass
    shutil.rmtree(root, ignore_errors=True)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


async def publish_snapshot(
    session: AsyncSession, snapshot_dir: Path, payload: dict[str, Any]
) -> Snapshot:
    slug = payload["project_slug"]
    project = await session.scalar(select(Project).where(Project.slug == slug).with_for_update())
    if project is None:
        project = Project(
            slug=slug,
            name=payload["project_name"],
            description=payload.get("project_description", ""),
        )
        session.add(project)
        await session.flush()
    elif payload["project_name"] != project.name:
        project.name = payload["project_name"]
        project.description = payload.get("project_description", project.description)

    docs = payload["documents"]
    if not docs:
        raise PublishError("a snapshot must contain at least one document")
    ids: set[str] = set()
    manifest_docs: list[dict[str, str]] = []
    contents: dict[str, bytes] = {}
    for document in docs:
        doc_id = document["id"]
        if not DOC_ID.fullmatch(doc_id) or doc_id in {".", ".."}:
            raise PublishError("invalid document id")
        if doc_id in ids:
            raise PublishError("duplicate document id")
        ids.add(doc_id)
        content = document["content"].encode("utf-8")
        digest = hashlib.sha256(content).hexdigest()
        expected = document.get("sha256")
        if expected is not None and not hmac_compare(digest, expected):
            raise IntegrityError(f"document hash mismatch: {doc_id}")
        title = document["title"]
        content_type = document.get("content_type", "text/markdown")
        extension = {"text/markdown": "md", "text/plain": "txt", "application/json": "json"}[
            content_type
        ]
        manifest_docs.append(
            {
                "id": doc_id,
                "title": title,
                "content_type": content_type,
                "extension": extension,
                "sha256": digest,
            }
        )
        contents[doc_id] = content

    snapshot_id = uuid4()
    generated_at = payload["generated_at"]
    verified_at = payload["verified_at"]
    manifest = {
        "snapshot_id": str(snapshot_id),
        "project_slug": slug,
        "source_refs": payload["source_refs"],
        "source_revision": payload["source_revision"],
        "generated_at": generated_at.isoformat(),
        "verified_at": verified_at.isoformat(),
        "documents": manifest_docs,
    }
    manifest_hash = hashlib.sha256(canonical_json(manifest)).hexdigest()
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    final_parent = snapshot_dir / str(project.id)
    final_parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=final_parent))
    final = final_parent / str(snapshot_id)
    moved = False
    try:
        (stage / "documents").mkdir()
        extensions = {item["id"]: item["extension"] for item in manifest_docs}
        for doc_id, content in contents.items():
            doc_path = stage / "documents" / f"{doc_id}.{extensions[doc_id]}"
            doc_path.write_bytes(content)
            doc_path.chmod(0o444)
        (stage / "manifest.json").write_bytes(canonical_json(manifest))
        (stage / "manifest.json").chmod(0o444)
        (stage / "documents").chmod(0o555)
        stage.chmod(0o555)
        os.replace(stage, final)
        moved = True
        final.chmod(0o555)
        snapshot = Snapshot(
            id=snapshot_id,
            project_id=project.id,
            source_refs=payload["source_refs"],
            source_revision=payload["source_revision"],
            generated_at=generated_at,
            verified_at=verified_at,
            published_at=datetime.now(UTC),
            manifest=manifest,
            manifest_sha256=manifest_hash,
            storage_path=str(final),
        )
        session.add(snapshot)
        project.latest_snapshot_id = snapshot_id
        session.add(
            AuditEvent(event="snapshot_published", project_id=project.id, detail=str(snapshot_id))
        )
        await session.commit()
        return snapshot
    except Exception:
        if moved:
            _remove_snapshot_tree(final)
        else:
            _remove_snapshot_tree(stage)
        await session.rollback()
        raise


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode("ascii"), right.encode("ascii"))


async def revoke_snapshot(session: AsyncSession, snapshot_id: UUID) -> bool:
    snapshot = await session.scalar(
        select(Snapshot).where(Snapshot.id == snapshot_id).with_for_update()
    )
    if snapshot is None:
        return False
    now = datetime.now(UTC)
    snapshot.revoked_at = snapshot.revoked_at or now
    project = await session.get(Project, snapshot.project_id, with_for_update=True)
    if project and project.latest_snapshot_id == snapshot.id:
        replacement = await session.scalar(
            select(Snapshot)
            .where(
                Snapshot.project_id == project.id,
                Snapshot.revoked_at.is_(None),
                Snapshot.id != snapshot.id,
            )
            .order_by(Snapshot.published_at.desc())
            .limit(1)
        )
        project.latest_snapshot_id = replacement.id if replacement else None
    session.add(
        AuditEvent(
            event="snapshot_revoked", project_id=snapshot.project_id, detail=str(snapshot.id)
        )
    )
    await session.commit()
    return True


async def issue_api_key(
    session: AsyncSession,
    *,
    label: str,
    project_scopes: dict[UUID, list[str]],
) -> tuple[APIKey, str]:
    from .auth import issue_key as mint_key

    projects = set(
        (await session.scalars(select(Project.id).where(Project.id.in_(project_scopes)))).all()
    )
    if projects != set(project_scopes):
        raise PublishError("one or more projects do not exist")
    raw, digest, expires_at = mint_key()
    record = APIKey(label=label, credential_hash=digest, expires_at=expires_at)
    session.add(record)
    await session.flush()
    for project_id, scopes in project_scopes.items():
        normalized = sorted(set(scopes))
        if not normalized or not set(normalized) <= {"read", "search", "export"}:
            raise PublishError("invalid scopes")
        session.add(KeyACL(key_id=record.id, project_id=project_id, scopes=normalized))
    session.add(AuditEvent(key_id=record.id, event="key_issued", detail=label))
    await session.commit()
    return record, raw


async def revoke_api_key(session: AsyncSession, key_id: UUID) -> bool:
    key = await session.scalar(select(APIKey).where(APIKey.id == key_id).with_for_update())
    if key is None:
        return False
    key.revoked_at = key.revoked_at or datetime.now(UTC)
    session.add(AuditEvent(key_id=key.id, event="key_revoked"))
    await session.commit()
    return True
