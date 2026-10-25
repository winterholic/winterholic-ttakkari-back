"""Acquisition helpers: inert GitHub inventory and an isolated PDF parser.

All web requests use the existing DNS-pinned bounded transport. Repository files
are data, including SKILL.md and scripts; no fetched instruction is executed.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
from collections.abc import Awaitable, Callable
from urllib.parse import quote, unquote, urlsplit

from ttakkari.collector.fetcher import FetchError, FetchLimits, SafeFetcher
from ttakkari.collector.netguard import NetGuard, normalize_url
from ttakkari.collector.parse import ExtractedDocument

PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC", "CC0-1.0", "Unlicense", "CC-BY-4.0", "CC-BY-SA-4.0"}
_EXCLUDED = {"node_modules", ".git", ".venv", "vendor", "dist", "build", "__pycache__"}
_DOC_SUFFIXES = (".md", ".rst", ".txt", ".mdx")
_CODE_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".rs", ".go", ".sh", ".yaml", ".yml", ".json")


async def fetch_json(url: str) -> dict:
    """Bounded public API metadata, with redirects restricted to the same provider."""
    parsed = normalize_url(url)
    fetcher = SafeFetcher(NetGuard(), user_agent="TtakkariCollector/1.0", limits=FetchLimits(max_bytes=8 * 1024 * 1024))
    try:
        response = await fetcher.fetch(parsed, host_check=lambda target: None if target.host == parsed.host else "api_redirect_host_changed")
    finally:
        await fetcher.aclose()
    value = json.loads(response.body)
    if not isinstance(value, dict):
        raise TypeError("invalid_api_object")
    return value


async def github_repository(url: str, *, fetch_json: Callable[[str], Awaitable[dict]] = fetch_json, limit: int = 200) -> dict:
    parsed = urlsplit(url)
    parts = [unquote(part) for part in parsed.path.strip("/").split("/")]
    if parsed.hostname != "github.com" or len(parts) < 2 or not 1 <= limit <= 2000:
        raise ValueError("invalid_github_repository")
    owner, repo = parts[:2]
    repo = repo.removesuffix(".git")
    if not all(re.fullmatch(r"[\w.-]+", part) and part not in {".", ".."} for part in (owner, repo)):
        raise ValueError("invalid_github_repository")
    base = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}"
    meta = await fetch_json(base)
    ref = parts[3] if len(parts) >= 4 and parts[2] in {"tree", "blob"} else meta.get("default_branch", "main")
    prefix = "/".join(parts[4:]) if len(parts) >= 5 and parts[2] in {"tree", "blob"} else ""
    commit = await fetch_json(f"{base}/commits/{quote(ref, safe='')}")
    revision = commit.get("sha", "")
    if not re.fullmatch(r"[0-9a-f]{40,64}", revision):
        raise ValueError("github_revision_missing")
    tree_sha = (commit.get("commit") or {}).get("tree", {}).get("sha")
    if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-f]{40,64}", tree_sha):
        raise ValueError("github_tree_missing")
    tree = await fetch_json(f"{base}/git/trees/{tree_sha}?recursive=1")
    try:
        license_info = await fetch_json(f"{base}/license?ref={revision}")
    except FetchError as exc:
        if exc.status != 404:
            raise
        license_info = {}
    license_id = (license_info.get("license") or {}).get("spdx_id")
    license_path = license_info.get("path", "LICENSE")
    candidates = []
    for entry in tree.get("tree", []):
        path = entry.get("path", "")
        segments = path.split("/")
        if entry.get("type") != "blob" or not path or any(segment in _EXCLUDED or segment in {"", ".", ".."} for segment in segments):
            continue
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            continue
        if entry.get("size", 0) > 1_000_000:
            continue
        lower = path.lower()
        is_skill = segments[-1] == "SKILL.md"
        is_doc = lower.endswith(_DOC_SUFFIXES)
        is_related_code = ("skills" in segments or "references" in segments or "examples" in segments) and lower.endswith(_CODE_SUFFIXES)
        if not (is_doc or is_related_code):
            continue
        priority = 0 if is_skill else 1 if segments[-1].lower().startswith("readme") else 2 if is_doc else 3
        candidates.append((priority, path, entry))
    candidates.sort(key=lambda entry: (entry[0], entry[1]))
    documents = [{"url": f"https://raw.githubusercontent.com/{quote(owner)}/{quote(repo)}/{revision}/{quote(path, safe='/')}",
                  "path": path, "blob_sha": entry.get("sha"), "repository": f"{owner}/{repo}",
                  "revision": revision, "kind": "skill" if path.endswith("/SKILL.md") or path == "SKILL.md" else "reference",
                  "provenance": "github_api", "adapter": "website"}
                 for _, path, entry in candidates[:limit]]
    return {
        "repository": f"{owner}/{repo}", "revision": revision, "ref": ref,
        "documents": documents,
        "license": {"id": license_id, "permission": "allowed" if license_id in PERMISSIVE_LICENSES else "unknown",
                    "url": f"https://github.com/{quote(owner)}/{quote(repo)}/blob/{revision}/{quote(license_path, safe='/')}",
                    "metadata_sha": license_info.get("sha")},
        "coverage": {"eligible_files": len(candidates), "selected_files": len(documents),
                     "truncated": bool(tree.get("truncated")), "limited": len(candidates) > limit,
                     "excluded_directories": sorted(_EXCLUDED)},
    }


async def extract_pdf(raw: bytes) -> ExtractedDocument:
    if len(raw) > 20 * 1024 * 1024:
        raise ValueError("pdf_byte_limit")
    proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "ttakkari.collector.pdf_worker",
                                               stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                                               stderr=asyncio.subprocess.DEVNULL)
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(raw), 20)
    except (TimeoutError, asyncio.CancelledError):
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise ValueError("pdf_parser_resource_or_process_failure")
    result = json.loads(stdout)
    if result.get("error"):
        raise ValueError(result["error"])
    return ExtractedDocument(result["text"], result["title"], None, None, result["author"], None, None, None,
                             result["truncated"], {"pdf_pages": json.dumps(result["pages"]),
                                                   "pdf_total_pages": str(result["total_pages"]), "adapter": "pypdf",
                                                   "parser_version": result["parser_version"]})
