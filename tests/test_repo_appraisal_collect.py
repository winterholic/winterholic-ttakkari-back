"""수집기: 가짜 GitHub 상류로 수집·표본·하네스 발견·202 처리·요청 예산을 확인한다."""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit_tests"))
from github_fakes import FakeGh, ok

from ttakkari.github import service as svc_mod
from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.client import GithubClient
from ttakkari.github.errors import GithubError
from ttakkari.github.logs import LogFetcher
from ttakkari.repo_appraisal import collect as collect_mod

NOW = datetime.now(UTC)
FILES = {
    "README.md": ("# widget\n" + "설명 " * 300),
    "pyproject.toml": '[project]\nname="widget"\n',
    "src/acme/__init__.py": "",
    "src/acme/core.py": "x = 1\n" * 2000,
    "src/acme/main.py": "def main():\n    print(1)\n",
    "src/acme/util.py": "y = 2\n",
    "tests/test_core.py": "def test_x():\n    assert True\n",
    "docs/guide.md": "# 안내\n",
    ".github/workflows/ci.yml": "on: push\n",
    "AGENTS.md": "# 지침\n짧다\n",
    ".claude/skills/a/SKILL.md": "# 스킬\n",
    "notes.txt": "메모\n",
    "uv.lock": "lock\n",
    "ruff.toml": "[lint]\n",
    "mypy.ini": "[mypy]\n",
    ".editorconfig": "root=true\n",
}
TREE = [{"path": p, "size": len(t.encode()), "type": "blob"} for p, t in FILES.items()]


def _commits(n=12, step_days=20):
    return [{"sha": f"{i:040x}"[-40:],
             "commit": {"author": {"date": (NOW - timedelta(days=i * step_days)).isoformat()}},
             "author": {"login": "alice"}} for i in range(n)]


def _state(**over):
    state = {"commits": _commits(), "tree": list(TREE), "truncated": False, "stats_status": [200],
             "contributors_status": 200, "readme_status": 200, "empty": False,
             "calls": [], "sleeps": []}
    state.update(over)
    return state


def _handler(state):
    def handle(req: httpx.Request) -> httpx.Response:
        state["calls"].append(req.url.path)
        p = req.url.path
        if p == "/repos/acme/widget":
            return ok({"default_branch": "main", "license": {"key": "mit"},
                       "open_issues_count": 3, "description": "위젯",
                       "language": "Python", "archived": False, "fork": False,
                       "created_at": "2024-01-01T00:00:00Z"})
        if p == "/repos/acme/widget/git/trees/main":
            if state["empty"]:
                return ok({"message": "Git Repository is empty."}, status=409)
            return ok({"tree": state["tree"], "truncated": state["truncated"]})
        if p == "/repos/acme/widget/readme":
            if state["readme_status"] != 200:
                return ok({"message": "Not Found"}, status=404)
            return ok(FILES["README.md"].encode())
        if p == "/repos/acme/widget/commits":
            return ok(state["commits"])
        if p == "/repos/acme/widget/contributors":
            if state["contributors_status"] != 200:
                return ok({"message": "boom"}, status=state["contributors_status"])
            return ok([{"login": "alice", "contributions": 90},
                       {"login": "bob", "contributions": 10}])
        if p == "/repos/acme/widget/releases":
            return ok([{"tag_name": "v1.0",
                        "published_at": (NOW - timedelta(days=30)).isoformat(),
                        "draft": False, "prerelease": False}])
        if p == "/repos/acme/widget/issues":
            rows = [{"number": i, "state": "closed"} for i in range(1, 9)]
            rows += [{"number": 9, "state": "open"}, {"number": 10, "state": "open"},
                     {"number": 11, "state": "closed", "pull_request": {"url": "x"}}]
            return ok(rows)
        if p == "/repos/acme/widget/stats/commit_activity":
            code = state["stats_status"][0] if len(state["stats_status"]) == 1 else state["stats_status"].pop(0)
            if code == 202:
                return ok(b"", status=202)
            week = int((NOW - timedelta(days=400)).timestamp())
            return ok([{"week": week + 7 * 86400 * w, "total": 3 if w % 4 else 0,
                        "days": [0] * 7} for w in range(52)])
        if p.startswith("/repos/acme/widget/contents/"):
            name = p.split("/contents/", 1)[1]
            if name not in FILES:
                return ok({"message": "Not Found"}, status=404)
            return ok(FILES[name].encode())
        raise AssertionError(f"예상 밖 호출: {p}")
    return handle


def _svc(state):
    gh = FakeGh([("alice", "success")])
    reg = AccountRegistry(runner=gh, gh_bin="gh")
    client = GithubClient(reg, transport=httpx.MockTransport(_handler(state)))
    return svc_mod.GithubService(registry=reg, client=client, logs=LogFetcher(client))


async def _sleep(state, seconds):
    state["sleeps"].append(seconds)


async def test_collects_facts_and_head_sha():
    state = _state()
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    assert got.head_sha == "0" * 39 + "0" or len(got.head_sha) == 40
    assert got.facts["repo_meta"]["license"] == "mit"
    assert len(got.facts["tree"]) == len(FILES) and not got.tree_truncated
    assert got.facts["readme_text"].startswith("# widget")
    assert len(got.facts["commits"]) == 12
    assert got.facts["contributors"][0]["login"] == "alice"
    assert got.facts["workflows"] == [".github/workflows/ci.yml"]
    assert got.facts["issues_sample"] and got.facts["pulls_sample"]
    assert all("pull_request" not in i for i in got.facts["issues_sample"])
    assert got.unavailable == []


async def test_quick_request_budget_within_fifteen():
    state = _state()
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    assert len(state["calls"]) <= 15, state["calls"]
    assert got.requests == len(state["calls"])


async def test_sample_selection_prefers_largest_entry_and_test():
    state = _state()
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    samples = got.facts["samples"]
    assert "src/acme/core.py" in samples  # 가장 큰 소스
    assert "src/acme/main.py" in samples  # 진입점
    assert "tests/test_core.py" in samples  # 테스트 1개
    assert len(samples) <= 5


async def test_harness_discovery_exact_and_prefix():
    state = _state()
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    assert sorted(got.facts["harness_found"]) == [".claude/skills/a/SKILL.md", "AGENTS.md"]
    assert set(got.facts["harness_texts"]) <= set(got.facts["harness_found"])
    assert "notes.txt" not in got.facts["harness_found"]


async def test_deep_fetches_more_samples_than_quick():
    quick = await collect_mod.collect(_svc(_state()), "alice", "acme", "widget", "quick")
    deep = await collect_mod.collect(_svc(_state()), "alice", "acme", "widget", "deep")
    assert len(deep.facts["samples"]) >= len(quick.facts["samples"])
    assert len(deep.facts["harness_texts"]) >= len(quick.facts["harness_texts"])


async def test_truncated_tree_flagged():
    state = _state(truncated=True)
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    assert got.tree_truncated


async def test_stats_202_retried_then_unavailable():
    state = _state(commits=_commits(100, 3), stats_status=[202, 202, 202])
    svc = _svc(state)
    got = await collect_mod.collect(svc, "alice", "acme", "widget", "quick",
                                    sleep=lambda s: _sleep(state, s))
    assert got.facts.get("stats_months") is None
    assert "stats_months" in got.unavailable
    assert len(state["sleeps"]) == 2  # 짧게 2회 재시도


async def test_stats_202_then_200_merges_months():
    state = _state(commits=_commits(100, 3), stats_status=[202, 200])
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick",
                                    sleep=lambda s: _sleep(state, s))
    assert got.facts.get("stats_months")
    assert "stats_months" not in got.unavailable


async def test_stats_not_called_when_commits_below_cap():
    state = _state()
    await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    assert not [c for c in state["calls"] if "commit_activity" in c]


async def test_empty_repo_completes_with_empty_facts():
    state = _state(empty=True)
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    assert got.head_sha == "empty" and got.facts["tree"] == [] and got.facts["commits"] == []


async def test_contributors_failure_fails_collection():
    state = _state(contributors_status=500)
    with pytest.raises(GithubError):
        await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")


async def test_readme_404_continues_with_empty():
    state = _state(readme_status=404)
    got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    assert got.facts["readme_text"] == ""


async def test_binary_sample_skipped():
    state = _state()
    FILES["src/acme/core.py"] = "x = 1\n\x00\x01바이너리"
    try:
        got = await collect_mod.collect(_svc(state), "alice", "acme", "widget", "quick")
    finally:
        FILES["src/acme/core.py"] = "x = 1\n" * 2000
    assert "src/acme/core.py" not in got.facts["samples"]


async def test_resolve_head_returns_sha_and_branch():
    state = _state()
    owner, repo, sha, branch = await collect_mod.resolve_head(_svc(state), "alice", "acme/widget")
    assert (owner, repo, branch) == ("acme", "widget", "main") and len(sha) == 40


async def test_resolve_head_rejects_non_repo_target():
    with pytest.raises(GithubError) as exc:
        await collect_mod.resolve_head(_svc(_state()), "alice", "acme")
    assert exc.value.code == "invalid_target"
