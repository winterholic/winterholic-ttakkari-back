"""Collector Platform: content-addressed raw byte store (CP-S2-06).

Raw responses are preserved exactly as received. The handle is `cblob:sha256:<hex>`; identical bytes are stored once
and each occurrence keeps its own provenance row. The handle is not a capability: reading goes through the item row
that references it, so tenant/scope access is checked against the DB, not the file system. The operator location of a
shared blob store is still an open decision with the artifact/kb owners (architecture 2); this port keeps that swap
local to one class.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Protocol

from ttakkari.config import get_settings

HANDLE_PREFIX = "cblob:sha256:"
_SHA = re.compile(r"^[0-9a-f]{64}$")
log = logging.getLogger(__name__)


class BlobQuotaExceeded(OSError):
    def __init__(self, used_bytes: int, requested_bytes: int, cap_bytes: int) -> None:
        self.used_bytes = used_bytes
        self.requested_bytes = requested_bytes
        self.cap_bytes = cap_bytes
        super().__init__(
            f"collector blob quota exceeded: used={used_bytes} requested={requested_bytes} cap={cap_bytes} bytes"
        )


class BlobStore(Protocol):
    def put(self, data: bytes, *, lineage: dict[str, str] | None = None) -> str: ...

    def get(self, handle: str) -> bytes: ...

    def usage_bytes(self) -> int: ...


def handle_for(sha256: str) -> str:
    return HANDLE_PREFIX + sha256


def sha_of_handle(handle: str) -> str:
    if not handle.startswith(HANDLE_PREFIX) or not _SHA.match(handle[len(HANDLE_PREFIX):]):
        raise ValueError(f"not a collector blob handle: {handle!r}")
    return handle[len(HANDLE_PREFIX):]


class FileBlobStore:
    def __init__(self, root: Path, *, cap_bytes: int | None = None) -> None:
        self.root = root
        self.cap_bytes = cap_bytes

    def _path(self, sha: str) -> Path:
        return self.root / sha[:2] / sha[2:4] / sha

    def put(self, data: bytes, *, lineage: dict[str, str] | None = None) -> str:
        sha = hashlib.sha256(data).hexdigest()
        path = self._path(sha)
        self._assert_root_chain()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_real_directory(self.root)
        # flock is process-scoped through the kernel: a dead writer releases the quota gate.
        lock_path = self.root / ".quota.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            shard = path.parent.parent
            shard.mkdir(mode=0o700, exist_ok=True)
            self._assert_real_directory(shard)
            path.parent.mkdir(mode=0o700, exist_ok=True)
            self._assert_real_directory(path.parent)
            self._reconcile_stages()
            cap = self.cap_bytes
            if cap is None:
                cap = get_settings().collector_blob_cap_bytes
            if cap < 0:
                raise ValueError("collector blob cap must be non-negative")
            try:
                path.lstat()
            except FileNotFoundError:
                existing_valid = False
            else:
                if not path.is_file() or path.is_symlink():
                    raise OSError("collector blob path is not a regular owned file")
                existing_valid = self._matches(path, sha)
            if existing_valid:
                if lineage is not None:
                    self._write_lineage(sha, lineage)
                return handle_for(sha)
            used = self._usage_bytes_unlocked()
            # A repair must coexist with the existing (possibly corrupt) raw file until atomic replace.
            # Count the durable stage reservation as well as every current raw/stage byte.
            projected = used + len(data)
            if projected > cap:
                log.warning(
                    "collector blob quota denied sha256=%s used_bytes=%d requested_bytes=%d cap_bytes=%d",
                    sha, used, len(data), cap,
                )
                raise BlobQuotaExceeded(used, len(data), cap)

            if lineage is not None:
                # Durable provenance precedes the raw object, so a process death after put()
                # but before the DB page commit leaves enough information for replay/reconciliation.
                self._write_lineage(sha, lineage)
            stage = path.parent / f".stage-{sha}"
            fd = os.open(stage, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if not self._matches(stage, sha):
                raise OSError("collector blob staging hash mismatch")
            os.chmod(stage, 0o600)
            os.replace(stage, path)  # same-filesystem atomic repair; readers see old or complete new bytes
            dir_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)
        return handle_for(sha)

    def lineage_records(self) -> list[dict[str, str]]:
        """Read the durable per-item provenance index; callers still enforce tenant/scope authorization."""
        self._assert_root_chain()
        directory = self.root / ".lineage"
        if not directory.exists():
            return []
        self._assert_real_directory(directory)
        rows: list[dict[str, str]] = []
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not path.is_file():
                raise OSError(f"invalid collector lineage record: {path}")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
                raise OSError(f"invalid collector lineage record: {path}")
            rows.append(value)
        return rows

    def _write_lineage(self, sha: str, lineage: dict[str, str]) -> None:
        item_id = lineage.get("item_id")
        if not item_id or not re.fullmatch(r"[0-9a-fA-F-]{36}", item_id):
            raise ValueError("collector lineage requires a UUID item_id")
        directory = self.root / ".lineage"
        directory.mkdir(mode=0o700, exist_ok=True)
        self._assert_real_directory(directory)
        path = directory / f"{item_id}-{sha}.json"
        payload = {**lineage, "blob_sha256": sha}
        if path.is_symlink():
            raise OSError(f"collector lineage path is a symlink: {path}")
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            if current != payload:
                raise OSError(f"collector lineage item/hash collision: {item_id}/{sha}")
            return
        fd, temp_name = tempfile.mkstemp(dir=directory, prefix=f".{item_id}-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                f.flush()
                os.fsync(f.fileno())
            os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

    def usage_bytes(self) -> int:
        """Physical regular-file bytes under the collector-owned root, including crash-left stages/orphans."""
        self._assert_root_chain()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._assert_real_directory(self.root)
        lock_path = self.root / ".quota.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            self._reconcile_stages()
            return self._usage_bytes_unlocked()
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

    @staticmethod
    def _assert_real_directory(path: Path) -> None:
        if path.is_symlink() or not path.is_dir():
            raise OSError(f"collector blob directory is not a real directory: {path}")

    def _assert_root_chain(self) -> None:
        current = Path(self.root.absolute().anchor)
        for part in self.root.absolute().parts[1:]:
            current /= part
            if current.is_symlink():
                raise OSError(f"symlink in collector blob root path: {current}")

    @staticmethod
    def _matches(path: Path, sha: str) -> bool:
        digest = hashlib.sha256()
        try:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    digest.update(chunk)
        except FileNotFoundError:
            return False
        return digest.hexdigest() == sha

    def _reconcile_stages(self) -> None:
        for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
            base = Path(directory)
            for name in list(dirnames):
                child = base / name
                if child.is_symlink():
                    raise OSError(f"symlink in collector blob root: {child}")
            for name in filenames:
                if not name.startswith(".stage-"):
                    continue
                sha = name[len(".stage-"):]
                stage = base / name
                if not _SHA.fullmatch(sha) or stage.is_symlink() or not stage.is_file():
                    raise OSError(f"invalid staged collector blob: {stage}")
                expected = self._path(sha)
                if base != expected.parent:
                    raise OSError(f"staged collector blob is outside its content shard: {stage}")
                if not self._matches(stage, sha):
                    # An interrupted partial write is not a complete raw object and can be fetched again.
                    stage.unlink()
                    continue
                if expected.is_symlink():
                    raise OSError(f"collector blob path is a symlink: {expected}")
                if expected.exists() and self._matches(expected, sha):
                    stage.unlink()
                else:
                    os.replace(stage, expected)

    def _usage_bytes_unlocked(self) -> int:
        total = 0
        for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
            base = Path(directory)
            for name in dirnames:
                if (base / name).is_symlink():
                    raise OSError(f"symlink in collector blob root: {base / name}")
            dirnames[:] = [name for name in dirnames if not (base == self.root and name == ".lineage")]
            for name in filenames:
                path = base / name
                if path == self.root / ".quota.lock":
                    continue
                info = path.lstat()
                if not path.is_file() or path.is_symlink():
                    raise OSError(f"non-regular entry in collector blob root: {path}")
                total += info.st_size
        return total

    def get(self, handle: str) -> bytes:
        sha = sha_of_handle(handle)
        path = self._path(sha)
        self._assert_root_chain()
        self._assert_real_directory(self.root)
        self._assert_real_directory(path.parent.parent)
        self._assert_real_directory(path.parent)
        if path.is_symlink() or not path.is_file():
            raise OSError(f"collector blob path is not a regular owned file: {path}")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise OSError(f"blob {sha} is corrupt on disk")
        return data


def default_blob_store() -> FileBlobStore:
    return FileBlobStore(get_settings().home / "collector" / "blobs")
