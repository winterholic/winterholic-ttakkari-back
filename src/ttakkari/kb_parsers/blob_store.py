"""Content-addressed original raw blob storage with SHA-256 verification and size limits."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO


class BlobStore:
    """Content-addressed filesystem blob store for immutable original files."""

    def __init__(self, root_dir: Path | str, max_bytes: int = 250 * 1024 * 1024):
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
        """Absolute path for a given SHA-256 hash."""
        rel = self.relpath_for(sha256_hash)
        full_path = (self.root_dir / rel).resolve()
        # Prevent directory traversal
        if not str(full_path).startswith(str(self.root_dir)):
            raise ValueError("Path traversal attempt detected")
        return full_path

    def exists(self, sha256_hash: str) -> bool:
        return self.path_for(sha256_hash).is_file()

    def put(self, data: bytes | BinaryIO) -> tuple[str, int, Path]:
        """Store bytes or stream into content-addressed location. Returns (sha256, byte_size, path)."""
        hasher = hashlib.sha256()
        total_size = 0

        # Write to temporary file first in the same filesystem for atomic rename
        with tempfile.NamedTemporaryFile(dir=self.root_dir, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            try:
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
            except Exception:
                tmp_path.unlink(missing_ok=True)
                raise

        digest = hasher.hexdigest()
        dest_path = self.path_for(digest)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        if dest_path.is_file():
            # Already exists (idempotent), clean up temp
            tmp_path.unlink(missing_ok=True)
        else:
            shutil.move(tmp_path, dest_path)

        return digest, total_size, dest_path

    def get(self, sha256_hash: str) -> bytes:
        """Retrieve raw bytes of stored blob."""
        path = self.path_for(sha256_hash)
        if not path.is_file():
            raise FileNotFoundError(f"Blob not found for hash {sha256_hash}")
        return path.read_bytes()

    def verify(self, sha256_hash: str) -> bool:
        """Verify on-disk file content matches its SHA-256 hash."""
        path = self.path_for(sha256_hash)
        if not path.is_file():
            return False
        hasher = hashlib.sha256()
        with path.open("rb") as f:
            while chunk := f.read(64 * 1024):
                hasher.update(chunk)
        return hasher.hexdigest().lower() == sha256_hash.lower()
