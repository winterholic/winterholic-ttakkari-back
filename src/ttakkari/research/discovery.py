"""Cheap discovery adapter. Metadata is evidence of discovery, not verified findings."""

from __future__ import annotations

from xml.etree import ElementTree

import httpx


async def search_arxiv(query: str) -> dict:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        r = await client.get(
            "https://export.arxiv.org/api/query",
            params={
                "search_query": "all:" + query,
                "max_results": 8,
                "sortBy": "relevance",
            },
            headers={"User-Agent": "TtakkariResearchLab/1.0 (metadata discovery)"},
        )
        r.raise_for_status()
        if (
            len(r.content) > 1_000_000
            or b"<!DOCTYPE" in r.content
            or b"<!ENTITY" in r.content
        ):
            raise ValueError("untrusted_xml_limit")
    root = ElementTree.fromstring(r.content)
    ns = {"a": "http://www.w3.org/2005/Atom"}
    return {
        "provider": "arxiv",
        "query": query,
        "verification": "discovered_only",
        "items": [
            {
                "source_key": "arxiv:" + e.findtext("a:id", "", ns).rsplit("/", 1)[-1],
                "locator": e.findtext("a:id", "", ns),
                "title": " ".join(e.findtext("a:title", "", ns).split()),
                "body": e.findtext("a:summary", "", ns).strip(),
                "updated": e.findtext("a:updated", "", ns),
                "authors": [
                    a.findtext("a:name", "", ns) for a in e.findall("a:author", ns)
                ],
                "content_scope": "abstract_only",
            }
            for e in root.findall("a:entry", ns)
        ],
    }
