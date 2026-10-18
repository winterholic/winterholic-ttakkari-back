"""/api/github 라우트 통합(상류는 MockTransport, gh 는 FakeGh). 응답 data 는 tests/fixtures/github/api 골든과 같아야 한다."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from github_fakes import FakeGh, fake_token, ok

from ttakkari.api import github as api
from ttakkari.github import service as svc_mod
from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.client import GithubClient
from ttakkari.github.logs import LogFetcher
from ttakkari.security.auth import require_user

FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "github"


def up(name):
    return json.loads((FIX / "upstream" / f"{name}.json").read_text(encoding="utf-8"))


def gold(name):
    return json.loads((FIX / "api" / f"{name}.json").read_text(encoding="utf-8"))


class Upstream:
    """경로 → 응답 표. 호출 기록을 남긴다."""

    def __init__(self):
        self.routes: dict[str, object] = {}
        self.calls: list[httpx.Request] = []

    def set(self, path: str, body, headers=None, status=200):
        self.routes[path] = (body, headers, status)

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        hit = self.routes.get(req.url.path)
        if hit is None:
            return httpx.Response(404, json={"message": "Not Found"})
        body, headers, status = hit
        if callable(body):
            return body(req)
        return ok(body, headers, status)

    def paths(self):
        return [c.url.path for c in self.calls]


@pytest.fixture
def world():
    gh = FakeGh([("alice", "success"), ("bob", "success")])
    upstream = Upstream()
    transport = httpx.MockTransport(upstream)
    reg = AccountRegistry(runner=gh, gh_bin="gh")
    client = GithubClient(reg, transport=transport, budget=5)
    service = svc_mod.GithubService(registry=reg, client=client, logs=LogFetcher(client, transport=transport))
    app = FastAPI()
    app.include_router(api.router)
    api.install(app)

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc):  # main.py 와 같은 모양
        return JSONResponse({"code": "validation", "message": "요청 형식이 올바르지 않습니다."}, status_code=422)

    app.dependency_overrides[require_user] = lambda: "owner"
    app.dependency_overrides[svc_mod.get_service] = lambda: service
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return http, upstream, gh, app


A = {"account": "alice"}


async def test_owner_auth_required_and_account_required(world):
    http, _, _, app = world
    app.dependency_overrides.pop(require_user)
    assert (await http.get("/api/github/me", params=A)).status_code == 401
    app.dependency_overrides[require_user] = lambda: "owner"
    r = await http.get("/api/github/me")  # account 누락: 서버가 기본 계정을 추정하지 않는다
    assert r.status_code == 422 and r.json()["code"] == "validation"
    r = await http.get("/api/github/me", params={"account": "a/b"})
    assert r.status_code == 422 and r.json()["code"] == "invalid_target"
    r = await http.get("/api/github/me", params={"account": "carol"})
    assert r.status_code == 404 and r.json()["code"] == "account_unavailable"


async def test_accounts_lists_metadata_only_and_probe_is_boolean(world):
    http, _, gh, _ = world
    r = await http.get("/api/github/accounts")
    body = r.json()
    assert r.status_code == 200 and body["gh_available"] is True
    assert [(a["login"], a["state"], a["token_available"]) for a in body["accounts"]] == [("alice", "success", None), ("bob", "success", None)]
    assert "token" not in json.dumps(body).lower().replace("token_available", "")
    assert not any(c[:2] == ("auth", "token") for c in gh.calls)
    gh.token_rc["bob"] = 1
    body = (await http.get("/api/github/accounts", params={"probe": "true"})).json()
    assert [a["token_available"] for a in body["accounts"]] == [True, False]
    assert fake_token("alice") not in json.dumps(body)


async def test_me_orgs_repos_and_owner_overview(world):
    http, u, _, _ = world
    u.set("/user", up("user"))
    u.set("/user/orgs", up("orgs"), {"link": '<https://api.github.com/user/orgs?page=2>; rel="next"'})
    u.set("/user/repos", up("repos"))
    r = await http.get("/api/github/me", params=A)
    env = r.json()
    assert r.status_code == 200 and env["data"] == gold("me") and env["data"]["is_viewer"] is True and env["account"] == "alice"
    assert env["rate"]["remaining"] == 4990 and env["cached"] is False and env["stale"] is False and env["page"] is None
    r = await http.get("/api/github/me/orgs", params=A | {"per_page": 2})
    assert r.json()["data"] == gold("orgs") and r.json()["page"] == {"page": 1, "per_page": 2, "has_more": True, "total_count": None, "limit_hit": False}
    q = next(c for c in u.calls if c.url.path == "/user/orgs").url.params
    assert q["per_page"] == "2" and q["page"] == "1"
    r = await http.get("/api/github/me/repos", params=A | {"sort": "pushed"})
    assert r.json()["data"] == gold("repos")
    q = next(c for c in u.calls if c.url.path == "/user/repos").url.params
    assert q["sort"] == "pushed" and q["affiliation"] == "owner,collaborator,organization_member"
    u.set("/users/acme", up("owner_org_user"))
    u.set("/orgs/acme", up("org"))
    u.set("/orgs/acme/repos", up("repos"))
    env = (await http.get("/api/github/owners/acme", params=A)).json()
    assert env["data"] == gold("owner_org") and env["data"]["is_viewer"] is False
    env = (await http.get("/api/github/owners/acme/repos", params=A)).json()
    assert env["data"] == gold("repos") and "/orgs/acme/repos" in u.paths() and "/users/acme/repos" not in u.paths()
    u.set("/users/octo", up("user"))
    u.set("/users/octo/repos", up("repos"))
    await http.get("/api/github/owners/octo/repos", params=A)
    assert "/users/octo/repos" in u.paths()
    assert (await http.get("/api/github/owners/a..b", params=A)).status_code == 422


async def test_repo_and_branches_mark_default(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget", up("repo"))
    u.set("/repos/acme/widget/branches", up("branches"))
    assert (await http.get("/api/github/repos/acme/widget", params=A)).json()["data"] == gold("repo")
    r = await http.get("/api/github/repos/acme/widget/branches", params=A)
    assert r.json()["data"] == gold("branches") and r.json()["data"][0]["is_default"] is True
    assert (await http.get("/api/github/repos/acme/wid get", params=A)).status_code in (404, 422)
    assert (await http.get("/api/github/repos/acme/x.git", params=A)).status_code == 422


async def test_contents_dir_file_binary_symlink_submodule(world):
    http, u, _, _ = world
    base = "/repos/acme/widget/contents"
    u.set(f"{base}/src", up("contents_dir"))
    u.set(f"{base}/src/main.py", up("contents_file"))
    u.set(f"{base}/logo.png", up("contents_file_binary"))
    u.set(f"{base}/src/link", up("contents_symlink"))
    u.set(f"{base}/src/vendor", up("contents_submodule"))
    g = lambda path, **p: http.get("/api/github/repos/acme/widget/contents", params=A | {"path": path} | p)
    r = await g("src", ref="main")
    assert r.json()["data"] == gold("contents_dir") | {"ref": "main"}
    sent = next(c for c in u.calls if c.url.path == f"{base}/src")
    assert sent.url.params["ref"] == "main" and sent.headers["accept"] == "application/vnd.github.object+json"
    assert (await g("src/main.py", ref="main")).json()["data"] == gold("contents_file")
    assert (await g("logo.png")).json()["data"] == gold("contents_file_binary")
    assert (await g("src/link")).json()["data"] == gold("contents_symlink")
    assert (await g("src/vendor")).json()["data"] == gold("contents_submodule")


async def test_contents_rejects_hostile_path_and_ref_without_calling_upstream(world):
    http, u, _, _ = world
    for p in ["../../etc/passwd", "a/../b", "/abs", "a\\b", "%2e%2e/x", "a//b"]:
        r = await http.get("/api/github/repos/acme/widget/contents", params=A | {"path": p})
        assert r.status_code == 422 and r.json()["code"] == "invalid_target", p
    for ref in ["-x", "a..b", "a b", "a~1", "a@{1}"]:
        r = await http.get("/api/github/repos/acme/widget/contents", params=A | {"ref": ref})
        assert r.status_code == 422, ref
    assert u.calls == []


async def test_contents_special_characters_are_encoded_not_interpreted(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/contents/a b#c?.py", up("contents_file"))  # Upstream 은 디코딩된 path 로 찾는다
    r = await http.get("/api/github/repos/acme/widget/contents", params=A | {"path": "a b#c?.py"})
    assert r.status_code == 200
    assert u.calls[0].url.raw_path.decode().startswith("/repos/acme/widget/contents/a%20b%23c%3F.py")


async def test_contents_huge_file_gets_truncated_prefix_never_silent_success(world):
    http, u, _, _ = world
    path = "/repos/acme/widget/contents/big.log"

    def serve(req):
        if "raw" in req.headers["accept"]:
            return ok(("line\n" * 400_000).encode())  # 2MB 이상
        return ok(up("contents_file_huge"))

    u.set(path, serve)
    r = await http.get("/api/github/repos/acme/widget/contents", params=A | {"path": "big.log"})
    f = r.json()["data"]["file"]
    assert r.status_code == 200 and f["too_large"] is True and f["truncated"] is True and f["binary"] is False
    assert f["size"] == 5 * 1024 * 1024 and len(f["text"].encode()) <= 1024 * 1024 and f["text"].startswith("line\n")
    # raw 조회가 실패하면 본문 없이 too_large 만(성공으로 위장하지 않는다)
    u.set(path, lambda req: httpx.Response(403, json={"message": "too large"}) if "raw" in req.headers["accept"] else ok(up("contents_file_huge")))
    f = (await http.get("/api/github/repos/acme/widget/contents", params=A | {"path": "big.log", "ref": "main"})).json()["data"]["file"]
    assert f["too_large"] is True and f["truncated"] is False and f["text"] is None
    # 100MiB 초과는 raw 시도 자체를 하지 않는다
    n = len(u.calls)
    huge = up("contents_file_huge") | {"size": 200 * 1024 * 1024}
    u.set(path, huge)
    f = (await http.get("/api/github/repos/acme/widget/contents", params=A | {"path": "big.log", "ref": "v2"})).json()["data"]["file"]
    assert f["too_large"] and f["text"] is None and len(u.calls) == n + 1


async def test_contents_empty_repo_is_explicit(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/contents", {"message": "This repository is empty."}, status=404)
    r = await http.get("/api/github/repos/acme/widget/contents", params=A)
    assert r.status_code == 409 and r.json()["code"] == "gh_empty_repository"


async def test_readme_found_and_not_found_vs_inaccessible_repo(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/readme", up("readme"))
    assert (await http.get("/api/github/repos/acme/widget/readme", params=A)).json()["data"] == gold("readme")
    u.set("/repos/acme/bare", up("repo") | {"name": "bare", "full_name": "acme/bare"})
    r = await http.get("/api/github/repos/acme/bare/readme", params=A)
    assert r.status_code == 404 and r.json()["code"] == "readme_not_found"
    r = await http.get("/api/github/repos/acme/secret/readme", params=A)  # 저장소 자체가 없음/권한 없음
    assert r.status_code == 404 and r.json()["code"] == "gh_not_found"


async def test_pulls_list_detail_files(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/pulls", up("pulls"), {"link": '<https://api.github.com/x?page=2>; rel="next"'})
    u.set("/repos/acme/widget/pulls/7", up("pull"))
    u.set("/repos/acme/widget/pulls/7/files", up("pull_files"))
    r = await http.get("/api/github/repos/acme/widget/pulls", params=A | {"state": "all", "sort": "updated", "direction": "asc"})
    assert r.json()["data"] == gold("pulls") and r.json()["page"]["has_more"] is True
    q = u.calls[-1].url.params
    assert (q["state"], q["sort"], q["direction"], q["per_page"]) == ("all", "updated", "asc", "30")
    assert (await http.get("/api/github/repos/acme/widget/pulls/7", params=A)).json()["data"] == gold("pull")
    r = await http.get("/api/github/repos/acme/widget/pulls/7/files", params=A)
    got = r.json()["data"]
    assert [f["patch_state"] for f in got] == ["ok", "empty", "unavailable", "unavailable", "truncated"]
    assert r.json()["page"]["limit_hit"] is False
    r = await http.get("/api/github/repos/acme/widget/pulls/7/files", params=A | {"page": 60, "per_page": 50})
    assert r.json()["page"]["limit_hit"] is True  # 3,000개 상한에 닿음


async def test_query_validation_is_strict(world):
    http, u, _, _ = world
    for path, p in [("pulls", {"state": "evil"}), ("pulls", {"per_page": 1000}), ("pulls", {"page": 0}), ("pulls", {"page": 201}),
                    ("pulls/abc", {}), ("pulls/0", {}), ("pulls/99999999999", {}), ("issues", {"labels": "a\nb"}),
                    ("issues", {"sort": "evil"}), ("actions/runs", {"status": "evil"}), ("actions/runs", {"branch": "a..b"}),
                    ("actions/runs", {"event": "push; rm"}), ("actions/runs/x", {}), ("actions/jobs/0/logs", {}),
                    ("actions/jobs/1/logs", {"lines": 5000}), ("actions/jobs/1/logs", {"from_line": 0})]:
        r = await http.get(f"/api/github/repos/acme/widget/{path}", params=A | p)
        assert r.status_code == 422, (path, p, r.status_code)
    assert u.calls == []


async def test_issues_filter_pr_detail_kind_and_comments(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/issues", up("issues"))
    u.set("/repos/acme/widget/issues/3", up("issue"))
    u.set("/repos/acme/widget/issues/7", up("issue_as_pull"))
    u.set("/repos/acme/widget/issues/3/comments", up("issue_comments"))
    r = await http.get("/api/github/repos/acme/widget/issues", params=A | {"labels": "bug,ui", "state": "all"})
    assert r.json()["data"] == gold("issues")
    assert u.calls[-1].url.params["labels"] == "bug,ui"
    assert (await http.get("/api/github/repos/acme/widget/issues/3", params=A)).json()["data"] == gold("issue")
    assert (await http.get("/api/github/repos/acme/widget/issues/7", params=A)).json()["data"] == gold("issue_as_pull")
    assert (await http.get("/api/github/repos/acme/widget/issues/3/comments", params=A)).json()["data"] == gold("issue_comments")


async def test_actions_runs_run_jobs(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/actions/runs", up("runs"))
    u.set("/repos/acme/widget/actions/runs/500", up("run"))
    u.set("/repos/acme/widget/actions/runs/500/jobs", up("jobs"))
    r = await http.get("/api/github/repos/acme/widget/actions/runs", params=A | {"status": "failure", "branch": "feature/x", "event": "push"})
    assert r.json()["data"] == gold("runs") and r.json()["page"]["total_count"] == 2
    q = u.calls[-1].url.params
    assert (q["status"], q["branch"], q["event"]) == ("failure", "feature/x", "push")
    assert (await http.get("/api/github/repos/acme/widget/actions/runs/500", params=A)).json()["data"] == gold("run")
    r = await http.get("/api/github/repos/acme/widget/actions/runs/500/jobs", params=A | {"filter": "all"})
    assert r.json()["data"] == gold("jobs") and u.calls[-1].url.params["filter"] == "all"


# ---- resolve ----

async def test_resolve_pure_parse_without_account(world):
    http, u, _, _ = world
    r = await http.get("/api/github/resolve", params={"q": "https://github.com/acme/widget/pull/7/files"})
    assert r.status_code == 200 and r.json()["kind"] == "pull" and r.json()["tab"] == "files" and r.json()["number"] == 7
    r = await http.get("/api/github/resolve", params={"q": "https://github.com/acme/widget/blob/feature/login-ui/src/a.py"})
    assert r.json()["needs_ref_resolution"] is True and r.json()["ref_path"] == "feature/login-ui/src/a.py"
    assert u.calls == []
    r = await http.get("/api/github/resolve", params={"q": "https://evil.com/a/b"})
    assert r.status_code == 422 and r.json()["code"] == "unsupported_host"
    assert (await http.get("/api/github/resolve", params={"q": "x" * 3000})).status_code == 422


async def test_resolve_slash_branch_uses_real_refs(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/git/matching-refs/heads/feature", up("matching_refs_heads"))
    u.set("/repos/acme/widget/git/matching-refs/tags/feature", [])
    q = "https://github.com/acme/widget/blob/feature/login-ui/src/a.py#L3"
    r = await http.get("/api/github/resolve", params={"q": q, "account": "alice"})
    j = r.json()
    assert r.status_code == 200 and (j["ref"], j["path"], j["is_file"], j["needs_ref_resolution"]) == ("feature/login-ui", "src/a.py", True, False)
    # 같은 접두사의 더 짧은 실제 브랜치(feature)도 있으므로 가장 긴 실제 브랜치가 이긴다. 없는 브랜치는 ref_not_found.
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/feature/other/dir", "account": "alice"})
    assert (r.json()["ref"], r.json()["path"]) == ("feature", "other/dir")
    u.set("/repos/acme/widget/git/matching-refs/heads/nope", [])
    u.set("/repos/acme/widget/git/matching-refs/tags/nope", [])
    u.set("/repos/acme/widget", up("repo"))
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/nope/x", "account": "alice"})
    assert r.status_code == 404 and r.json()["code"] == "ref_not_found"
    sha = "a" * 40
    n = len(u.calls)
    r = await http.get("/api/github/resolve", params={"q": f"acme/widget/blob/{sha}/src/a.py", "account": "alice"})
    assert (r.json()["ref"], r.json()["path"]) == (sha, "src/a.py") and len(u.calls) == n  # SHA 는 호출 없이
    # 저장소가 안 보이면 ref_not_found 가 아니라 권한/없음 오류를 그대로
    r = await http.get("/api/github/resolve", params={"q": "acme/ghost/tree/main/x", "account": "alice"})
    assert r.status_code == 404 and r.json()["code"] == "gh_not_found"


# ---- 오류 본문 ----

async def test_error_bodies_carry_reason_reset_and_retry_after_header(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget", {"message": "x"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}, 403)
    r = await http.get("/api/github/repos/acme/widget", params=A)
    assert r.status_code == 429 and r.json()["code"] == "gh_rate_limited" and r.json()["reset"] == 9999999999
    assert int(r.headers["retry-after"]) > 0 and r.json()["retry_after"] == int(r.headers["retry-after"])
    u.set("/repos/acme/sso", {"message": "x"}, {"x-github-sso": "required; url=https://github.com/orgs/acme/sso?x=1"}, 403)
    r = await http.get("/api/github/repos/acme/sso", params={"account": "bob"})  # alice 는 위 요청으로 한도 소진 상태
    assert r.status_code == 403 and r.json()["reason"] == "sso_required" and r.json()["sso_url"].startswith("https://github.com/orgs/acme/")
    assert "x-ratelimit" not in {k.lower() for k in r.headers}


async def test_upstream_401_is_never_a_401_for_the_app(world):
    http, u, _, _ = world
    u.set("/user", {"message": "Bad credentials"}, status=401)
    r = await http.get("/api/github/me", params=A)
    assert r.status_code == 502 and r.json()["code"] == "gh_auth_expired" and "www-authenticate" not in {k.lower() for k in r.headers}


async def test_two_accounts_same_path_are_cached_separately_end_to_end(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget", lambda req: ok(up("repo") | {"description": req.headers["authorization"][8:24]}))
    a = (await http.get("/api/github/repos/acme/widget", params={"account": "alice"})).json()
    b = (await http.get("/api/github/repos/acme/widget", params={"account": "bob"})).json()
    a2 = (await http.get("/api/github/repos/acme/widget", params={"account": "alice"})).json()
    assert a["data"]["description"] != b["data"]["description"] and a2["cached"] is True and b["cached"] is False
    assert a["account"] == "alice" and b["account"] == "bob"


async def test_stale_flag_on_envelope(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget", up("repo"))
    r1 = await http.get("/api/github/repos/acme/widget", params=A)
    assert r1.json()["stale"] is False
    # 상류가 죽은 뒤 만료된 캐시 → stale
    svc = world[3].dependency_overrides[svc_mod.get_service]()
    svc.client._cache._clock = lambda: 10**9  # 모든 항목 만료
    u.routes.clear()
    u.set("/repos/acme/widget", {}, status=500)
    svc.client._backoff = (0.0, 0.0)
    r = await http.get("/api/github/repos/acme/widget", params=A)
    assert r.status_code == 200 and r.json()["stale"] is True and r.json()["cached"] is True and r.json()["data"]["full_name"] == "acme/widget"


# ---- Actions 로그 ----

LOG_LOC = "https://pipelines.actions.githubusercontent.com/abc/job-9/logs.txt?sig=SIGNEDSECRET"


def log_world(world, text: str, location: str = LOG_LOC, job: int = 9):
    _, u, _, _ = world
    u.set(f"/repos/acme/widget/actions/jobs/{job}/logs", lambda req: httpx.Response(302, headers={"location": location}))
    downloads: list[httpx.Request] = []
    orig = u.__call__

    def route(req):
        if req.url.host != "api.github.com":
            downloads.append(req)
            return httpx.Response(200, content=text.encode(), headers={"content-type": "text/plain"})
        return orig(req)

    transport = httpx.MockTransport(route)
    svc = world[3].dependency_overrides[svc_mod.get_service]()
    svc.client._transport = transport
    svc.logs._transport = transport
    svc.logs._dl = None
    return downloads


async def test_logs_window_masks_secrets_and_download_carries_no_credentials(world):
    http, _, _, _ = world
    lines = [f"2026-10-17T00:00:{i % 60:02d}Z step {i}" for i in range(1, 1201)]
    lines[2] = "export TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    lines[3] = "Authorization: Bearer abcdefghijklmnopqrstuvwxyz"
    downloads = log_world(world, "\r\n".join(lines) + "\r\n")
    r = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A | {"from_line": 1, "lines": 500})
    d = r.json()["data"]
    assert r.status_code == 200 and d["job_id"] == 9 and d["from_line"] == 1 and len(d["lines"]) == 500
    assert d["total_lines"] == 1200 and d["next_from_line"] == 501 and d["masked"] is True and d["truncated"] is False
    blob = "\n".join(d["lines"])
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in blob and "abcdefghijklmnopqrstuvwxyz" not in blob
    assert "[REDACTED]" in d["lines"][2] and d["lines"][0].endswith("step 1")
    assert len(downloads) == 1 and "authorization" not in {k.lower() for k in downloads[0].headers}
    assert fake_token("alice") not in json.dumps(r.json()) and "SIGNEDSECRET" not in json.dumps(r.json())
    r2 = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A | {"from_line": 1001, "lines": 500})
    d2 = r2.json()["data"]
    assert len(d2["lines"]) == 200 and d2["next_from_line"] is None and len(downloads) == 1  # 두 번째 창은 캐시
    r3 = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A | {"from_line": 5000})
    assert r3.status_code == 200 and r3.json()["data"]["lines"] == [] and r3.json()["data"]["total_lines"] == 1200


async def test_logs_private_key_block_is_masked_keeping_line_numbers(world):
    http, _, _, _ = world
    text = "a\n-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxxxxxxxxxxxxxxxxxxxx\nyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy\n-----END RSA PRIVATE KEY-----\nb\n"
    log_world(world, text)
    d = (await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A)).json()["data"]
    assert len(d["lines"]) == 6 and d["lines"][0] == "a" and d["lines"][5] == "b"
    assert "MIIEow" not in "\n".join(d["lines"]) and "yyyyyyyy" not in "\n".join(d["lines"])


@pytest.mark.parametrize("loc", [
    "https://evil.com/logs.txt", "http://pipelines.actions.githubusercontent.com/x", "https://pipelines.actions.githubusercontent.com:8443/x",
    "https://user:pw@pipelines.actions.githubusercontent.com/x", "https://githubusercontent.com.evil.com/x", "https://127.0.0.1/x",
    "https://productionresultssa1.blob.core.windows.net.evil.com/x", "https://blob.core.windows.net/x", "file:///etc/passwd", "//evil.com/x",
])
async def test_logs_redirect_to_unlisted_host_is_refused_without_downloading(world, loc):
    http, _, _, _ = world
    downloads = log_world(world, "secret log", loc)
    r = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A)
    assert r.status_code == 502 and r.json()["code"] == "gh_logs_unavailable" and r.json()["reason"] == "redirect_host_not_allowed"
    assert downloads == []
    assert "sig=" not in json.dumps(r.json()) and "evil" not in json.dumps(r.json()).replace("redirect_host_not_allowed", "")


@pytest.mark.parametrize("loc", [
    "https://pipelines.actions.githubusercontent.com/a/b?sig=1",
    "https://productionresultssa12.blob.core.windows.net/actionsresults/x?sig=1",
    "https://results-receiver.actions.githubusercontent.com/x",
])
async def test_logs_known_hosts_are_allowed(world, loc):
    http, _, _, _ = world
    downloads = log_world(world, "only line\n", loc)
    r = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A)
    assert r.status_code == 200 and r.json()["data"]["lines"] == ["only line"] and len(downloads) == 1


async def test_logs_too_large_expired_and_redirect_chain(world):
    http, _, _, _ = world
    log_world(world, "x" * (9 * 1024 * 1024))
    r = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A)
    assert r.status_code == 413 and r.json()["code"] == "gh_too_large"
    # 다운로드 호스트가 다시 리다이렉트하면 따라가지 않는다
    http2, _, _, app2 = world
    svc = app2.dependency_overrides[svc_mod.get_service]()
    svc.logs._cache.clear() if hasattr(svc.logs._cache, "clear") else None
    t = httpx.MockTransport(lambda req: httpx.Response(302, headers={"location": "https://evil.com/x"}) if req.url.host != "api.github.com"
                            else httpx.Response(302, headers={"location": LOG_LOC}))
    svc.client._transport = t
    svc.client._http_client = None
    svc.logs._transport = t
    svc.logs._dl = None
    r = await http2.get("/api/github/repos/acme/widget/actions/jobs/10/logs", params=A)
    assert r.status_code == 502 and r.json()["code"] == "gh_logs_unavailable" and r.json()["reason"] == "redirect_chain"
    # 로그 만료(410)
    t2 = httpx.MockTransport(lambda req: httpx.Response(410) if req.url.host != "api.github.com" else httpx.Response(302, headers={"location": LOG_LOC}))
    svc.client._transport = t2
    svc.client._http_client = None
    svc.logs._transport = t2
    svc.logs._dl = None
    r = await http2.get("/api/github/repos/acme/widget/actions/jobs/11/logs", params=A)
    assert r.status_code == 410 and r.json()["code"] == "gh_gone"


async def test_logs_endpoint_inline_200_body_is_accepted(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget/actions/jobs/9/logs", b"inline one\ninline two\n")
    d = (await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A)).json()["data"]
    assert d["lines"] == ["inline one", "inline two"]


async def test_logs_download_shares_one_budget_and_times_out(world):
    """로그는 302 호출과 내려받기가 한 예산(기본 20초, 여기서는 0.4초)을 나눠 쓴다."""
    import asyncio
    import time

    http, _, _, app = world
    svc = app.dependency_overrides[svc_mod.get_service]()

    async def route(req):
        if req.url.host == "api.github.com":
            await asyncio.sleep(0.15)
            return httpx.Response(302, headers={"location": LOG_LOC})
        await asyncio.sleep(30)  # 내려받기 호스트가 멈춤
        return httpx.Response(200, content=b"never")

    t = httpx.MockTransport(route)
    svc.client._transport = t
    svc.logs._transport = t
    svc.logs._budget = 0.4
    t0 = time.monotonic()
    r = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A)
    assert r.status_code == 504 and r.json()["code"] == "gh_timeout" and time.monotonic() - t0 < 0.8


async def test_no_account_and_gh_missing(world):
    http, _, gh, _ = world
    gh.accounts = []
    await world[3].dependency_overrides[svc_mod.get_service]().registry.list_accounts(force=True)
    r = await http.get("/api/github/me", params=A)
    assert r.status_code == 503 and r.json()["code"] == "no_account"
    body = (await http.get("/api/github/accounts")).json()
    assert body["accounts"] == [] and body["gh_available"] is True
    # gh 바이너리를 못 찾으면 계정 목록은 빈 목록 + gh_available=false 로 알린다(500 이 아니다)
    svc = world[3].dependency_overrides[svc_mod.get_service]()
    svc.registry._gh = None
    svc.registry._listed_at = None
    import ttakkari.github.accounts as acc_mod
    orig = acc_mod.find_gh
    acc_mod.find_gh = lambda: None
    try:
        r = await http.get("/api/github/accounts")
        assert r.status_code == 200 and r.json() == {"accounts": [], "gh_available": False, "fetched_at": r.json()["fetched_at"]}
    finally:
        acc_mod.find_gh = orig


async def test_upstream_shape_drift_is_gh_schema_changed_not_500(world):
    http, u, _, _ = world
    u.set("/repos/acme/widget", b"<html>maintenance</html>")
    r = await http.get("/api/github/repos/acme/widget", params=A)
    assert r.status_code == 502 and r.json()["code"] == "gh_schema_changed"
    u.set("/repos/acme/widget/pulls", {"unexpected": "object"})
    r = await http.get("/api/github/repos/acme/widget/pulls", params=A)
    assert r.status_code == 502 and r.json()["code"] == "gh_schema_changed"
    u.set("/repos/acme/widget/pulls/7", up("pull") | {"number": "7"})
    assert (await http.get("/api/github/repos/acme/widget/pulls/7", params=A)).json()["code"] == "gh_schema_changed"


def test_every_error_code_is_documented_in_the_contract_readme():
    from ttakkari.github.errors import CATALOG

    text = (FIX / "README.md").read_text(encoding="utf-8")
    for code, (status, _) in CATALOG.items():
        assert f"`{code}`" in text, code
        assert str(status) in text, (code, status)


async def test_signed_log_url_and_tokens_never_reach_logs(world, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    http, _, _, _ = world
    log_world(world, "line one\n")
    r = await http.get("/api/github/repos/acme/widget/actions/jobs/9/logs", params=A)
    assert r.status_code == 200
    log_world(world, "x", "https://evil.com/steal?sig=SIGNEDSECRET", job=10)
    await http.get("/api/github/repos/acme/widget/actions/jobs/10/logs", params=A)
    assert "SIGNEDSECRET" not in caplog.text and fake_token("alice") not in caplog.text and "sig=" not in caplog.text
