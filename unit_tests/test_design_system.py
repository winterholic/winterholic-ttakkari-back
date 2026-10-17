"""Immutable design-system browsing, without a production database."""
import json
import subprocess

import pytest


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "user.name", "Test")
    files = {
        "ttakkari/package.json": {"name": "ttakkari", "version": "1.2.3"},
        "ttakkari/tokens/src/brand.json": {"brand": {"$type": "color", "blue": {"$value": "#123456"}}},
        "ttakkari/tokens/src/color.light.json": {"color": {"$type": "color", "action": {"$value": "{brand.blue}", "$description": "사람의 행동"}}},
        "ttakkari/tokens/src/color.dark.json": {"color": {"$type": "color", "action": {"$value": "#abcdef"}}},
    }
    for path, value in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(value))
    docs = tmp_path / "ttakkari/docs"
    docs.mkdir()
    (docs / "00-guide.md").write_text("# 사용 지침\n\n사람의 행동에는 Blue를 사용한다.\n" + "긴 문서\n" * 3000)
    (tmp_path / "ttakkari/components").mkdir()
    (tmp_path / "ttakkari/components/button.css").write_text(".tk-button { color: var(--tk-color-action); }")
    (tmp_path / "ttakkari/examples").mkdir()
    (tmp_path / "ttakkari/examples/button.html").write_text('<script>alert(1)</script><button class="tk-button">확인</button>')
    (docs / "secret-link.md").symlink_to("/etc/passwd")
    (tmp_path / "secret.env").write_text("SECRET=never")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "fixture")
    return tmp_path


def browser(repo):
    from ttakkari.design_system import Browser
    return Browser(repo)


def test_catalog_uses_committed_source_and_reports_revision(repo):
    b = browser(repo)
    (repo / "ttakkari/package.json").write_text('{"version":"dirty"}')
    result = b.catalog()
    assert result["revision"] == git(repo, "rev-parse", "HEAD")
    assert result["items"][0]["version"] == "1.2.3"
    assert result["items"][0]["counts"] == {"token": 2, "document": 1, "component": 1, "example": 1}


def test_token_resolves_each_theme_and_exposes_reference(repo):
    result = browser(repo).detail("ttakkari", "token:color.action")
    assert result["light"]["resolved"] == "#123456"
    assert result["dark"]["resolved"] == "#abcdef"
    assert result["light"]["references"] == ["brand.blue"]
    assert result["light"]["source"] == "ttakkari/tokens/src/color.light.json"
    assert result["css_variable"] == "--tk-color-action"


def test_search_matches_values_descriptions_and_full_document(repo):
    b = browser(repo)
    assert b.search("ttakkari", query="사람의 행동", kind="token")["total"] == 1
    assert b.search("ttakkari", query="#abcdef", kind="token")["total"] == 1
    result = b.search("ttakkari", query="Blue", kind="document")
    assert result["total"] == 1 and "Blue" in result["items"][0]["excerpt"]


def test_pagination_is_bounded_and_revision_is_pinned(repo):
    b = browser(repo)
    first = b.search("ttakkari", limit=1)
    second = b.search("ttakkari", limit=1, cursor=first["next_cursor"], revision=first["revision"])
    assert first["items"][0]["id"] != second["items"][0]["id"]
    assert len(json.dumps(first, ensure_ascii=False)) < 16000
    with pytest.raises(ValueError):
        b.search("ttakkari", revision="HEAD~1")


def test_source_read_is_paginated_and_does_not_execute_html(repo):
    b = browser(repo)
    result = b.detail("ttakkari", "file:ttakkari/docs/00-guide.md", length=1000)
    rest = b.detail("ttakkari", result["id"], offset=result["next_offset"], length=1000, revision=result["revision"])
    original = (repo / result["source"]).read_text()
    assert result["content"] + rest["content"] == original[:2000]
    assert result["total_chars"] == len(original)
    assert "<script>" in b.detail("ttakkari", "file:ttakkari/examples/button.html")["content"]


@pytest.mark.parametrize("item", ["file:secret.env", "file:ttakkari/docs/secret-link.md", "file:../secret.env", "file:/etc/passwd"])
def test_rejects_unlisted_files_and_symlinks(repo, item):
    from ttakkari.design_system import NotFound
    with pytest.raises(NotFound):
        browser(repo).detail("ttakkari", item)


def test_cycle_and_missing_reference_are_explicit(repo):
    p = repo / "ttakkari/tokens/src/brand.json"
    p.write_text(json.dumps({"brand": {"blue": {"$value": "{brand.blue}"}}}))
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "cycle")
    result = browser(repo).detail("ttakkari", "token:color.action")
    assert result["light"]["resolved"] is None
    assert "cycle" in result["light"]["error"]


def test_unknown_system_and_missing_repo_are_explicit(repo, tmp_path):
    from ttakkari.design_system import NotFound, Unavailable
    with pytest.raises(NotFound):
        browser(repo).search("unknown")
    with pytest.raises(Unavailable):
        browser(tmp_path / "missing").catalog()


def test_large_document_title_cannot_stall_search_cursor(repo):
    (repo / "ttakkari/docs/00-guide.md").write_text("# " + "긴제목" * 6000 + "\n본문")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "long heading")
    result = browser(repo).search("ttakkari", kind="document", limit=1)
    assert len(result["items"]) == 1
    assert len(result["items"][0]["title"]) <= 200
    assert len(json.dumps(result, ensure_ascii=False)) < 16000
