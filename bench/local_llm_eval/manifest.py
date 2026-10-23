"""File manifests, tree hashes and diffs (spec sections 1-7 and 5.1)."""

import hashlib
import os
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


def dir_manifest(root: Path) -> dict[str, str]:
    """sha256 per file under root. Symlinks are recorded as `symlink:<target>`."""
    out: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
        for name in sorted(
            filenames + [d for d in dirnames if (Path(dirpath) / d).is_symlink()]
        ):
            path = Path(dirpath) / name
            rel = path.relative_to(root)
            if _ignored(rel):
                continue
            if path.is_symlink():
                out[rel.as_posix()] = "symlink:" + os.readlink(path)
            elif path.is_file():
                out[rel.as_posix()] = sha256_bytes(path.read_bytes())
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
        if not digest.startswith("symlink:"):
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
