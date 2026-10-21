"""Immutable design-system browsing, without a production database."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
from git_isolation import isolated_git_env, scrub_process_env

# Browser 가 부르는 git 도 훅의 GIT_DIR 같은 호출 환경을 받지 않게 한다.
scrub_process_env()


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, env=isolated_git_env()).strip()


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


@pytest.fixture
def preview_repo(repo):
    files = {
        'ttakkari/dist/tokens.css': ':root { --sample: #123456; }',
        'ttakkari/examples/examples.css': '.hero { background: url(../assets/brand/mark.svg?v=1); }',
        'ttakkari/examples/icons.js': 'window.previewReady = true;',
        'ttakkari/assets/brand/mark.svg': '<svg xmlns="http://www.w3.org/2000/svg"/>',
        'ttakkari/examples/preview.html': '''<!doctype html><html><head>
        <link rel="stylesheet" href="../dist/tokens.css"><link rel="stylesheet" href="examples.css">
        <script src="icons.js" defer></script></head><body><h2 id="color">색상</h2>
        <img src="../assets/brand/mark.svg?v=1"><a href="#color">색</a>
        <a href="https://evil.invalid">외부</a><iframe src="https://evil.invalid"></iframe>
        <script>document.body.dataset.ready = 'yes';</script></body></html>''',
    }
    for path, content in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'preview fixture')
    return repo


def test_preview_bundles_committed_resources_and_sections(preview_repo):
    b = browser(preview_repo)
    revision = b.catalog()['revision']
    (preview_repo / 'ttakkari/dist/tokens.css').write_text('dirty')
    result = b.preview('ttakkari', revision=revision, theme='dark')
    assert result['revision'] == revision
    assert 'data:text/css;base64,' in result['html']
    assert 'data:image/svg+xml;base64,' in result['html']
    assert 'data:text/javascript;base64,' in result['html']
    assert 'dataset.ready' in result['html']
    assert 'data-theme="dark"' in result['html']
    assert result['sections'] == [{'id': 'color', 'title': '색상'}]
    assert result['warnings'] == []
    import html
    assert "connect-src 'none'" in html.unescape(result['html'])
    assert "base-uri 'none'" in html.unescape(result['html'])
    assert '<iframe' not in result['html']
    assert 'href="https://evil.invalid"' not in result['html']
    assert 'href="#color"' in result['html']


@pytest.mark.parametrize('href', ['../../secret.env', '%2e%2e/%2e%2e/secret.env', '../assets/brand/link.svg', '../missing.css'])
def test_preview_never_reads_outside_allowlist_or_symlinks(preview_repo, href):
    (preview_repo / 'ttakkari/assets/brand/link.svg').symlink_to('/etc/passwd')
    (preview_repo / 'ttakkari/examples/preview.html').write_text(f'<html><head><link rel="stylesheet" href="{href}"></head><body><img src="{href}"></body></html>')
    git(preview_repo, 'add', '.')
    git(preview_repo, 'commit', '-qm', 'unsafe paths')
    result = browser(preview_repo).preview('ttakkari')
    assert 'SECRET=never' not in result['html']
    assert 'root:' not in result['html']
    assert result['warnings']


def test_preview_requires_an_example_and_checks_theme(preview_repo):
    from ttakkari.design_system import NotFound
    b = browser(preview_repo)
    with pytest.raises(NotFound):
        b.preview('ttakkari', item='file:ttakkari/docs/00-guide.md')
    with pytest.raises(ValueError):
        b.preview('ttakkari', theme='bad')


def test_preview_assets_use_pinned_revision(preview_repo):
    import base64
    b = browser(preview_repo)
    old = b.catalog()['revision']
    (preview_repo / 'ttakkari/dist/tokens.css').write_text('NEW-CSS')
    git(preview_repo, 'add', '.')
    git(preview_repo, 'commit', '-qm', 'new css')
    assert base64.b64encode(b'NEW-CSS').decode() not in b.preview('ttakkari', revision=old)['html']
    assert base64.b64encode(b'NEW-CSS').decode() in b.preview('ttakkari')['html']


def test_token_search_carries_visual_values(repo):
    row = browser(repo).search('ttakkari', kind='token', query='color.action')['items'][0]
    assert row['values'] == {'light': '#123456', 'dark': '#abcdef'}


def test_example_titles_come_from_html_title(preview_repo):
    (preview_repo / 'ttakkari/examples/preview.html').write_text('<html><head><title>색상 &amp; 컴포넌트 미리보기</title></head><body>견본</body></html>')
    git(preview_repo, 'add', '.')
    git(preview_repo, 'commit', '-qm', 'html title')
    rows = browser(preview_repo).search('ttakkari', kind='example')['items']
    assert next(r for r in rows if r['source'].endswith('preview.html'))['title'] == '색상 & 컴포넌트 미리보기'


def test_preview_asset_limits_are_explicit(preview_repo, monkeypatch):
    import ttakkari.design_preview as module
    from ttakkari.design_system import Unavailable
    monkeypatch.setattr(module, 'MAX_FILE', 1)
    with pytest.raises(Unavailable, match='한도'):
        browser(preview_repo).preview('ttakkari')


def test_preview_css_cycles_are_reported(preview_repo):
    (preview_repo / 'ttakkari/dist/tokens.css').write_text('@import "../examples/examples.css";')
    (preview_repo / 'ttakkari/examples/examples.css').write_text('@import "../dist/tokens.css";')
    git(preview_repo, 'add', '.')
    git(preview_repo, 'commit', '-qm', 'css cycle')
    assert browser(preview_repo).preview('ttakkari')['warnings']


def test_preview_restores_selected_theme_after_original_initializers(preview_repo):
    result = browser(preview_repo).preview('ttakkari', theme='dark')
    assert "addEventListener('load'" in result['html']
    assert 'document.documentElement.dataset.theme="dark"' in result['html']
