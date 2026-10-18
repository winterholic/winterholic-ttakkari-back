"""입력 검증·URL 해석(ADR 0042 §7). 사용자 입력은 fetch 하지 않고 고정 필드로만 바뀐다."""
from __future__ import annotations

import pytest

from ttakkari.github import targets
from ttakkari.github.errors import GithubError


def code(fn, *a, **k) -> str:
    with pytest.raises(GithubError) as e:
        fn(*a, **k)
    return e.value.code


@pytest.mark.parametrize("v", ["octo", "a", "a-b", "A1", "x" * 39])
def test_owner_ok(v):
    assert targets.owner(v) == v


@pytest.mark.parametrize("v", ["", "-a", "a b", "a/b", "a..b", "..", "x" * 40, "a@b", "a%2fb", "한글", "a\n", "a.b"])
def test_owner_rejected(v):
    assert code(targets.owner, v) == "invalid_target"


@pytest.mark.parametrize("v", ["repo", "re.po", "re_po-1", ".github", "x" * 100])
def test_repo_ok(v):
    assert targets.repo(v) == v


@pytest.mark.parametrize("v", ["", ".", "..", "a/b", "a b", "x" * 101, "r.git", "a\x00", "a%2e%2e"])
def test_repo_rejected(v):
    assert code(targets.repo, v) == "invalid_target"


def test_numbers_and_ids():
    assert targets.number("7") == 7 and targets.number(2**31 - 1) == 2**31 - 1
    for bad in ("0", "-1", "abc", "1.5", str(2**31), "", "1e3", " 1"):
        assert code(targets.number, bad) == "invalid_target"
    assert targets.big_id("12345678901") == 12345678901
    for bad in ("0", "-5", "x", str(2**53 + 1)):
        assert code(targets.big_id, bad) == "invalid_target"


@pytest.mark.parametrize("p,want", [("", ""), ("src/a.py", "src/a.py"), ("src/한글 파일.md", "src/한글 파일.md"),
                                     ("a/b/", "a/b"), ("%.txt", "%.txt"), (".github/workflows/ci.yml", ".github/workflows/ci.yml")])
def test_path_ok(p, want):
    assert targets.path(p) == want


@pytest.mark.parametrize("p", ["../x", "a/../b", "a/./b", "/etc/passwd", "a//b", "a\\b", "a\x00b", "a\nb", "x" * 1025,
                               "%2e%2e/x", "a/%2E%2E/b", "a%00b", "a%2f..%2fb".replace("%2f", "/")])
def test_path_rejected(p):
    assert code(targets.path, p) == "invalid_target"


def test_quote_path_encodes_each_segment():
    assert targets.quote_path("src/a b#c?.py") == "src/a%20b%23c%3F.py"
    assert targets.quote_path("") == ""
    assert targets.quote_path("100%/x") == "100%25/x"


@pytest.mark.parametrize("r", ["main", "feature/login-ui", "v1.2.3", "a" * 40, "release/2026-10"])
def test_ref_ok(r):
    assert targets.ref(r) == r


@pytest.mark.parametrize("r", ["", "-evil", "a..b", "a b", "a~1", "a^", "a:b", "a?", "a*", "a[", "a\\b", "a@{1}", "a\x00", "x" * 256, "a\n"])
def test_ref_rejected(r):
    assert code(targets.ref, r) == "invalid_target"


def test_labels_csv():
    assert targets.labels("bug,ui") == "bug,ui"
    assert targets.labels(None) is None
    for bad in ("a" * 51, ",".join(f"l{i}" for i in range(11)), "a\nb", "bug,,ui"):
        assert code(targets.labels, bad) == "invalid_target"


# ---- resolve (순수 파싱) ----

def P(q):
    return targets.parse(q).model_dump(exclude_none=True)


def test_shorthand_and_urls():
    assert P("octo/hello") == {"kind": "repo", "owner": "octo", "repo": "hello", "needs_ref_resolution": False}
    assert P("  octo/hello.git ") == {"kind": "repo", "owner": "octo", "repo": "hello", "needs_ref_resolution": False}
    assert P("octo") == {"kind": "owner", "owner": "octo", "needs_ref_resolution": False}
    assert P("https://github.com/octo/hello") == P("octo/hello")
    assert P("https://www.github.com/octo/hello/") == P("octo/hello")
    assert P("github.com/octo/hello") == P("octo/hello")
    assert P("https://github.com/orgs/acme/repositories")["kind"] == "owner"
    assert P("https://github.com/octo/hello?tab=readme-ov-file#readme") == P("octo/hello")


def test_pr_issue_actions_urls():
    assert P("https://github.com/o/r/pulls") == {"kind": "pulls", "owner": "o", "repo": "r", "needs_ref_resolution": False}
    assert P("https://github.com/o/r/pull/12") == {"kind": "pull", "owner": "o", "repo": "r", "number": 12, "needs_ref_resolution": False}
    assert P("https://github.com/o/r/pull/12/files")["tab"] == "files"
    assert P("https://github.com/o/r/pull/12/commits")["kind"] == "pull" and "tab" not in P("https://github.com/o/r/pull/12/commits")
    assert P("o/r/issues")["kind"] == "issues"
    assert P("https://github.com/o/r/issues/5")["number"] == 5 and P("https://github.com/o/r/issues/5")["kind"] == "issue"
    assert P("https://github.com/o/r/actions")["kind"] == "actions"
    assert P("https://github.com/o/r/actions/workflows/ci.yml")["kind"] == "actions"
    r = P("https://github.com/o/r/actions/runs/123456789")
    assert r["kind"] == "run" and r["run_id"] == 123456789
    j = P("https://github.com/o/r/actions/runs/123456789/job/987")
    assert j["kind"] == "job" and j["job_id"] == 987 and j["run_id"] == 123456789
    assert P("https://github.com/o/r/branches")["kind"] == "repo"  # 지원 밖 섹션은 저장소 개요로


def test_tree_blob_needs_ref_resolution_without_account():
    t = P("https://github.com/o/r/tree/main/src/ui")
    assert t["kind"] == "code" and t["needs_ref_resolution"] is True and t["ref_path"] == "main/src/ui" and t["is_file"] is False
    assert "ref" not in t
    b = P("https://github.com/o/r/blob/feature/x/src/a.py#L10")
    assert b["is_file"] is True and b["ref_path"] == "feature/x/src/a.py"
    assert P("https://github.com/o/r/tree")["kind"] == "repo"  # ref 없는 tree


@pytest.mark.parametrize("q", [
    "http://github.com/o/r", "ftp://github.com/o/r", "file:///etc/passwd", "git@github.com:o/r.git", "ssh://git@github.com/o/r",
    "https://user:pw@github.com/o/r", "https://github.com:8443/o/r", "https://github.com@evil.com/o/r",
    "//evil.com/o/r", "https://github.com\\@evil.com/o/r", "https://github.com/o/r\x00", "https://github.com/o/ r",
    "https://github.com/../o/r", "https://github.com/o/r/blob/main/../../x", "https://github.com/o//r",
    "https://github.com/%2e%2e/r", "https://github.com/o/%2e%2e", "x" * 2049, "", "   ", "https://github.com/", "https://github.com",
    "https://github.com/o/r/pull/0", "https://github.com/o/r/pull/abc", "https://github.com/o/r/issues/new",
    "https://github.com/settings/profile", "https://github.com/pulls", "https://github.com/o/r/actions/runs/x",
])
def test_resolve_rejects_hostile_or_malformed(q):
    assert code(targets.parse, q) in ("invalid_target", "unsupported_host")


@pytest.mark.parametrize("q", [
    "https://evil.com/o/r", "https://github.com.evil.com/o/r", "https://gist.github.com/o/abc", "https://raw.githubusercontent.com/o/r/main/a",
    "https://api.github.com/repos/o/r", "https://127.0.0.1/o/r", "https://[::1]/o/r", "https://github.example.com/o/r",
    "github.com.evil.com/o/r", "example.com/o/r", "https://GITHUB.COM.evil.com/o/r", "https://githubcom/o/r".replace("githubcom", "git-hub.com"),
])
def test_resolve_other_hosts_are_unsupported_host(q):
    assert code(targets.parse, q) == "unsupported_host"


def test_resolve_host_case_insensitive():
    assert P("https://GitHub.com/O/R")["owner"] == "O"


def test_split_ref_path_uses_real_ref_names():
    refs = ["feature", "feature/login-ui", "feature-x", "main"]
    assert targets.split_ref_path("feature/login-ui/src/a.py", refs) == ("feature/login-ui", "src/a.py")
    assert targets.split_ref_path("feature/login-ui", refs) == ("feature/login-ui", "")
    assert targets.split_ref_path("feature/other/a.py", refs) == ("feature", "other/a.py")  # 가장 긴 실제 브랜치
    assert targets.split_ref_path("main/feature/x.py", refs) == ("main", "feature/x.py")
    sha = "a" * 40
    assert targets.split_ref_path(f"{sha}/src", []) == (sha, "src")
    assert targets.split_ref_path("nope/src", refs) is None
    assert targets.split_ref_path("feature-xy/a", refs) is None  # 세그먼트 경계가 아니면 일치하지 않는다
