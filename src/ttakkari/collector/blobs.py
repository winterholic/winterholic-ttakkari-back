"""Collector Platform: content-addressed raw byte store (CP-S2-06).

Raw responses are preserved exactly as received. The handle is `cblob:sha256:<hex>`; identical bytes are stored once
and each occurrence keeps its own provenance row. The handle is not a capability: reading goes through the item row
that references it, so tenant/scope access is checked against the DB, not the file system. The operator location of a
shared blob store is still an open decision with the artifact/kb owners (architecture 2); this port keeps that swap
local to one class.
"""
from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from ttakkari.config import get_settings

HANDLE_PREFIX = "cblob:sha256:"
_SHA = re.compile(r"^[0-9a-f]{64}$")


class BlobStore(Protocol):
    def put(self, data: bytes) -> str: ...

    def get(self, handle: str) -> bytes: ...


def handle_for(sha256: str) -> str:
    return HANDLE_PREFIX + sha256


def sha_of_handle(handle: str) -> str:
    if not handle.startswith(HANDLE_PREFIX) or not _SHA.match(handle[len(HANDLE_PREFIX):]):
        raise ValueError(f"not a collector blob handle: {handle!r}")
    return handle[len(HANDLE_PREFIX):]


class FileBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha[2:4] / sha

    def put(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        if path.exists() and path.stat().st_size == len(data):
            return handle_for(sha)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)  # atomic: a reader never sees a partial blob
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return handle_for(sha)

    def get(self, handle: str) -> bytes:
        sha = sha_of_handle(handle)
        data = self._path(sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise OSError(f"blob {sha} is corrupt on disk")
        return data


def default_blob_store() -> FileBlobStore:
    return FileBlobStore(get_settings().home / "collector" / "blobs")
