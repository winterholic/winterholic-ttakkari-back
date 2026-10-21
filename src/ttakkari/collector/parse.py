"""Collector Platform: pure parsers for fetched bytes (CP-S2-04). No network, no database, no clock.

- detect_kind: what the bytes really are, so a mislabeled response is held instead of parsed as something else
- extract_document: main text and metadata from HTML (trafilatura) or plain text
- parse_feed / parse_sitemap / extract_links: discovery targets (RSS, Atom, sitemap, in-page links)

trafilatura is used only on bytes already in memory. Its own downloader is never called, so the safe fetch layer
keeps ownership of every network access.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin

import trafilatura
from charset_normalizer import from_bytes
from lxml import etree
from lxml import html as lxml_html

PARSER_VERSION = f"trafilatura-{trafilatura.__version__}+parse-1"
MAX_DISCOVERED_PER_DOCUMENT = 500
MAX_TEXT_CHARS = 1_000_000
_BINARY_EXTENSIONS = (
    ".pdf", ".zip", ".gz", ".tar", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".mp3", ".mp4",
    ".webm", ".mov", ".avi", ".woff", ".woff2", ".ttf", ".eot", ".exe", ".dmg", ".css", ".js", ".json", ".xml",
)
_HTML_TYPES = {"text/html", "application/xhtml+xml"}
_TEXT_TYPES = {"text/plain", "text/markdown"}
_FEED_TYPES = {"application/rss+xml", "application/atom+xml", "application/xml", "text/xml", "application/feed+json"}
_VERSION_RE = re.compile(r"(?<![\w.])v?(\d{1,3}(?:\.\d{1,3}){1,3})(?![\w.])", re.IGNORECASE)


@dataclass(frozen=True)
class DetectedKind:
    kind: str  # html | text | feed | sitemap | unsupported
    media_type: str
    reason: str | None = None


def decode_bytes(raw: bytes, charset_hint: str | None) -> tuple[str, str]:
    """Decode with the declared charset when it works, else detect. Returns (text, encoding used)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    if charset_hint:
        try:
            return raw.decode(charset_hint), charset_hint
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    best = from_bytes(raw[:200_000]).best()
    encoding = best.encoding if best is not None else "utf-8"
    return raw.decode(encoding, errors="replace"), encoding


def detect_kind(raw: bytes, declared_media_type: str) -> DetectedKind:
    head = raw[:2048]
    declared = declared_media_type.split(";")[0].strip().lower()
    stripped = head.lstrip()
    if head.startswith(b"%PDF-"):
        return DetectedKind("unsupported", "application/pdf", "pdf_not_supported")
    if head[:4] in (b"\x89PNG", b"GIF8", b"PK\x03\x04") or head[:3] == b"\xff\xd8\xff":
        return DetectedKind("unsupported", declared or "application/octet-stream", "binary_content")
    if b"\x00" in head:
        return DetectedKind("unsupported", declared or "application/octet-stream", "binary_content")
    lowered = stripped[:512].lower()
    looks_html = lowered.startswith((b"<!doctype html", b"<html")) or b"<html" in lowered or b"<body" in lowered
    root = _xml_root(stripped)
    if root in ("rss", "feed", "rdf"):
        return DetectedKind("feed", declared or "application/xml")
    if root in ("urlset", "sitemapindex"):
        return DetectedKind("sitemap", declared or "application/xml")
    if declared in _HTML_TYPES or (declared in ("", "application/octet-stream") and looks_html):
        return DetectedKind("html", declared or "text/html")
    if declared in _TEXT_TYPES:
        if looks_html:
            return DetectedKind("unsupported", declared, "media_type_mismatch")
        return DetectedKind("text", declared)
    if declared in _FEED_TYPES:
        return DetectedKind("unsupported", declared, "feed_unparseable")
    if declared.startswith("text/") and looks_html:
        return DetectedKind("html", declared)
    return DetectedKind("unsupported", declared or "application/octet-stream", "media_type_unsupported")


def _xml_root(stripped: bytes) -> str | None:
    if not stripped.startswith(b"<"):
        return None
    match = re.search(rb"<([A-Za-z_][\w.-]*:)?([A-Za-z_][\w.-]*)", re.sub(rb"<\?xml[^>]*\?>|<!--.*?-->|<!DOCTYPE[^>]*>", b"", stripped[:4096], flags=re.DOTALL))
    if match is None:
        return None
    name = match.group(2).decode("ascii", "replace").lower()
    return name if name in ("rss", "feed", "rdf", "urlset", "sitemapindex") else None


@dataclass(frozen=True)
class ExtractedDocument:
    text: str
    title: str | None
    published: datetime | None
    published_precision: str | None  # "date" when only a calendar date was found
    author: str | None
    language_hint: str | None
    canonical_url: str | None
    license: str | None
    truncated: bool
    meta: dict[str, str] = field(default_factory=dict)


def _parse_date(value: str | None) -> tuple[datetime | None, str | None]:
    if not value:
        return None, None
    try:
        if len(value) == 10:
            return datetime.fromisoformat(value).replace(tzinfo=UTC), "date"
        parsed = datetime.fromisoformat(value)
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)), "datetime"
    except ValueError:
        return None, None


def extract_document(raw: bytes, *, url: str, kind: str, charset: str | None = None) -> ExtractedDocument:
    """Main text and metadata. Raises ValueError("empty_extraction") when nothing usable is found."""
    decoded, _ = decode_bytes(raw, charset)
    if kind == "text":
        text = decoded
        return ExtractedDocument(text[:MAX_TEXT_CHARS], None, None, None, None, None, None, None, len(text) > MAX_TEXT_CHARS)
    if kind != "html":
        raise ValueError(f"cannot extract kind {kind}")
    doc = trafilatura.bare_extraction(
        decoded,
        url=url,
        with_metadata=True,
        include_comments=False,
        include_tables=True,
        include_formatting=False,
        favor_precision=True,
        deduplicate=False,
    )
    if doc is None or not (doc.text or "").strip():
        raise ValueError("empty_extraction")
    text = doc.text
    published, precision = _parse_date(doc.date)
    meta = _html_meta(decoded)
    return ExtractedDocument(
        text=text[:MAX_TEXT_CHARS],
        title=doc.title,
        published=published,
        published_precision=precision,
        author=doc.author,
        language_hint=doc.language,
        canonical_url=meta.get("canonical") or None,
        license=doc.license,
        truncated=len(text) > MAX_TEXT_CHARS,
        meta=meta,
    )


def _html_meta(decoded: str) -> dict[str, str]:
    """Head metadata the profiles need: canonical link, last-modified hints, generator/version hints."""
    try:
        tree = lxml_html.fromstring(decoded[:300_000])
    except (etree.ParserError, ValueError):
        return {}
    meta: dict[str, str] = {}
    for link in tree.xpath("//link[@rel='canonical']/@href")[:1]:
        meta["canonical"] = link.strip()
    for key, xpath in (
        ("modified", "//meta[@property='article:modified_time' or @name='last-modified' or @property='og:updated_time']/@content"),
        ("generator", "//meta[@name='generator']/@content"),
        ("doc_version", "//meta[@name='docsearch:version' or @name='version' or @name='docs-version']/@content"),
    ):
        found = tree.xpath(xpath)
        if found:
            meta[key] = str(found[0]).strip()[:200]
    return meta


def find_version_hint(url: str, text: str, meta: dict[str, str]) -> tuple[str | None, str | None]:
    """Where a document version can be read from: explicit meta, then the URL path, then the first text lines."""
    if meta.get("doc_version"):
        return meta["doc_version"], "meta"
    match = re.search(r"/(?:v|version/)?(\d{1,3}(?:\.\d{1,3}){0,3})(?=/|$)", url)
    if match and ("." in match.group(1) or re.search(r"/v\d", url)):
        return match.group(1), "url"
    head = "\n".join(text.splitlines()[:30])
    found = re.search(r"(?:version|ver\.?|release|버전)\s*[:：]?\s*v?(\d{1,3}(?:\.\d{1,3}){1,3})", head, re.IGNORECASE)
    if found:
        return found.group(1), "text"
    return None, None


def is_latest_alias(url: str) -> bool:
    return bool(re.search(r"/(latest|stable|current|master|main|dev)(/|$)", url))


_XML = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, dtd_validation=False, huge_tree=False)


def _local(tag: object) -> str:
    return etree.QName(tag).localname.lower() if isinstance(tag, str) else ""


def parse_feed(raw: bytes, base_url: str) -> list[str]:
    """Entry links of an RSS 2.0, RSS 1.0 or Atom feed, in document order, de-duplicated."""
    root = etree.fromstring(raw, _XML)
    links: list[str] = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "item":
            for child in el:
                if _local(child.tag) == "link" and (child.text or "").strip():
                    links.append(child.text.strip())
        elif name == "entry":
            best = None
            for child in el:
                if _local(child.tag) != "link":
                    continue
                if child.get("rel") in (None, "alternate") and child.get("href"):
                    best = child.get("href")
                    break
            if best:
                links.append(best)
    return _unique_joined(links, base_url)


def parse_sitemap(raw: bytes, base_url: str) -> tuple[list[str], list[str]]:
    """(page urls, nested sitemap urls)."""
    root = etree.fromstring(raw, _XML)
    pages: list[str] = []
    nested: list[str] = []
    for el in root.iter():
        if _local(el.tag) == "loc" and (el.text or "").strip():
            parent = _local(el.getparent().tag) if el.getparent() is not None else ""
            (nested if parent == "sitemap" else pages).append(el.text.strip())
    return _unique_joined(pages, base_url), _unique_joined(nested, base_url)


def extract_links(raw: bytes, base_url: str, charset: str | None = None) -> list[str]:
    decoded, _ = decode_bytes(raw, charset)
    try:
        tree = lxml_html.fromstring(decoded[:1_000_000])
    except (etree.ParserError, ValueError):
        return []
    out: list[str] = []
    for href in tree.xpath("//a[@href]/@href"):
        href = href.strip()
        if not href or href.startswith(("#", "mailto:", "javascript:", "tel:", "data:")):
            continue
        out.append(href)
    return _unique_joined(out, base_url, skip_binary=True)


def _unique_joined(urls: list[str], base_url: str, *, skip_binary: bool = False) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for u in urls:
        absolute = urljoin(base_url, u).split("#", 1)[0]
        if absolute in seen or (skip_binary and absolute.lower().split("?", 1)[0].endswith(_BINARY_EXTENSIONS)):
            continue
        seen.add(absolute)
        out.append(absolute)
        if len(out) >= MAX_DISCOVERED_PER_DOCUMENT:
            break
    return out
