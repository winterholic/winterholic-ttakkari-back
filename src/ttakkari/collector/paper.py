"""Paper metadata and rights from DOI / arXiv identifiers.

A DOI or arXiv link in user text or in a collected reference list names a paper; it never grants a right to store
the full text. Full text is offered to the collector only when the provider itself reports an open license
(Creative Commons BY family or CC0). Everything else stays metadata-only and is reported as such.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

from lxml import etree

from ttakkari.collector import adapters
from ttakkari.collector.fetcher import FetchError

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_OAI = "https://oaipmh.arxiv.org/oai"
ARXIV_MIN_INTERVAL = 3.0  # arXiv API terms: at most one request every three seconds
_ATOM = {"a": "http://www.w3.org/2005/Atom", "x": "http://arxiv.org/schemas/atom"}
_CC = {"by/4.0": "CC-BY-4.0", "by-sa/4.0": "CC-BY-SA-4.0", "zero/1.0": "CC0-1.0", "by/3.0": "CC-BY-3.0", "by-sa/3.0": "CC-BY-SA-3.0",
       "by-nc/4.0": "CC-BY-NC-4.0", "by-nd/4.0": "CC-BY-ND-4.0", "by-nc-sa/4.0": "CC-BY-NC-SA-4.0", "by-nc-nd/4.0": "CC-BY-NC-ND-4.0"}
_gate = {"lock": None, "last": 0.0}
Fetch = Callable[[str], Awaitable[bytes]]


class PaperError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def license_from_url(url: str | None, *, source: str) -> dict:
    """Map a provider-reported license URL to an SPDX id. Only the open set is `allowed`; all else is unknown."""
    normalized = re.sub(r"^https?://(www\.)?", "", (url or "").strip().lower()).rstrip("/")
    normalized = re.sub(r"/(legalcode|deed)(\.\w+)?$", "", normalized)
    match = re.fullmatch(r"creativecommons\.org/(?:licenses|publicdomain)/(.+)", normalized)
    spdx = _CC.get(match.group(1)) if match else None
    return {"id": spdx, "url": url, "source": source,
            "permission": "allowed" if spdx in adapters.PERMISSIVE_LICENSES else "unknown"}


async def _throttle(interval: float) -> None:
    if _gate["lock"] is None:
        _gate["lock"] = asyncio.Lock()
    async with _gate["lock"]:
        wait = _gate["last"] + interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        _gate["last"] = time.monotonic()


def _xml(raw: bytes):
    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    try:
        return etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as exc:
        raise PaperError("provider_response_unparseable", str(exc)[:120]) from exc


def _text(node, path: str) -> str:
    found = node.find(path, _ATOM)
    return " ".join((found.text or "").split()) if found is not None else ""


def _entry_record(entry) -> dict:
    abs_url = _text(entry, "a:id")
    identifier = re.sub(r"^https?://arxiv\.org/abs/", "", abs_url)
    pdf = next((link.get("href") for link in entry.findall("a:link", _ATOM) if link.get("title") == "pdf"), None)
    published = _text(entry, "a:published")
    return {
        "kind": "arxiv", "identifier": identifier, "canonical_url": f"https://arxiv.org/abs/{identifier}",
        "title": _text(entry, "a:title"), "authors": [_text(a, "a:name") for a in entry.findall("a:author", _ATOM)][:20],
        "year": int(published[:4]) if published[:4].isdigit() else None, "venue": _text(entry, "x:journal_ref") or "arXiv",
        "abstract": _text(entry, "a:summary")[:1500], "doi": _text(entry, "x:doi") or None,
        "pdf_url": (pdf or f"https://arxiv.org/pdf/{identifier}").replace("http://", "https://"),
    }


async def arxiv_record(identifier: str, *, fetch: Fetch = adapters.fetch_bytes, min_interval: float = ARXIV_MIN_INTERVAL) -> dict:
    await _throttle(min_interval)
    root = _xml(await fetch(f"{ARXIV_API}?{urlencode({'id_list': identifier, 'max_results': 1})}"))
    entries = root.findall("a:entry", _ATOM)
    if not entries or _text(entries[0], "a:title").lower() == "error":
        raise PaperError("paper_not_found", identifier)
    record = _entry_record(entries[0])
    record["identifier"] = identifier  # keep the version the reader named
    record["canonical_url"] = f"https://arxiv.org/abs/{identifier}"
    record["pdf_url"] = f"https://arxiv.org/pdf/{identifier}"
    base = re.sub(r"v\d+$", "", identifier)
    rights = {"id": None, "url": None, "source": "arxiv_oai", "permission": "unknown", "error": None}
    evidence = b""
    try:
        await _throttle(min_interval)
        evidence = await fetch(f"{ARXIV_OAI}?{urlencode({'verb': 'GetRecord', 'identifier': f'oai:arXiv.org:{base}', 'metadataPrefix': 'arXiv'})}")
        node = _xml(evidence).find(".//{http://arxiv.org/OAI/arXiv/}license")
        rights = {**license_from_url(node.text.strip() if node is not None and node.text else None, source="arxiv_oai"), "error": None}
    except (FetchError, PaperError, TimeoutError) as exc:  # unknown rights, never assumed open
        rights["error"] = getattr(exc, "code", type(exc).__name__)
    rights["metadata_sha"] = hashlib.sha256(evidence).hexdigest() if evidence else None
    return _finish(record, rights, "arxiv_api+oai")


async def crossref_record(doi: str, *, fetch_json: Callable[[str], Awaitable[dict]] = adapters.fetch_json) -> dict:
    try:
        payload = await fetch_json(f"https://api.crossref.org/works/{quote(doi, safe='/')}")
    except FetchError as exc:
        if exc.status == 404:
            raise PaperError("paper_not_found", doi) from exc
        raise
    work = payload.get("message") or {}
    issued = ((work.get("issued") or {}).get("date-parts") or [[None]])[0]
    licenses = [license_from_url(entry.get("URL"), source="crossref") for entry in work.get("license") or []]
    rights = next((entry for entry in licenses if entry["permission"] == "allowed"), licenses[0] if licenses else
                  {"id": None, "url": None, "source": "crossref", "permission": "unknown"})
    rights = {**rights, "error": None, "metadata_sha": hashlib.sha256(repr(sorted(work.get("license") or [], key=repr)).encode()).hexdigest()}
    pdf = next((link.get("URL") for link in work.get("link") or [] if link.get("content-type") == "application/pdf"), None)
    record = {
        "kind": "doi", "identifier": doi, "canonical_url": f"https://doi.org/{doi}", "title": " ".join(((work.get("title") or [""])[0]).split()),
        "authors": [" ".join(filter(None, [a.get("given"), a.get("family")])) or a.get("name", "") for a in work.get("author") or []][:20],
        "year": issued[0] if issued else None, "venue": ((work.get("container-title") or [""])[0]) or work.get("publisher"),
        "abstract": None, "doi": doi, "pdf_url": pdf,
    }
    return _finish(record, rights, "crossref")


def _finish(record: dict, rights: dict, source: str) -> dict:
    allowed = rights["permission"] == "allowed" and bool(record.get("pdf_url"))
    return {**record, "license": rights, "metadata_source": source, "retrieved_at": datetime.now(UTC).isoformat(),
            "fulltext": "allowed" if allowed else "metadata_only",
            "rights_note": None if allowed else ("전문 보관 권한 미확인: 서지 정보만 기록" if rights["permission"] != "allowed"
                                               else "공개 라이선스이나 전문 PDF 주소 없음")}


async def resolve(ref: dict[str, str], **kwargs) -> dict:
    if ref["kind"] == "arxiv":
        return await arxiv_record(ref["identifier"], **{k: v for k, v in kwargs.items() if k in ("fetch", "min_interval")})
    return await crossref_record(ref["identifier"], **{k: v for k, v in kwargs.items() if k == "fetch_json"})


def document_for(record: dict, topics: list[str] | None = None) -> dict | None:
    """The collectable source for a paper whose provider-reported license is open, else None (metadata only)."""
    if record["fulltext"] != "allowed":
        return None
    rights = record["license"]
    return {"url": record["pdf_url"], "adapter": "paper_pdf", "provenance": "paper_metadata", "path": record["title"][:200] or record["identifier"],
            "paper": record["identifier"], "topics": list(topics or []),
            "license": {"id": rights["id"], "url": rights["url"], "permission": "allowed", "metadata_sha": rights.get("metadata_sha"),
                        "basis": f"{rights['source']} reports {rights['id']} for {record['kind']}:{record['identifier']}; "
                                 "preserve attribution and license URL."}}


async def search_arxiv(query: str, *, limit: int = 5, fetch: Fetch = adapters.fetch_bytes,
                       min_interval: float = ARXIV_MIN_INTERVAL) -> list[dict]:
    """Independent paper provider. Non-English queries yield no candidates (an honest empty), provider errors propagate."""
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z0-9\-]{2,}", query) if w.lower() not in {"arxiv", "paper", "research"}][:8]
    if not words:
        return []
    await _throttle(min_interval)
    raw = await fetch(f"{ARXIV_API}?{urlencode({'search_query': ' AND '.join(f'all:{w}' for w in words), 'max_results': limit, 'sortBy': 'relevance'})}")
    out = []
    for entry in _xml(raw).findall("a:entry", _ATOM):
        record = _entry_record(entry)
        if record["identifier"]:
            out.append({"url": record["canonical_url"], "adapter": "paper", "provenance": "arxiv_api", "provider": "arxiv_api",
                        "query": query, "title": record["title"][:500], "rank": len(out) + 1, "identifier": record["identifier"]})
    return out
