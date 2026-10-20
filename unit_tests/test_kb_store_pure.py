"""KB2 순수 함수 테스트(DB 없음). DB 통합 테스트는 tests/test_kb_library.py 에 있다."""

import hashlib

import pytest

from ttakkari.cli import main as cli_main
from ttakkari.kb_store import (
    _write_file_atomic,
    normalize_text,
    object_relpath,
    span_digest,
    split_paragraphs,
)


def test_normalize_text_is_deterministic():
    raw = "é Æ\r\n둘째".encode()  # e + 결합 악센트
    once = normalize_text(raw)
    assert once == normalize_text(raw)
    assert "\r" not in once
    assert once.startswith("é")


def test_normalize_text_replaces_invalid_bytes():
    assert normalize_text(b"abc\xff\xfe") == "abc��"


def test_split_paragraphs_offsets_slice_back():
    text = "첫째 문단이다.\n\n둘째 문단에는 인용할 문장이 있다.\n\n셋째 문단이다.\n"
    parts = split_paragraphs(text)
    assert [b for _, b, _, _ in parts] == ["첫째 문단이다.", "둘째 문단에는 인용할 문장이 있다.", "셋째 문단이다."]
    for _h, body, start, end in parts:
        assert text[start:end] == body


def test_split_paragraphs_edge_cases():
    assert split_paragraphs("") == []
    assert split_paragraphs("   \n\n  \n") == []
    assert split_paragraphs("한 문단") == [(None, "한 문단", 0, 4)]
    assert split_paragraphs("\n\n앞뒤 공백\n\n") == [(None, "앞뒤 공백", 2, 7)]


def test_span_digest_matches_sha256_of_slice():
    body = "둘째 문단에는 인용할 문장이 있다."
    start = body.index("인용할 문장")
    end = start + len("인용할 문장")
    assert span_digest(body, start, end) == hashlib.sha256("인용할 문장".encode()).hexdigest()


def test_object_relpath_fanout():
    digest = "ab" + "cd" + "e" * 60
    assert str(object_relpath(digest)) == f"ab/cd/{digest}"


def test_write_file_atomic(tmp_path):
    target = tmp_path / "aa" / "bb" / "object"
    _write_file_atomic(target, b"bytes")
    assert target.read_bytes() == b"bytes"
    assert list((tmp_path / "aa" / "bb").iterdir()) == [target]


def test_cli_registers_kb_commands(capsys):
    with pytest.raises(SystemExit) as e:
        cli_main(["kb-verify", "--help"])
    assert e.value.code == 0
    assert "clean-orphans" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli_main(["kb-ingest", "--help"])
    assert e.value.code == 0
    assert "source-key" in capsys.readouterr().out


def test_normalize_text_nfd_to_nfkc():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "가나다\r\n라마바")
    nfkc = normalize_text(nfd.encode("utf-8"))
    assert unicodedata.is_normalized("NFKC", nfkc)
    assert nfkc == "가나다\n라마바"
