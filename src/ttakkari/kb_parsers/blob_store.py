"""Content-addressed original raw blob storage with SHA-256 verification and size limits.

Every filesystem touch below the store root goes through a directory file descriptor
(``dir_fd``) and opens each path component with ``O_NOFOLLOW | O_DIRECTORY``. A path is
therefore never checked and used as two separate steps, so replacing a directory with a
symlink between "check" and "use" cannot redirect a write outside the store: the process
keeps operating on the directory object it already opened.

Platform note: this needs ``os.supports_dir_fd`` for open/mkdir/rename/unlink/stat plus
``O_DIRECTORY`` and ``O_NOFOLLOW`` (all present on Linux and macOS). Where they are missing
(e.g. Windows) ``BlobStore`` fails closed with ``NotImplementedError`` instead of falling
back to the racy path based implementation.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import secrets
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

_DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_WRITE_FLAGS = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
_MAX_SYMLINK_RETRIES = 4


def _require_dir_fd_support() -> None:
    needed = (os.open, os.mkdir, os.rename, os.unlink, os.stat)
    missing = [f.__name__ for f in needed if f not in os.supports_dir_fd]
    for flag in ("O_DIRECTORY", "O_NOFOLLOW"):
        if not hasattr(os, flag):
            missing.append(flag)
    if missing:
        raise NotImplementedError(
            "BlobStore requires dir_fd based filesystem access; unsupported on this "
            f"platform: {', '.join(missing)}"
        )


class BlobStore:
    """Content-addressed filesystem blob store for immutable original files with symlink defense."""

    def __init__(self, root_dir: Path | str, max_bytes: int = 250 * 1024 * 1024):
        _require_dir_fd_support()
        self.root_dir = Path(root_dir).resolve()
        self.max_bytes = max_bytes
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def relpath_for(self, sha256_hash: str) -> Path:
        """Compute relative path within store: sha256/ab/cd/abcdef...blob"""
        digest = sha256_hash.lower()
        if len(digest) != 64 or not all(c in "0123456789abcdef" for c in digest):
            raise ValueError(f"Invalid sha256 digest: {sha256_hash}")
        return Path("sha256") / digest[:2] / digest[2:4] / f"{digest}.blob"

    def path_for(self, sha256_hash: str) -> Path:
        """Informational absolute path for a hash. Pure computation: never touches the filesystem.

        All store I/O goes through directory fds, so this path must not be used to open the blob
        outside this class (a path string can be redirected by a symlink after it was computed).
        """
        return self.root_dir / self.relpath_for(sha256_hash)

    # ------------------------------------------------------------------ fd helpers

    @contextlib.contextmanager
    def _open_root(self) -> Iterator[int]:
        fd = os.open(self.root_dir, _DIR_FLAGS)
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def _is_symlink_at(name: str, parent_fd: int) -> bool:
        try:
            st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        return stat.S_ISLNK(st.st_mode)

    def _open_subdir(self, parent_fd: int, name: str, *, create: bool) -> int | None:
        """Open ``name`` below ``parent_fd`` without following symlinks.

        create=False: return None when absent or when the entry is a symlink (never mutates).
        create=True: a symlink planted inside the managed store is unlinked (the link itself,
        via dir_fd) and replaced by a real directory; re-planting during the retry window is
        bounded and ends in an error, never in an out-of-store write.
        """
        for _ in range(_MAX_SYMLINK_RETRIES):
            try:
                return os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
            except FileNotFoundError:
                if not create:
                    return None
                try:
                    os.mkdir(name, 0o700, dir_fd=parent_fd)
                except FileExistsError:
                    pass
            except OSError as exc:
                if exc.errno not in (errno.ELOOP, errno.ENOTDIR):
                    raise
                if not self._is_symlink_at(name, parent_fd):
                    if create:
                        raise ValueError(
                            f"Store path component {name!r} is not a directory"
                        ) from exc
                    return None
                if not create:
                    return None
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name, dir_fd=parent_fd)
        raise ValueError(
            f"Store path component {name!r} kept being replaced by a symlink"
        )

    @contextlib.contextmanager
    def _open_leaf_dir(
        self, root_fd: int, rel: Path, *, create: bool
    ) -> Iterator[int | None]:
        """Walk ``rel.parent`` below ``root_fd`` one component at a time; yield the leaf dir fd."""
        opened: list[int] = []
        try:
            cur = root_fd
            for part in rel.parts[:-1]:
                nxt = self._open_subdir(cur, part, create=create)
                if nxt is None:
                    yield None
                    return
                opened.append(nxt)
                cur = nxt
            yield cur
        finally:
            for fd in reversed(opened):
                os.close(fd)

    # ------------------------------------------------------------------ public API

    def exists(self, sha256_hash: str) -> bool:
        rel = self.relpath_for(sha256_hash)
        with (
            self._open_root() as root_fd,
            self._open_leaf_dir(root_fd, rel, create=False) as leaf_fd,
        ):
            if leaf_fd is None:
                return False
            try:
                st = os.stat(rel.name, dir_fd=leaf_fd, follow_symlinks=False)
            except OSError:
                return False
            return stat.S_ISREG(st.st_mode)

    def put(self, data: bytes | BinaryIO) -> tuple[str, int, Path]:
        """Store bytes or stream into content-addressed location with atomic rename via dir_fds."""
        hasher = hashlib.sha256()
        total_size = 0
        tmp_name = f".tmp-{secrets.token_hex(16)}"

        with self._open_root() as root_fd:
            tmp_fd = os.open(tmp_name, _WRITE_FLAGS, 0o600, dir_fd=root_fd)
            try:
                with os.fdopen(tmp_fd, "wb") as tmp:
                    if isinstance(data, bytes):
                        total_size = len(data)
                        if total_size > self.max_bytes:
                            raise ValueError(
                                f"Blob size {total_size} exceeds maximum {self.max_bytes}"
                            )
                        hasher.update(data)
                        tmp.write(data)
                    else:
                        while True:
                            chunk = data.read(64 * 1024)
                            if not chunk:
                                break
                            total_size += len(chunk)
                            if total_size > self.max_bytes:
                                raise ValueError(
                                    f"Blob size {total_size} exceeds maximum {self.max_bytes}"
                                )
                            hasher.update(chunk)
                            tmp.write(chunk)
                    tmp.flush()
                    os.fsync(tmp.fileno())

                digest = hasher.hexdigest()
                rel = self.relpath_for(digest)
                with self._open_leaf_dir(root_fd, rel, create=True) as leaf_fd:
                    assert leaf_fd is not None
                    try:
                        st = os.stat(rel.name, dir_fd=leaf_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        st = None
                    if st is not None:
                        if not stat.S_ISREG(st.st_mode):
                            if stat.S_ISLNK(st.st_mode):
                                raise ValueError(
                                    "Destination path cannot be a symbolic link"
                                )
                            raise ValueError("Destination path is not a regular file")
                        # Already stored (content-addressed, idempotent)
                        os.unlink(tmp_name, dir_fd=root_fd)
                    else:
                        # Both ends are pinned directory fds; nothing is re-resolved by path.
                        os.rename(
                            tmp_name,
                            rel.name,
                            src_dir_fd=root_fd,
                            dst_dir_fd=leaf_fd,
                        )
                        with contextlib.suppress(OSError):
                            os.fsync(leaf_fd)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_name, dir_fd=root_fd)
                raise

        return digest, total_size, self.path_for(digest)

    def _open_blob(self, sha256_hash: str) -> BinaryIO | None:
        rel = self.relpath_for(sha256_hash)
        with (
            self._open_root() as root_fd,
            self._open_leaf_dir(root_fd, rel, create=False) as leaf_fd,
        ):
            if leaf_fd is None:
                return None
            try:
                fd = os.open(rel.name, _READ_FLAGS, dir_fd=leaf_fd)
            except OSError:
                return None
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                return None
        except OSError:
            os.close(fd)
            return None
        return os.fdopen(fd, "rb")

    def get(self, sha256_hash: str) -> bytes:
        """Retrieve raw bytes of stored blob; every component is opened with O_NOFOLLOW."""
        f = self._open_blob(sha256_hash)
        if f is None:
            raise FileNotFoundError(f"Blob not found for hash {sha256_hash}")
        with f:
            return f.read()

    def verify(self, sha256_hash: str) -> bool:
        """Verify on-disk file content matches its SHA-256 hash using fd based reading."""
        f = self._open_blob(sha256_hash)
        if f is None:
            return False
        hasher = hashlib.sha256()
        try:
            with f:
                while chunk := f.read(64 * 1024):
                    hasher.update(chunk)
        except OSError:
            return False
        return hasher.hexdigest() == sha256_hash.lower()
