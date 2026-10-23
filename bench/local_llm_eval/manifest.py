"""File manifests, tree hashes and diffs (spec sections 1-7 and 5.1)."""

import hashlib
import os
import stat
from pathlib import Path

IGNORED_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_of_files(files: dict[str, str]) -> dict[str, str]:
    return {p: sha256_bytes(c.encode("utf-8")) for p, c in sorted(files.items())}


def tree_hash(manifest: dict[str, str]) -> str:
    body = "".join(f"{p}\0{h}\n" for p, h in sorted(manifest.items()))
    return sha256_bytes(body.encode("utf-8"))


def _ignored(rel: Path) -> bool:
    return any(part in IGNORED_DIRS for part in rel.parts) or rel.suffix == ".pyc"


NON_REGULAR_PREFIXES = ("symlink:", "special:", "hardlink:")


def _entry_digest(path: Path) -> str | None:
    """Digest of one directory entry, or a marker for anything that is not a plain single-link file.

    Nothing is silently skipped: a symlink, FIFO, socket, device or hard-linked file changes the manifest,
    so a start tree is a base mismatch and an end tree is a forbidden write. Special files are never opened.
    """
    info = path.lstat()
    mode = info.st_mode
    if stat.S_ISLNK(mode):
        return "symlink:" + os.readlink(path)
    if stat.S_ISDIR(mode):
        return None
    if not stat.S_ISREG(mode):
        return f"special:{stat.S_IFMT(mode):o}"
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            return f"special:{stat.S_IFMT(opened.st_mode):o}"
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            digest = sha256_bytes(handle.read())
    finally:
        os.close(descriptor)
    return f"hardlink:{digest}" if opened.st_nlink > 1 else digest


def dir_manifest(root: Path) -> dict[str, str]:
    """sha256 per regular file under root; every other entry is recorded with a `symlink:`, `special:`
    or `hardlink:` marker instead of being skipped."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        names = sorted(filenames + dirnames)
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in names:
            path = Path(dirpath) / name
            rel = path.relative_to(root)
            digest = _entry_digest(path)
            if digest is None:
                continue
            # cache files are ignored, but a link or special file hiding under such a name is not
            if _ignored(rel) and not digest.startswith(NON_REGULAR_PREFIXES):
                continue
            out[rel.as_posix()] = digest
    return dict(sorted(out.items()))


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def read_files(root: Path) -> dict[str, str]:
    """Text content of every non-ignored regular file (symlinks skipped)."""
    out: dict[str, str] = {}
    for rel, digest in dir_manifest(root).items():
        if not digest.startswith(NON_REGULAR_PREFIXES[:2]):
            out[rel] = (root / rel).read_text(encoding="utf-8", errors="replace")
    return out


def diff_manifests(base: dict[str, str], end: dict[str, str]) -> dict[str, list[str]]:
    return {
        "added": sorted(p for p in end if p not in base),
        "modified": sorted(p for p in end if p in base and end[p] != base[p]),
        "deleted": sorted(p for p in base if p not in end),
    }


def changed_paths(diff: dict[str, list[str]]) -> list[str]:
    return sorted(diff["added"] + diff["modified"] + diff["deleted"])
