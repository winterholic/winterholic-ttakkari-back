"""share 기존 행의 stale 보관 사본 갱신. DB 없이 micro 동작만 본다."""
from __future__ import annotations

import asyncio
import hashlib
import shutil

import pytest

from ttakkari.artifacts import service
from ttakkari.broker.policy import BrokerError
from ttakkari.models import Artifact, PreviewStatus


def _art(tmp_path, name="report.md", mode="copy", **kw):
    orig = tmp_path / name
    orig.write_bytes(b"# v1\n")
    stored = tmp_path / "store" / name
    kw.setdefault("size_bytes", 5)
    kw.setdefault("sha256", hashlib.sha256(b"# v1\n").hexdigest())
    kw.setdefault("mime_type", "text/markdown")
    kw.setdefault("kind", "markdown")
    kw.setdefault("preview_status", PreviewStatus.not_required)
    if mode == "copy":
        stored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(orig, stored)
    return orig, Artifact(
        filename=name, mime_type=kw.pop("mime_type"), kind=kw.pop("kind"), source="agent_output",
        original_path=str(orig), storage_mode=mode, stored_path=str(stored),
        size_bytes=kw.pop("size_bytes"), sha256=kw.pop("sha256"),
        preview_status=kw.pop("preview_status"), **kw,
    )


def _store(monkeypatch, tmp_path):
    import types

    monkeypatch.setattr(
        service, "get_settings",
        lambda: types.SimpleNamespace(artifact_store=(tmp_path / "store").resolve()),
    )


def test_refresh_updates_stale_copy_and_metadata(tmp_path, monkeypatch):
    _store(monkeypatch, tmp_path)
    orig, art = _art(tmp_path)
    orig.write_bytes(b"# v2 much longer content\n")
    changed = asyncio.run(service.refresh_from_original(art, orig))
    assert changed is True
    assert (tmp_path / "store" / "report.md").read_bytes() == b"# v2 much longer content\n"
    assert art.size_bytes == len(b"# v2 much longer content\n")
    assert art.sha256 == hashlib.sha256(b"# v2 much longer content\n").hexdigest()
    assert art.preview_status == PreviewStatus.not_required


def test_refresh_noop_when_unchanged(tmp_path, monkeypatch):
    _store(monkeypatch, tmp_path)
    orig, art = _art(tmp_path)
    before = (tmp_path / "store" / "report.md").stat().st_mtime_ns
    changed = asyncio.run(service.refresh_from_original(art, orig))
    assert changed is False
    assert (tmp_path / "store" / "report.md").stat().st_mtime_ns == before


def test_refresh_resets_preview_when_content_changed(tmp_path, monkeypatch):
    _store(monkeypatch, tmp_path)
    orig = tmp_path / "a.docx"
    orig.write_bytes(b"PK v1 docx")
    stored = tmp_path / "store" / "a.docx"
    stored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(orig, stored)
    art = Artifact(
        filename="a.docx", mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        kind="docx", source="agent_output",
        original_path=str(orig), storage_mode="copy", stored_path=str(stored),
        size_bytes=len(b"PK v1 docx"), sha256=hashlib.sha256(b"PK v1 docx").hexdigest(),
        preview_status=PreviewStatus.ready, preview_path=str(tmp_path / "store" / "a.preview.pdf"),
    )
    (tmp_path / "store" / "a.preview.pdf").write_bytes(b"old preview")
    orig.write_bytes(b"PK v2 docx with more bytes")
    changed = asyncio.run(service.refresh_from_original(art, orig))
    assert changed is True
    assert art.preview_status == PreviewStatus.pending
    assert art.preview_path is None


def test_refresh_reference_mode_updates_metadata_only(tmp_path):
    orig, art = _art(tmp_path, mode="reference")
    assert not (tmp_path / "store" / "report.md").exists()
    orig.write_bytes(b"# v2\n")
    changed = asyncio.run(service.refresh_from_original(art, orig))
    assert changed is True
    assert art.size_bytes == len(b"# v2\n")
    assert not (tmp_path / "store" / "report.md").exists()


def test_refresh_rejects_store_outside_managed_area(tmp_path, monkeypatch):
    _store(monkeypatch, tmp_path)
    orig, art = _art(tmp_path)
    art.stored_path = str(tmp_path / "elsewhere" / "report.md")
    orig.write_bytes(b"# v2\n")
    with pytest.raises(BrokerError):
        asyncio.run(service.refresh_from_original(art, orig))
