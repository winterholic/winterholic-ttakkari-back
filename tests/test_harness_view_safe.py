"""하네스 조회: 경로 화이트리스트·심볼릭 링크 탈출 방지·비밀 마스킹(ADR 0069)."""
from __future__ import annotations

import json
import os

import pytest

from ttakkari.harness_view import safe
from ttakkari.harness_view.model import parse_frontmatter
from ttakkari.harness_view.safe import Denied, Root, SafeFiles

pytestmark = [pytest.mark.security]


@pytest.fixture
def tree(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "skills/alpha").mkdir(parents=True)
    (claude / "skills/alpha/SKILL.md").write_text("---\nname: alpha\ndescription: a\n---\nbody", encoding="utf-8")
    (claude / "CLAUDE.md").write_text("hello", encoding="utf-8")
    (claude / "notes.md").write_text("not whitelisted", encoding="utf-8")
    (claude / ".credentials.json").write_text('{"token": "CANARY-CRED"}', encoding="utf-8")
    (claude / "history.jsonl").write_text("CANARY-HISTORY", encoding="utf-8")
    (claude / ".claude.json").write_text(json.dumps({
        "mcpServers": {"s": {"command": "x"}}, "oauthAccount": {"emailAddress": "CANARY-ACCOUNT"}}), encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("CANARY-OUTSIDE", encoding="utf-8")
    (outside / "secret.txt").write_text("CANARY-SECRET", encoding="utf-8")
    return home, claude, outside


def files(claude):
    return SafeFiles([Root(claude, ("CLAUDE.md", ".credentials.json", "history.jsonl", ".claude.json", "skills/*/SKILL.md"))])


def test_reads_only_whitelisted_patterns(tree):
    _, claude, _ = tree
    f = files(claude)
    assert f.read_text(claude / "CLAUDE.md") == ("hello", False)
    assert f.read_text(claude / "skills/alpha/SKILL.md")[0].startswith("---")
    with pytest.raises(Denied):
        f.read_text(claude / "notes.md")
    with pytest.raises(Denied):
        f.read_text(claude / "skills/alpha/../../notes.md")


def test_pattern_star_does_not_cross_directories(tree):
    _, claude, _ = tree
    (claude / "skills/alpha/deep").mkdir()
    (claude / "skills/alpha/deep/SKILL.md").write_text("deep", encoding="utf-8")
    with pytest.raises(Denied):
        files(claude).read_text(claude / "skills/alpha/deep/SKILL.md")


@pytest.mark.parametrize("name", [".credentials.json", "history.jsonl"])
def test_secret_names_are_never_read_even_if_pattern_allows(tree, name):
    _, claude, _ = tree
    with pytest.raises(Denied, match="비밀"):
        files(claude).read_text(claude / name)


def test_symlink_escape_outside_roots_is_denied(tree):
    _, claude, outside = tree
    (claude / "skills/evil").symlink_to(outside, target_is_directory=True)
    with pytest.raises(Denied, match="허용 범위 밖"):
        files(claude).read_text(claude / "skills/evil/SKILL.md")
    (claude / "skills/leak").mkdir()
    (claude / "skills/leak/SKILL.md").symlink_to(outside / "secret.txt")
    with pytest.raises(Denied):
        files(claude).read_text(claude / "skills/leak/SKILL.md")


def test_symlink_to_secret_inside_root_is_denied_by_pattern(tree):
    _, claude, _ = tree
    (claude / "skills/steal").mkdir()
    (claude / "skills/steal/SKILL.md").symlink_to(claude / "notes.md")
    with pytest.raises(Denied):
        files(claude).read_text(claude / "skills/steal/SKILL.md")


def test_symlink_inside_allowed_roots_is_followed(tree, tmp_path):
    _, claude, _ = tree
    agents = tmp_path / "home/.agents"
    (agents / "skills/shared").mkdir(parents=True)
    (agents / "skills/shared/SKILL.md").write_text("shared", encoding="utf-8")
    (claude / "skills/shared").symlink_to(agents / "skills/shared", target_is_directory=True)
    f = SafeFiles([Root(claude, ("skills/*/SKILL.md",)), Root(agents, ("skills/*/SKILL.md",))])
    assert f.read_text(claude / "skills/shared/SKILL.md")[0] == "shared"


def test_directory_and_missing_are_denied(tree):
    _, claude, _ = tree
    with pytest.raises(Denied):
        files(claude).read_text(claude / "skills/alpha")
    with pytest.raises(Denied):
        files(claude).read_text(claude / "skills/none/SKILL.md")


def test_oversized_file_is_truncated(tree):
    _, claude, _ = tree
    (claude / "CLAUDE.md").write_text("x" * 5000, encoding="utf-8")
    text, truncated = files(claude).read_text(claude / "CLAUDE.md", limit=1000)
    assert len(text) == 1000 and truncated


def test_claude_json_only_exposes_requested_keys(tree):
    _, claude, _ = tree
    f = files(claude)
    with pytest.raises(Denied):
        f.read_text(claude / ".claude.json")
    got = f.read_json_subset(claude / ".claude.json", ("mcpServers",))
    assert got == {"mcpServers": {"s": {"command": "x"}}}
    assert "CANARY-ACCOUNT" not in json.dumps(got)


def test_exists_does_not_probe_outside_roots(tree):
    _, claude, outside = tree
    f = files(claude)
    assert f.exists(claude / "CLAUDE.md") is True
    assert f.exists(claude / "missing.md") is False
    assert f.exists(outside / "secret.txt") is None
    assert f.exists("/etc/passwd") is None


def test_mask_config_hides_env_headers_and_secret_keys():
    data = {
        "env": {"GITHUB_TOKEN": "CANARY-1", "PLAIN": "CANARY-2"},
        "mcpServers": {"s": {"command": "npx", "args": ["-y", "pkg", "--api-key", "CANARY-3", "--token=CANARY-4"],
                             "headers": {"Authorization": "Bearer CANARY-5"}, "env": {"K": "CANARY-6"},
                             "bearer_token": "CANARY-7", "url": "https://user:CANARY-8@host/x"}},
        "statusLine": {"command": "bash x.sh", "refreshInterval": 5},
        "max_tokens": 100,
    }
    masked = safe.mask_config(data)
    text = json.dumps(masked)
    for n in range(1, 9):
        assert f"CANARY-{n}" not in text
    assert set(masked["env"]) == {"GITHUB_TOKEN", "PLAIN"}  # 키 이름은 남긴다
    assert masked["statusLine"]["refreshInterval"] == 5
    assert masked["max_tokens"] == 100
    assert masked["mcpServers"]["s"]["command"] == "npx"
    assert safe.count_masked(data, masked) >= 6


def test_mask_text_hides_secret_shaped_values_but_keeps_prose():
    token = "ghp_" + "A1b2C3d4E5" * 3
    body = (f"token: {token}\nAPI_KEY=abcd1234efgh5678ijkl\nAuthorization: Bearer abc.def.ghi\n"
            "Basic Authentication 설명, token: 만료 처리, 인증 토큰 보관은 dev-auth 가 맡는다.\n"
            "-----BEGIN PRIVATE KEY-----\nMIIB\n-----END PRIVATE KEY-----\n")
    masked, count = safe.mask_text(body)
    assert token not in masked and "abcd1234efgh5678ijkl" not in masked and "MIIB" not in masked
    assert "abc.def.ghi" not in masked
    assert "Basic Authentication 설명" in masked and "token: 만료 처리" in masked and "dev-auth 가 맡는다" in masked
    assert count >= 4


def test_mask_text_is_idempotent():
    once, _ = safe.mask_text("API_KEY=abcd1234efgh5678ijkl")
    assert safe.mask_text(once)[0] == once


def test_parse_frontmatter_handles_colons_blocks_and_quotes():
    meta, body = parse_frontmatter(
        '---\nname: x\ndescription: "한글: 콜론 포함"\nlong: >\n  첫 줄\n  둘째 줄\nargument-hint: a: b\n---\n본문')
    assert meta["name"] == "x" and meta["description"] == "한글: 콜론 포함"
    assert meta["long"] == "첫 줄 둘째 줄" and meta["argument-hint"] == "a: b" and body == "본문"
    assert parse_frontmatter("no front matter") == ({}, "no front matter")


def test_realpath_of_root_symlink_is_used(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "CLAUDE.md").write_text("ok", encoding="utf-8")
    link = tmp_path / "link"
    os.symlink(real, link)
    assert SafeFiles([Root(link, ("CLAUDE.md",))]).read_text(link / "CLAUDE.md")[0] == "ok"


def test_home_root_reads_only_claude_json_and_never_probes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"s": {}}, "oauthAccount": {"e": "CANARY-ACCT"}}), encoding="utf-8")
    (home / "other.md").write_text("CANARY-OTHER", encoding="utf-8")
    f = SafeFiles([Root(home, (".claude.json",), probe=False)])
    assert f.read_json_subset(home / ".claude.json", ("mcpServers",)) == {"mcpServers": {"s": {}}}
    with pytest.raises(Denied):
        f.read_text(home / ".claude.json")
    with pytest.raises(Denied):
        f.read_text(home / "other.md")
    assert f.exists(home / "other.md") is None and f.exists(home / "missing.md") is None
