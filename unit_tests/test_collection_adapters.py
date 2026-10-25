"""Adapter discovery preserves immutable references and bounded failure evidence."""
import importlib
import json

import pytest


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
