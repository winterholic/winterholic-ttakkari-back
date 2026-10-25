"""Adapter discovery preserves immutable references and bounded failure evidence."""
import importlib
import json

import pytest

MIT_BODY = '''MIT License

Copyright (c) 2026 Example

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.'''


def adapter():
    try:
        return importlib.import_module("ttakkari.collector.adapters")
    except ModuleNotFoundError:
        pytest.fail("collection adapters are not implemented")


async def test_github_uses_api_and_pins_skill_resources_to_commit():
    calls = []
    async def fetch(url):
        calls.append(url)
        if "/license?" in url:
            return {"license": {"spdx_id": "MIT"}, "path": "LICENSE", "sha": "b" * 40}
        if "/git/trees/" in url:
            return {"sha": "c" * 40, "truncated": False, "tree": [
                {"path": "README.md", "type": "blob", "size": 500},
                {"path": "skills/robot/SKILL.md", "type": "blob", "size": 1000},
                {"path": "skills/robot/references/control.md", "type": "blob", "size": 1000},
                {"path": "skills/robot/scripts/control.py", "type": "blob", "size": 1000},
                {"path": "node_modules/ignored.md", "type": "blob", "size": 1000},
            ]}
        if "/commits/" in url:
            return {"sha": "d" * 40, "commit": {"tree": {"sha": "c" * 40}}}
        return {"default_branch": "main", "license": {"spdx_id": "MIT"}, "full_name": "owner/skills"}
    result = await adapter().github_repository("https://github.com/owner/skills", fetch_json=fetch, limit=20)
    assert all(url.startswith("https://api.github.com/repos/owner/skills") for url in calls)
    assert result["revision"] == "d" * 40
    paths = {entry["path"] for entry in result["documents"]}
    assert "skills/robot/SKILL.md" in paths
    assert "skills/robot/references/control.md" in paths
    assert "skills/robot/scripts/control.py" in paths
    assert not any("node_modules" in path for path in paths)
    assert all("/" + "d" * 40 + "/" in entry["url"] for entry in result["documents"])
    assert result["license"]["id"] == "MIT"
    assert result["coverage"]["truncated"] is False


async def test_github_never_calls_missing_license_permission_allowed():
    async def fetch(url):
        if "/git/trees/" in url:
            return {"sha": "c" * 40, "tree": [], "truncated": True}
        if "/license?" in url:
            return {"license": {"spdx_id": "NOASSERTION"}, "path": "LICENSE"}
        if "/commits/" in url:
            return {"sha": "d" * 40, "commit": {"tree": {"sha": "c" * 40}}}
        return {"default_branch": "main", "full_name": "owner/unlicensed"}
    result = await adapter().github_repository("https://github.com/owner/unlicensed", fetch_json=fetch)
    assert result["license"]["permission"] == "unknown"
    assert result["coverage"]["truncated"] is True


@pytest.mark.parametrize("prefix,extra,allowed", [("", "", True), ("", "\nAdditional restriction: personal use only.", False),
                                                ("Personal use only.\n", "", False)])
async def test_nested_license_overrides_repository_grant_and_is_commit_pinned(prefix, extra, allowed):
    calls, raw_calls = [], []
    async def fetch_license(url):
        raw_calls.append(url)
        return (prefix + MIT_BODY + extra).encode() if "/skills/open/LICENSE" in url else b"All rights reserved."
    async def fetch(url):
        calls.append(url)
        if url == "https://api.github.com/licenses/mit":
            return {"spdx_id": "MIT", "body": MIT_BODY}
        if "/git/trees/" in url:
            return {"tree": [
                {"path": "skills/open/SKILL.md", "type": "blob"},
                {"path": "skills/open/LICENSE", "type": "blob"},
                {"path": "skills/restricted/SKILL.md", "type": "blob"},
                {"path": "skills/restricted/LICENSE", "type": "blob"},
            ]}
        if "/license?" in url:
            return {"license": {"spdx_id": "MIT"}, "path": "LICENSE"}
        if "/commits/" in url:
            return {"sha": "d" * 40, "commit": {"tree": {"sha": "c" * 40}}}
        return {"default_branch": "main"}
    result = await adapter().github_repository("https://github.com/owner/skills", fetch_json=fetch, fetch_license=fetch_license)
    docs = {entry["path"]: entry for entry in result["documents"]}
    assert docs["skills/open/SKILL.md"]["license"]["permission"] == ("allowed" if allowed else "unknown")
    assert docs["skills/restricted/SKILL.md"]["license"]["permission"] == "unknown"
    assert len(raw_calls) == 2
    assert all("/" + "d" * 40 + "/" in url for url in raw_calls)
    assert len(calls) <= 5  # License lookup must not consume an API call per file/directory.


def test_automatic_license_policy_cannot_authorize_neighboring_paths():
    from ttakkari.collector.netguard import normalize_url
    from ttakkari.collector.policy import SourcePolicySpec

    spec = SourcePolicySpec.from_dict({
        "allowed_hosts": ["raw.githubusercontent.com"], "allowed_path_prefixes": ["/owner/repo/sha/SKILL.md"],
        "permissions": {"store": "allowed"}, "license_id": "MIT", "policy_url": "https://github.com/owner/repo/blob/sha/LICENSE",
        "checked_at": "2026-10-25T00:00:00Z", "basis": "commit-pinned file license",
    })
    assert spec.check_url(normalize_url("https://raw.githubusercontent.com/owner/repo/sha/SKILL.md")) is None
    assert spec.check_url(normalize_url("https://raw.githubusercontent.com/other/repo/sha/SKILL.md")) == "path_outside_policy"
    assert spec.check_url(normalize_url("https://raw.githubusercontent.com/owner/repo/sha/SKILL.md-secret")) == "path_outside_policy"


async def test_search_results_retain_query_provider_and_never_grant_rights():
    async def fetch(url):
        return b'''<html><body><a class="result__a" href="https://example.org/paper.pdf">Original paper</a>
        <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fgithub.com%2Fowner%2Frepo">Skill</a>
        <a class="result__a" href="https://example.org/paper.pdf">Duplicate</a></body></html>'''
    result = await adapter().search_web("harness engineering papers", fetch_bytes=fetch)
    assert len(result) == 2
    assert result[0]["query"] == "harness engineering papers"
    assert result[0]["adapter"] == "paper_pdf"
    assert result[1]["adapter"] == "github"
    assert result[1]["provider"] == "duckduckgo_html"
    assert all(entry["provenance"] == "web_search" and "license" not in entry for entry in result)


def test_html_discovery_keeps_pdf_references_and_filters_archives():
    from ttakkari.collector.parse import extract_links

    result = extract_links(b'<a href="paper.pdf">Paper</a><a href="bundle.zip">Archive</a>', "https://example.org/docs/")
    assert result == ["https://example.org/docs/paper.pdf"]


def test_document_navigation_precedes_unrelated_site_chrome():
    from ttakkari.collector.parse import extract_links

    raw = b'''<html><body><nav><a href="/changelog">Release history</a></nav>
    <main><a href="chapter.html">Chapter</a><a href="paper.pdf">Paper</a></main></body></html>'''
    assert extract_links(raw, "https://example.org/docs/") == [
        "https://example.org/docs/chapter.html", "https://example.org/docs/paper.pdf", "https://example.org/changelog"]


async def test_pdf_process_extracts_text_and_rejects_encrypted_bytes():
    from ttakkari.collector import parse
    assert parse.detect_kind(b"%PDF-1.4 data", "text/html").kind == "pdf"
    from io import BytesIO

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(300, 300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 20 200 Td (Harness engineering research paper.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.add_metadata({"/Title": "Harness research"})
    buf = BytesIO()
    writer.write(buf)
    result = await adapter().extract_pdf(buf.getvalue())
    assert "Harness engineering" in result.text
    assert result.title == "Harness research"
    assert json.loads(result.meta["pdf_pages"])[0]["page"] == 1
    writer.encrypt("secret")
    buf = BytesIO()
    writer.write(buf)
    with pytest.raises(ValueError, match="pdf_encrypted"):
        await adapter().extract_pdf(buf.getvalue())


async def test_truncated_adapter_body_never_becomes_a_complete_normalized_document():
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from ttakkari.collector.parse import ExtractedDocument
    from ttakkari.collector.pipeline import _analyze
    from ttakkari.collector.policy import CollectRequest

    document = ExtractedDocument("A useful extracted fragment of a much larger document.", None, None, None, None, None, None, None, True)
    result = await _analyze(document, "https://example.org/doc", None, CollectRequest(("https://example.org/doc",), "general", {}, 1, 0), SimpleNamespace(clock=lambda: datetime.now(UTC)))
    assert result.truncated is True
