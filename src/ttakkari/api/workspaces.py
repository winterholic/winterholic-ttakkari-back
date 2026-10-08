from __future__ import annotations

import asyncio
import os
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ttakkari.api.deps import Db, broker_http, get_workspace
from ttakkari.audit import record
from ttakkari.broker.policy import (
    BrokerError,
    is_sensitive,
    resolve_in_root,
    validate_workspace_root,
)
from ttakkari.config import get_settings
from ttakkari.models import Workspace, utcnow
from ttakkari.schemas import (
    DirListing,
    FileEntry,
    WorkspaceIn,
    WorkspaceOut,
    WorkspacePatch,
)
from ttakkari.security.auth import User, client_ip

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])
MAX_ENTRIES = 1000
SEARCH_LIMIT = 200
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist", "build", ".cache", ".turbo"}


@router.get("", response_model=list[WorkspaceOut])
async def list_workspaces(_: User, db: Db, include_archived: bool = False) -> list[Workspace]:
    q = select(Workspace).order_by(Workspace.created_at.desc())
    if not include_archived:
        q = q.where(Workspace.archived_at.is_(None))
    return list(await db.scalars(q))


@router.post("", response_model=WorkspaceOut, status_code=201)
async def create_workspace(body: WorkspaceIn, _: User, db: Db, request: Request) -> Workspace:
    try:
        root = validate_workspace_root(body.root_path, get_settings().allowed_roots)
    except BrokerError as e:
        record(db, action="workspace.create", outcome="denied", detail={"path": body.root_path, "code": e.code},
               ip=client_ip(request))
        await db.commit()
        raise broker_http(e) from e
    ws = Workspace(name=body.name, root_path=str(root), is_git=(root / ".git").exists(),
                   default_engine=body.default_engine, codex_sandbox=body.codex_sandbox,
                   export_allowed=body.export_allowed)
    db.add(ws)
    try:
        await db.flush()
    except IntegrityError as e:
        raise HTTPException(status.HTTP_409_CONFLICT, "이미 등록된 경로입니다.") from e
    record(db, action="workspace.create", target_type="workspace", target_id=ws.id, detail={"path": str(root)},
           ip=client_ip(request))
    await db.commit()
    return ws


@router.get("/{ws_id}", response_model=WorkspaceOut)
async def get_ws(ws_id: uuid.UUID, _: User, db: Db) -> Workspace:
    return await get_workspace(db, ws_id)


@router.patch("/{ws_id}", response_model=WorkspaceOut)
async def patch_ws(ws_id: uuid.UUID, body: WorkspacePatch, _: User, db: Db, request: Request) -> Workspace:
    ws = await get_workspace(db, ws_id)
    data = body.model_dump(exclude_unset=True)
    archived = data.pop("archived", None)
    for k, v in data.items():
        setattr(ws, k, v)
    if archived is not None:
        ws.archived_at = utcnow() if archived else None
    ws.is_git = (Path(ws.root_path) / ".git").exists()
    record(db, action="workspace.update", target_type="workspace", target_id=ws.id, detail=body.model_dump(exclude_unset=True),
           ip=client_ip(request))
    await db.commit()
    return ws


def _entry(p: Path, root: Path) -> FileEntry:
    st = p.lstat()
    if stat.S_ISLNK(st.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(st.st_mode):
        kind = "dir"
    elif stat.S_ISREG(st.st_mode):
        kind = "file"
    else:
        kind = "other"
    return FileEntry(
        name=p.name, rel_path=p.relative_to(root).as_posix(), type=kind,
        size=st.st_size if kind == "file" else None,
        modified_at=datetime.fromtimestamp(st.st_mtime, UTC), sensitive=is_sensitive(p),
    )


@router.get("/{ws_id}/files", response_model=DirListing)
async def list_files(ws_id: uuid.UUID, _: User, db: Db, path: str = Query(".", max_length=4096),
                     show_hidden: bool = False) -> DirListing:
    ws = await get_workspace(db, ws_id)
    try:
        r = resolve_in_root(Path(ws.root_path), path)
    except BrokerError as e:
        raise broker_http(e) from e
    if not r.path.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "디렉터리가 아닙니다.")
    entries: list[FileEntry] = []
    truncated = False
    with os.scandir(r.path) as it:
        for de in sorted(it, key=lambda d: (not d.is_dir(follow_symlinks=False), d.name.lower())):
            if not show_hidden and de.name.startswith("."):
                continue
            if len(entries) >= MAX_ENTRIES:
                truncated = True
                break
            entries.append(_entry(Path(de.path), r.root))
    return DirListing(rel_path=r.rel, entries=entries, truncated=truncated)


@router.get("/{ws_id}/search", response_model=list[FileEntry])
async def search_files(ws_id: uuid.UUID, _: User, db: Db, q: str = Query(min_length=1, max_length=200)) -> list[FileEntry]:
    """파일명 부분 일치 검색. 내용 검색·자연어 검색은 에이전트에게 맡긴다."""
    ws = await get_workspace(db, ws_id)
    return await asyncio.to_thread(_search, Path(ws.root_path).resolve(), q.lower())


def _search(root: Path, needle: str) -> list[FileEntry]:
    out: list[FileEntry] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if needle in name.lower():
                out.append(_entry(Path(dirpath) / name, root))
                if len(out) >= SEARCH_LIMIT:
                    return out
    return out
