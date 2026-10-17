"""Read-only, commit-pinned winterholic design-system index (ADR 0034).

Never build, execute, fetch, or read working-tree files. All content is selected
from regular Git blobs under an allowlist, including during concurrent DS edits.
"""
from __future__ import annotations

import html
import json
import re
import subprocess
from collections import Counter
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

SYSTEMS = {
    "ttakkari": ("Ttakkari", "tk"), "aip": ("AIP", "aip"),
    "winterholic-base": ("Winterholic Base", "wh"), "stock-gosu": ("Stock Gosu", "sg"),
    "memoir": ("Memoir", "mm"), "notting": ("Notting", "nt"), "yeohaenggajya": ("여행가쟈", "yg"),
}
KINDS = ("token", "document", "component", "example")
MAX_RESPONSE = 16000
REF = re.compile(r"\{([^{}]+)\}")


class Unavailable(Exception):
    pass


class NotFound(Exception):
    pass


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    try:
        return subprocess.run(["git", "-C", str(root), *args], input=data, capture_output=True,
                              check=True, timeout=15).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise Unavailable("디자인시스템 저장소를 읽을 수 없습니다. 경로와 Git 상태를 확인하세요.") from exc


def _kind(path: str) -> str | None:
    p = PurePosixPath(path)
    parts = p.parts
    if len(parts) < 2 or parts[0] not in SYSTEMS:
        return None
    tail = parts[1:]
    if len(tail) == 1 and tail[0] in ("README.md", "CLAUDE.md", "CHANGELOG.md", "package.json"):
        return "metadata" if p.suffix == ".json" else "document"
    if len(tail) == 3 and tail[:2] == ("tokens", "src") and p.suffix == ".json":
        return "token_source"
    if tail[0] == "docs" and p.suffix == ".md":
        return "document"
    if tail[0] == "components" and p.suffix == ".css":
        return "component"
    if tail[0] == "examples" and p.suffix == ".html":
        return "example"
    return None


def _flatten(node: Any, source: str, path: tuple[str, ...] = (), inherited_type: str | None = None,
             description: str = "") -> dict[str, dict]:
    out = {}
    if not isinstance(node, dict):
        return out
    typ = node.get("$type", inherited_type)
    desc = node.get("$description", description)
    if "$value" in node:
        out[".".join(path)] = {"raw": node["$value"], "type": typ, "description": desc,
                               "source": source, "pointer": "/" + "/".join(path)}
    else:
        for key, child in node.items():
            if not key.startswith("$"):
                out.update(_flatten(child, source, (*path, key), typ, desc))
    return out


def _resolve(value: Any, tokens: dict, seen: tuple[str, ...] = ()) -> Any:
    if len(seen) > 40:
        raise ValueError("reference depth exceeded")
    if isinstance(value, str):
        match = REF.fullmatch(value)
        if not match:
            return value
        key = match[1]
        if key in seen:
            raise ValueError("reference cycle: " + " → ".join((*seen, key)))
        if key not in tokens:
            raise ValueError("missing reference: " + key)
        return _resolve(tokens[key]["raw"], tokens, (*seen, key))
    if isinstance(value, list):
        return [_resolve(v, tokens, seen) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, tokens, seen) for k, v in value.items()}
    return value


def _theme(tokens: dict, key: str) -> dict | None:
    if key not in tokens:
        return None
    token = tokens[key]
    result = {**token, "references": sorted(set(REF.findall(json.dumps(token["raw"]))))}
    try:
        result.update(resolved=_resolve(token["raw"], tokens, (key,)), error=None)
    except ValueError as exc:
        result.update(resolved=None, error=str(exc))
    return result


@lru_cache(maxsize=4)
def _snapshot(root: Path, revision: str) -> dict:
    tree = _git(root, "ls-tree", "-rlz", revision).decode().split("\0")
    blobs = []
    for row in filter(None, tree):
        meta, path = row.split("\t", 1)
        mode, typ, oid, size = meta.split()
        kind = _kind(path)
        if mode not in ("100644", "100755") or typ != "blob" or kind is None:
            continue
        if int(size) > 512 * 1024:
            raise Unavailable("디자인시스템 파일이 조회 한도(512 KiB)를 넘었습니다: " + path)
        blobs.append((oid, path, kind))
    if len(blobs) > 600 or sum(int(r.split("\t")[0].split()[-1]) for r in filter(None, tree)
                              if _kind(r.split("\t", 1)[1]) is not None and r.startswith("100")) > 16 * 1024 * 1024:
        raise Unavailable("디자인시스템 색인 크기가 한도를 넘었습니다.")
    data = _git(root, "cat-file", "--batch", data="".join(oid + "\n" for oid, _, _ in blobs).encode()) if blobs else b""
    files = {}
    pos = 0
    try:
        for _, path, kind in blobs:
            end = data.index(b"\n", pos)
            size = int(data[pos:end].split()[-1])
            content = data[end + 1:end + 1 + size].decode("utf-8")
            pos = end + size + 2
            files[path] = {"kind": kind, "content": content}
        systems = {}
        for system, (label, fallback_prefix) in SYSTEMS.items():
            package = files.get(f"{system}/package.json")
            if package is None:
                continue
            pkg = json.loads(package["content"])
            # Prefix is derived from the committed build contract, not display names.
            # All current systems declare a literal PREFIX in this fixed build path.
            build_path = f"{system}/tokens/build.mjs"
            build = _git(root, "show", f"{revision}:{build_path}").decode() if any(
                r.endswith("\t" + build_path) and r.startswith("100") for r in tree) else ""
            match = re.search(r"const PREFIX\s*=\s*['\"]([\w-]+)['\"]", build)
            prefix = match[1] if match else fallback_prefix
            shared, light, dark = {}, {}, {}
            for path, file in files.items():
                if path.startswith(system + "/") and file["kind"] == "token_source":
                    flat = _flatten(json.loads(file["content"]), path)
                    name = PurePosixPath(path).name
                    if name == "color.light.json":
                        light.update(flat)
                    elif name == "color.dark.json":
                        dark.update(flat)
                    else:
                        shared.update(flat)
            maps = ({**shared, **light}, {**shared, **dark})
            entries = {}
            for key in sorted(set(maps[0]) | set(maps[1])):
                l, d = _theme(maps[0], key), _theme(maps[1], key)
                value = l or d
                entry = {"id": "token:" + key, "kind": "token", "title": key, "type": value["type"],
                         "description": value["description"], "source": value["source"],
                         "css_variable": "--" + prefix + "-" + key.replace(".", "-"), "light": l, "dark": d}
                entries[entry["id"]] = entry
            for path, file in files.items():
                if path.startswith(system + "/") and file["kind"] in KINDS:
                    text = file["content"]
                    title = next((s.lstrip("# ").strip() for s in text.splitlines() if s.startswith("# ")), PurePosixPath(path).name)[:200]
                    if file["kind"] == "example":
                        title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
                        if title_match:
                            title = html.unescape(re.sub(r"<[^>]+>", "", title_match[1])).strip()[:200]
                    entries["file:" + path] = {"id": "file:" + path, "kind": file["kind"], "title": title,
                                               "source": path, "content": text}
            counts = Counter(e["kind"] for e in entries.values())
            systems[system] = {"id": system, "name": label, "version": pkg.get("version", "unknown"),
                               "counts": {k: counts[k] for k in KINDS}, "entries": entries}
    except (ValueError, UnicodeError, KeyError, IndexError) as exc:
        raise Unavailable("디자인시스템 원본을 해석하지 못했습니다: " + str(exc)[:180]) from exc
    return {"revision": revision, "systems": systems}


class Browser:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def _read(self, revision: str | None = None) -> dict:
        if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise ValueError("revision은 전체 40자리 커밋 SHA여야 합니다.")
        rev = _git(self.root, "rev-parse", "--verify", (revision or "HEAD") + "^{commit}").decode().strip()
        return _snapshot(self.root, rev)

    def catalog(self) -> dict:
        snap = self._read()
        return {"revision": snap["revision"], "source_mode": "committed", "items": [
            {k: v for k, v in system.items() if k != "entries"} for system in snap["systems"].values()]}

    def _system(self, system: str, revision: str | None) -> tuple[dict, dict]:
        snap = self._read(revision)
        if system not in snap["systems"]:
            raise NotFound("디자인시스템이 없습니다.")
        return snap, snap["systems"][system]

    def search(self, system: str, query: str = "", kind: str | None = None, limit: int = 20, cursor: int = 0,
               revision: str | None = None) -> dict:
        if not 1 <= limit <= 40 or cursor < 0 or len(query) > 200 or kind not in (*KINDS, None):
            raise ValueError("검색 조건이 올바르지 않습니다.")
        snap, ds = self._system(system, revision)
        terms = query.casefold().split()
        rows = []
        for entry in ds["entries"].values():
            haystack = json.dumps(entry, ensure_ascii=False).casefold()
            if (kind is None or kind == entry["kind"]) and all(t in haystack for t in terms):
                rows.append(entry)
        rows.sort(key=lambda e: (KINDS.index(e["kind"]), e["source"], e["id"]))
        out = {"revision": snap["revision"], "system": system, "total": len(rows), "items": [], "next_cursor": None}
        for entry in rows[cursor:cursor + limit]:
            summary = {k: entry[k] for k in ("id", "kind", "title", "source")}
            content = entry.get("content", entry.get("description", ""))
            at = max(0, content.casefold().find(terms[0]) - 70) if terms else 0
            summary["excerpt"] = content[at:at + 240]
            if entry["kind"] == "token":
                summary.update(type=entry["type"], css_variable=entry["css_variable"],
                               values={t: entry[t]["resolved"] if entry[t] else None for t in ("light", "dark")})
            out["items"].append(summary)
            if len(json.dumps(out, ensure_ascii=False)) > 13000:
                out["items"].pop()
                break
        next_cursor = cursor + len(out["items"])
        out["next_cursor"] = next_cursor if next_cursor < len(rows) else None
        return out

    def detail(self, system: str, item: str, offset: int = 0, length: int = 6000, revision: str | None = None) -> dict:
        if offset < 0 or not 500 <= length <= 10000:
            raise ValueError("본문 범위가 올바르지 않습니다.")
        snap, ds = self._system(system, revision)
        if item not in ds["entries"]:
            raise NotFound("조회 가능한 항목이 없습니다. 검색 결과의 id를 사용하세요.")
        entry = ds["entries"][item]
        out = {"revision": snap["revision"], "system": system, **entry}
        if "content" in entry:
            content = entry["content"]
            out.update(content=content[offset:offset + length], offset=offset, total_chars=len(content),
                       next_offset=offset + length if offset + length < len(content) else None)
        if len(json.dumps(out, ensure_ascii=False)) > MAX_RESPONSE:
            raise Unavailable("항목이 응답 한도를 넘었습니다. 본문 length를 줄여 조회하세요.")
        return out

    def preview(self, system: str, item: str | None = None, theme: str = "light", revision: str | None = None) -> dict:
        from ttakkari.design_preview import build_preview
        if theme not in ("light", "dark"):
            raise ValueError("테마는 light 또는 dark여야 합니다.")
        snap, ds = self._system(system, revision)
        entry = ds["entries"].get(item or f"file:{system}/examples/preview.html")
        if entry is None or entry["kind"] != "example":
            raise NotFound("미리보기 가능한 예제가 없습니다.")
        return build_preview(self.root, snap["revision"], system, entry["source"], theme)
