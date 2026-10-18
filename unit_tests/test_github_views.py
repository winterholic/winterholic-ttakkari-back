"""상류 JSON → 응답 모델 매핑(화이트리스트). 골든 fixture 는 tests/fixtures/github/api/*.json — 프론트가 같은 파일을 쓴다.

골든 갱신: GITHUB_FIXTURES_UPDATE=1 pytest unit_tests/test_github_views.py
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ttakkari.github import views
from ttakkari.github.errors import GithubError

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "github"


def up(name: str):
    return json.loads((FIX / "upstream" / f"{name}.json").read_text(encoding="utf-8"))


def golden(name: str, model) -> None:
    got = json.loads(json.dumps(model.model_dump(mode="json") if hasattr(model, "model_dump")
                                else [m.model_dump(mode="json") for m in model], ensure_ascii=False))
    path = FIX / "api" / f"{name}.json"
    if os.environ.get("GITHUB_FIXTURES_UPDATE") == "1":
        path.write_text(json.dumps(got, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8")) == got, f"golden {name} 불일치"


def test_no_upstream_only_fields_leak():
    """응답 어디에도 상류 전용 키·값(토큰 흉내, URL 템플릿, node_id, 이메일)이 없다."""
    outs = [
        views.view_repo(up("repo")), views.view_profile(up("user"), viewer_login="octo"),
        views.view_pull_detail(up("pull")), views.view_issue_detail(up("issue")), views.view_run_detail(up("run")),
    ]
    blob = json.dumps([o.model_dump(mode="json") for o in outs], ensure_ascii=False)
    for needle in ("MUST_NOT_LEAK", "node_id", "clone_url", "billing_email", "two_factor", "_links", "x@y", "api.github.com"):
        assert needle not in blob, needle


def test_profile_and_org():
    me = views.view_profile(up("user"), viewer_login="octo")
    assert me.is_viewer and me.total_private_repos == 3 and me.public_repos == 12
    golden("me", me)
    org = views.view_profile(up("owner_org_user"), viewer_login="octo", org=up("org"))
    assert org.type == "Organization" and org.description == "We make things" and not org.is_viewer
    assert org.name == "Acme Inc" and org.blog is None  # 빈 문자열은 None
    golden("owner_org", org)
    golden("orgs", [views.view_org_ref(o) for o in up("orgs")])


def test_repo_summary_and_list():
    r = views.view_repo(up("repo"))
    assert r.full_name == "acme/widget" and r.license == "MIT License" and r.topics == ["cli", "python"]
    assert r.permissions and r.permissions.push and not r.permissions.admin and r.homepage is None
    golden("repo", r)
    rows = [views.view_repo(x) for x in up("repos")]
    assert rows[1].permissions is None and rows[1].license is None
    golden("repos", rows)


def test_branches_mark_default():
    rows = views.view_branches(up("branches"), default_branch="main")
    assert [(b.name, b.is_default, b.protected) for b in rows] == [("main", True, True), ("feature/login-ui", False, False)]
    golden("branches", rows)


def test_contents_dir_sorted_dirs_first_and_limit_flag():
    out = views.view_contents(up("contents_dir"), path="src", ref="main")
    assert [e.name for e in out.entries] == ["Util", "alpha.py", "link", "vendor", "zeta.py"]
    assert [e.type for e in out.entries] == ["dir", "file", "symlink", "submodule", "file"]
    assert out.kind == "dir" and not out.entries_limit_hit
    golden("contents_dir", out)
    big = {"type": "dir", "entries": [{"type": "file", "name": f"f{i}", "path": f"f{i}", "size": 1, "sha": "a"} for i in range(1000)]}
    assert views.view_contents(big, path="", ref=None).entries_limit_hit is True
    # 배열 형태(object 미디어 타입이 아닌 응답)도 같은 결과
    assert views.view_contents(up("contents_dir")["entries"], path="src", ref="main").kind == "dir"


def test_contents_file_text_binary_invalid_huge_symlink_submodule():
    f = views.view_contents(up("contents_file"), path="src/main.py", ref="main")
    assert f.kind == "file" and f.file.text == "print('안녕')\nprint(2)\n" and f.file.line_count == 2
    assert not (f.file.binary or f.file.too_large or f.file.truncated)
    golden("contents_file", f)
    b = views.view_contents(up("contents_file_binary"), path="logo.png", ref=None)
    assert b.file.binary is True and b.file.text is None
    golden("contents_file_binary", b)
    # UTF-8 이 아니면 깨진 글자로 성공시키지 않고 binary 로 표시한다
    assert views.view_contents(up("contents_file_invalid_utf8"), path="latin.txt", ref=None).file.binary is True
    huge = views.view_contents(up("contents_file_huge"), path="big.log", ref=None)
    assert huge.file.too_large is True and huge.file.text is None and huge.file.size == 5 * 1024 * 1024
    golden("contents_file_huge", huge)
    s = views.view_contents(up("contents_symlink"), path="src/link", ref=None)
    assert s.kind == "symlink" and s.symlink_target == "../README.md" and s.file is None
    golden("contents_symlink", s)
    m = views.view_contents(up("contents_submodule"), path="src/vendor", ref=None)
    assert m.kind == "submodule" and m.submodule.git_url == "https://github.com/acme/vendor-lib.git" and m.submodule.sha == "3" * 40
    golden("contents_submodule", m)


def test_file_from_raw_prefix_marks_truncated():
    base = views.view_contents(up("contents_file_huge"), path="big.log", ref=None).file
    prefix = ("line\n" * 100).encode()
    out = views.file_from_bytes(base, prefix, truncated=True)
    assert out.truncated and out.too_large and out.text.startswith("line\n") and out.size == 5 * 1024 * 1024
    # 잘린 경계가 UTF-8 한 글자 중간이면 그 글자만 버린다(binary 오판 금지)
    cut = ("가" * 10).encode()[:-1]
    assert views.file_from_bytes(base, cut, truncated=True).binary is False
    assert views.file_from_bytes(base, b"\x00\x01\x02", truncated=True).binary is True


def test_readme():
    out = views.view_contents(up("readme"), path="README.md", ref=None)
    assert out.file.text == "# Hello\n"
    golden("readme", out)


def test_pulls():
    rows = [views.view_pull_summary(p) for p in up("pulls")]
    assert rows[0].head.ref == "feature/login-ui" and rows[0].head.repo_full_name == "acme/widget"
    assert rows[1].merged is True and rows[1].head.repo_full_name is None  # 삭제된 포크
    golden("pulls", rows)
    d = views.view_pull_detail(up("pull"))
    assert d.mergeable is None and d.mergeable_computing is True and d.requested_reviewers[0].login == "rev"
    golden("pull", d)
    closed = up("pull") | {"state": "closed", "mergeable": None}
    assert views.view_pull_detail(closed).mergeable_computing is False
    long = up("pull") | {"body": "x" * 70000}
    out = views.view_pull_detail(long)
    assert out.body_truncated is True and len(out.body) <= 65536 + 1


def test_pull_files_patch_states():
    rows = [views.view_pull_file(f) for f in up("pull_files")]
    assert [r.patch_state for r in rows] == ["ok", "empty", "unavailable", "unavailable", "truncated"]
    assert rows[1].previous_filename == "docs/old-name.md" and rows[0].patch.startswith("@@")
    assert rows[2].patch is None
    assert len(rows[4].patch) <= 256 * 1024 + 1
    golden("pull_files", [r.model_copy(update={"patch": (r.patch[:60] + "…") if r.patch_state == "truncated" else r.patch}) for r in rows])


def test_issues_filter_pulls_and_detail_kind():
    issues = views.view_issue_list(up("issues"))
    assert [i.number for i in issues] == [3, 2]  # PR(#7) 제거
    golden("issues", issues)
    d = views.view_issue_detail(up("issue"))
    assert d.kind == "issue" and d.milestone == "v1" and d.assignees[0].login == "rev" and d.author_association == "OWNER"
    golden("issue", d)
    p = views.view_issue_detail(up("issue_as_pull"))
    assert p.kind == "pull" and p.body is None  # PR 본문은 이 경로로 내보내지 않는다
    golden("issue_as_pull", p)


def test_comments():
    rows = [views.view_comment(c) for c in up("issue_comments")]
    assert rows[0].body == "me too" and rows[1].body is None and rows[1].user is None
    golden("issue_comments", rows)


def test_actions():
    runs = [views.view_run_summary(r) for r in up("runs")["workflow_runs"]]
    assert runs[0].conclusion == "failure" and runs[1].conclusion is None
    golden("runs", runs)
    d = views.view_run_detail(up("run"))
    assert d.head_commit_message == "Merge pull request #7" and d.triggering_actor.login == "rev"
    golden("run", d)
    jobs = [views.view_job(j) for j in up("jobs")["jobs"]]
    assert jobs[0].first_failed_step == 2 and jobs[1].first_failed_step is None
    golden("jobs", jobs)


@pytest.mark.parametrize("fn,raw", [
    (views.view_repo, []), (views.view_repo, {"name": "x"}), (views.view_pull_summary, {"title": "no number"}),
    (views.view_run_summary, {"id": "not-int"}), (views.view_job, {"id": 1}),
])
def test_schema_drift_is_reported_not_swallowed(fn, raw):
    with pytest.raises(GithubError) as e:
        fn(raw)
    assert e.value.code == "gh_schema_changed" and e.value.status == 502


def test_avatar_only_from_github_cdn():
    raw = up("user") | {"avatar_url": "https://evil.example/a.png"}
    assert views.view_profile(raw, viewer_login=None).avatar_url is None
