"""평가용 GitHub 데이터 수집(ADR 0073). 읽기 전용 클라이언트·계정·캐시·대상 파싱을 그대로 쓴다.

클론 없이 GitHub API 만 쓴다. quick 은 저장소당 상류 호출 15회 이하(통계 재시도 1회 제외),
deep 는 표본 확대분만큼 더 쓴다. 치명(repo·트리·커밋) 실패는 그대로 올리고 나머지는
조회 실패로 두지 않고 실행을 실패시킨다. 0점 위장을 막으려고 통계 202만 항목
unavailable 로 둔다.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ttakkari.github import targets
from ttakkari.github.errors import GithubError
from ttakkari.github.service import GithubService
from ttakkari.repo_appraisal import rubric

RAW = "application/vnd.github.raw"
COMMITS_CAP = 100
_STATS_RETRIES = 2
_STATS_WAIT = 2.0
_FILE_BYTES = 24 * 1024
_README_BYTES = 32 * 1024

HARNESS_EXACT = frozenset({"CLAUDE.md", "AGENTS.md", "GEMINI.md", "MEMORY.md",
                           ".github/copilot-instructions.md"})
HARNESS_PREFIXES = (".claude/skills/", ".claude/agents/", ".claude/commands/",
                    ".claude/hooks/", ".cursor/rules/")
HARNESS_SETTINGS = frozenset({".claude/settings.json", ".claude/settings.local.json"})

# 모드별 표본 수. quick 파일 8개 + 고정 7회 = 15회 상한 안이다.
SAMPLE_PLAN = {"quick": {"largest": 3, "entries": 1, "tests": 1, "harness": 3},
               "deep": {"largest": 5, "entries": 2, "tests": 2, "harness": 4}}


@dataclass
class Collected:
    owner: str
    repo: str
    head_sha: str
    default_branch: str
    facts: dict[str, Any]
    requests: int = 0
    tree_truncated: bool = False
    unavailable: list[str] = field(default_factory=list)


def empty_facts() -> dict[str, Any]:
    return {"tree": [], "readme_text": "", "description": "",
            "repo_meta": {"default_branch": "", "license": None, "open_issues_count": 0,
                          "archived": False, "fork": False, "language": None},
            "commits": [], "contributors": [], "releases": [], "issues_sample": [],
            "pulls_sample": [], "workflows": [], "samples": {}, "harness_found": [],
            "harness_texts": {}, "harness_truncated": []}


def find_harness(paths: list[str]) -> list[str]:
    """트리에서 하네스 후보를 고른다. 설정 상수라 추가·제거가 쉽다."""
    return sorted(p for p in paths
                  if p in HARNESS_EXACT or p in HARNESS_SETTINGS
                  or p.startswith(HARNESS_PREFIXES))


def select_samples(tree: list[dict], mode: str) -> tuple[list[str], list[str]]:
    """표본 파일 경로(소스 표본, 하네스). 규칙: 큰 소스·진입점·테스트·하네스 앞부분."""
    plan = SAMPLE_PLAN[mode]
    blobs = [t for t in tree if t.get("type") == "blob"]
    sources = sorted((b for b in blobs
                      if rubric._is_source(b["path"]) and not rubric._is_test(b["path"])),
                     key=lambda b: -(b.get("size") or 0))
    picked: list[str] = [b["path"] for b in sources[:plan["largest"]]]
    entries = [b["path"] for b in blobs if b["path"].rsplit("/", 1)[-1] in rubric.ENTRY_NAMES]
    for path in entries:
        if len([p for p in picked if p in entries]) >= plan["entries"]:
            break
        if path not in picked:
            picked.append(path)
    tests = [b["path"] for b in blobs if rubric._is_source(b["path"]) and rubric._is_test(b["path"])]
    picked += [p for p in tests if p not in picked][:plan["tests"]]
    harness = find_harness([b["path"] for b in blobs])[:plan["harness"]]
    return picked, harness


async def resolve_head(svc: GithubService, login: str, target: str) -> tuple[str, str, str, str]:
    """대상을 owner/repo 로 풀고 기본 브랜치 끝 sha 를 잰다. 캐시 조회용 2회 호출이다."""
    parsed = targets.parse(target)
    if parsed.kind != "repo" or not parsed.owner or not parsed.repo:
        raise GithubError("invalid_target", "저장소 주소(owner/repo 또는 github.com URL)여야 합니다.")
    owner, repo = targets.owner(parsed.owner), targets.repo(parsed.repo)
    meta = (await svc.client.get(login, "repo", f"/repos/{owner}/{repo}", ttl=60)).json()
    if not isinstance(meta, dict):
        raise GithubError("gh_schema_changed")
    branch = meta.get("default_branch") or "main"
    try:
        commits = (await svc.client.get(login, "commits", f"/repos/{owner}/{repo}/commits",
                                        params={"sha": branch, "per_page": 1}, ttl=30)).json()
    except GithubError as exc:
        if exc.code == "gh_empty_repository":
            return owner, repo, "empty", branch
        raise
    sha = commits[0].get("sha") if isinstance(commits, list) and commits else None
    if not isinstance(sha, str) or len(sha) != 40:
        raise GithubError("gh_schema_changed")
    return owner, repo, sha, branch


async def collect(svc: GithubService, login: str, owner: str, repo: str, mode: str = "quick",
                  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> Collected:
    """저장소 데이터를 모아 루브릭 입력 facts 를 만든다."""
    if mode not in SAMPLE_PLAN:
        raise GithubError("invalid_target", "mode 는 quick·deep 중 하나여야 합니다.")
    calls = 0

    async def get(*args: Any, **kw: Any):
        nonlocal calls
        calls += 1
        return await svc.client.get(*args, **kw)

    base = f"/repos/{owner}/{repo}"
    meta = (await get(login, "repo", base, ttl=60)).json()
    if not isinstance(meta, dict):
        raise GithubError("gh_schema_changed")
    branch = meta.get("default_branch") or "main"
    facts = empty_facts()
    facts["repo_meta"] = {"default_branch": branch,
                          "license": (meta.get("license") or {}).get("key"),
                          "open_issues_count": meta.get("open_issues_count", 0),
                          "archived": meta.get("archived", False), "fork": meta.get("fork", False),
                          "language": meta.get("language")}
    facts["description"] = meta.get("description") or ""
    try:
        trees = (await get(login, "trees", f"{base}/git/trees/{branch}",
                           params={"recursive": "1"}, ttl=60)).json()
    except GithubError as exc:
        if exc.code != "gh_empty_repository":
            raise
        return Collected(owner, repo, "empty", branch, facts, calls, False, [])
    if not isinstance(trees, dict) or not isinstance(trees.get("tree"), list):
        raise GithubError("gh_schema_changed")
    truncated = trees.get("truncated") is True
    facts["tree"] = [{"path": t.get("path"), "size": t.get("size", 0), "type": t.get("type")}
                     for t in trees["tree"] if isinstance(t.get("path"), str)]
    try:
        readme = await get(login, "readme", f"{base}/readme", accept=RAW, ttl=60,
                           max_bytes=_README_BYTES, truncate=True)
        facts["readme_text"] = _text(readme.body) or ""
    except GithubError as exc:
        if exc.code != "gh_not_found":  # 저장소는 위에서 확인했으니 404 는 README 없음이다
            raise
    commits = (await get(login, "commits", f"{base}/commits",
                         params={"sha": branch, "per_page": COMMITS_CAP}, ttl=30)).json()
    if not isinstance(commits, list):
        raise GithubError("gh_schema_changed")
    facts["commits"] = [{"sha": c.get("sha"), "date": (c.get("commit") or {}).get("author", {}).get("date"),
                         "author": (c.get("author") or {}).get("login")
                         or (c.get("commit") or {}).get("author", {}).get("name")}
                        for c in commits if isinstance(c, dict)]
    head = facts["commits"][0]["sha"] if facts["commits"] else "empty"
    contributors = (await get(login, "contributors", f"{base}/contributors",
                              params={"per_page": 100}, ttl=60)).json()
    if not isinstance(contributors, list):
        raise GithubError("gh_schema_changed")
    facts["contributors"] = [{"login": c.get("login"), "contributions": c.get("contributions", 0)}
                             for c in contributors if isinstance(c, dict)]
    releases = (await get(login, "releases", f"{base}/releases",
                          params={"per_page": 10}, ttl=60)).json()
    if not isinstance(releases, list):
        raise GithubError("gh_schema_changed")
    facts["releases"] = [{"tag": r.get("tag_name"), "published_at": r.get("published_at"),
                          "draft": r.get("draft", False), "prerelease": r.get("prerelease", False)}
                         for r in releases if isinstance(r, dict)]
    issues = (await get(login, "issues", f"{base}/issues",
                        params={"state": "all", "per_page": 50, "sort": "created",
                                "direction": "desc"}, ttl=30)).json()
    if not isinstance(issues, list):
        raise GithubError("gh_schema_changed")
    facts["issues_sample"] = [{"state": i.get("state")} for i in issues
                              if isinstance(i, dict) and "pull_request" not in i]
    facts["pulls_sample"] = [{"state": i.get("state")} for i in issues
                             if isinstance(i, dict) and "pull_request" in i]
    facts["workflows"] = sorted(t["path"] for t in facts["tree"]
                                if t["path"].startswith(".github/workflows/")
                                and t["path"].endswith((".yml", ".yaml")))
    unavailable: list[str] = []
    if len(facts["commits"]) >= COMMITS_CAP:  # 목록 상한에 걸렸을 때만 통계를 본다
        months = await _stats_months(get, login, base, sleep)
        if months is None:
            unavailable.append("stats_months")
        else:
            facts["stats_months"] = months
    samples, harness = select_samples(facts["tree"], mode)
    for path in samples:
        text = await _fetch_text(get, login, f"{base}/contents/{targets.quote_path(path)}")
        if text is not None:
            facts["samples"][path] = text
    facts["harness_found"] = find_harness([t["path"] for t in facts["tree"]])
    for path in harness:
        fetched = await _fetch_text(get, login, f"{base}/contents/{targets.quote_path(path)}",
                                    track_truncated=True)
        if fetched is None:
            continue
        text, cut = fetched
        facts["harness_texts"][path] = text
        if cut:
            facts["harness_truncated"].append(path)
    return Collected(owner, repo, head if isinstance(head, str) else "empty", branch, facts,
                     calls, truncated, unavailable)


async def _stats_months(get, login: str, base: str,
                        sleep: Callable[[float], Awaitable[None]]) -> list[str] | None:
    """52주 통계를 월 집합으로. 202(계산 중)는 짧게 재시도 후 포기한다."""
    for attempt in range(_STATS_RETRIES + 1):
        up = await get(login, "commit_activity", f"{base}/stats/commit_activity",
                       ttl=300, statuses=(200, 202))
        if up.status == 200:
            weeks = up.json()
            if not isinstance(weeks, list):
                raise GithubError("gh_schema_changed")
            return sorted({datetime.fromtimestamp(w["week"], UTC).strftime("%Y-%m")
                           for w in weeks if isinstance(w, dict) and w.get("total")})
        if attempt < _STATS_RETRIES:
            await sleep(_STATS_WAIT)
    return None


def _text(body: bytes) -> str | None:
    if b"\x00" in body:
        return None
    return body.decode("utf-8", "replace")


async def _fetch_text(get, login: str, path: str, *, track_truncated: bool = False):
    try:
        up = await get(login, "contents", path, accept=RAW, ttl=60,
                       max_bytes=_FILE_BYTES, truncate=True)
    except GithubError as exc:
        if exc.code == "gh_not_found":  # 트리와 본문 사이 브랜치가 움직였다
            return None
        raise
    text = _text(up.body)
    if text is None:
        return None
    return (text, up.truncated) if track_truncated else text
