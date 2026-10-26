"""실제 앱(lifespan·미들웨어·소유자 JWT 인증 포함)으로 /api/github 를 부른다. 상류 GitHub 와 gh 만 가짜."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "unit_tests"))

from github_fakes import FakeGh, fake_token, ok

from ttakkari.github import service as svc_mod
from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.client import GithubClient
from ttakkari.github.git_ops import GithubGitOperations
from ttakkari.github.logs import LogFetcher
from ttakkari.main import app


@pytest.fixture
def fake_service():
    gh = FakeGh([("alice", "success")])
    state = {"handler": lambda req: ok({"login": "alice", "type": "User"})}
    transport = httpx.MockTransport(lambda req: state["handler"](req))
    reg = AccountRegistry(runner=gh, gh_bin="gh")
    client = GithubClient(reg, transport=transport)
    service = svc_mod.GithubService(registry=reg, client=client, logs=LogFetcher(client, transport=transport))
    app.dependency_overrides[svc_mod.get_service] = lambda: service
    yield service, gh, state
    app.dependency_overrides.pop(svc_mod.get_service, None)


async def test_owner_jwt_required_and_error_shapes_through_real_app(client, auth, fake_service):
    r = await client.get("/api/github/accounts")
    assert r.status_code == 401  # 소유자 인증 없이는 계정 목록도 못 본다
    r = await client.get("/api/github/accounts", headers={"Authorization": "Bearer not-a-token"})
    assert r.status_code == 401
    r = await client.get("/api/github/accounts", headers=auth)
    assert r.status_code == 200 and [a["login"] for a in r.json()["accounts"]] == ["alice"]
    assert fake_token("alice") not in r.text
    r = await client.get("/api/github/me", params={"account": "alice"}, headers=auth)
    assert r.status_code == 200 and r.json()["data"]["login"] == "alice" and r.json()["account"] == "alice"
    r = await client.get("/api/github/me", headers=auth)
    assert r.status_code == 422 and r.json()["code"] == "validation"  # main.py 공통 검증 오류 형식
    r = await client.get("/api/github/me", params={"account": "zed"}, headers=auth)
    assert r.status_code == 404 and r.json()["code"] == "account_unavailable"


async def test_rate_limit_body_and_retry_after_survive_main_error_handlers(client, auth, fake_service):
    _, _, state = fake_service
    state["handler"] = lambda req: ok({"message": "x"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}, 403)
    r = await client.get("/api/github/repos/acme/widget", params={"account": "alice"}, headers=auth)
    assert r.status_code == 429 and r.json()["code"] == "gh_rate_limited" and r.json()["reset"] == 9999999999
    assert int(r.headers["retry-after"]) > 0 and r.json()["retry_after"] == int(r.headers["retry-after"])


async def test_upstream_401_does_not_log_the_app_session_out(client, auth, fake_service):
    _, _, state = fake_service
    state["handler"] = lambda req: httpx.Response(401, json={"message": "Bad credentials"})
    r = await client.get("/api/github/me", params={"account": "alice"}, headers=auth)
    assert r.status_code == 502 and r.json()["code"] == "gh_auth_expired" and "www-authenticate" not in {k.lower() for k in r.headers}


async def test_agent_git_is_account_scoped_and_personal_session_only(client, auth, make_dir, fake_service):
    import uuid

    from ttakkari.api import github_agent
    from ttakkari.orchestrator.runtime import issue_token

    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": "github work", "root_path": str(root)},
                            headers=auth)).json()
    headers = {}
    for pool in ("personal", "work"):
        session = await client.post("/api/sessions", json={"workspace_id": ws["id"], "pool": pool}, headers=auth)
        assert session.status_code == 201, session.text
        headers[pool] = {"Authorization": "Bearer " + issue_token(uuid.UUID(session.json()["id"]))}

    seen = []

    async def runner(args, *, cwd, env, timeout):
        seen.append((env["TTAKKARI_GITHUB_TOKEN"], list(args)))
        if "clone" in args:
            Path(args[-1], ".git").mkdir(parents=True)
            return 0, b"cloned", b""
        return 0, b"a" * 40, b""

    ops = GithubGitOperations(fake_service[0].registry, runner=runner)
    app.dependency_overrides[github_agent.get_git_operations] = lambda: ops
    try:
        body = {"operation": "clone", "account": "alice", "owner": "acme", "repo": "private"}
        denied = await client.post("/api/agent/github/git", json=body, headers=headers["work"])
        assert denied.status_code == 403
        assert not seen

        response = await client.post("/api/agent/github/git", json=body, headers=headers["personal"])
        assert response.status_code == 200, response.text
        assert response.json()["account"] == "alice"
        assert response.json()["path"] == ".ttakkari-github/acme/private"
        assert seen[0][0] == fake_token("alice")
        assert fake_token("alice") not in response.text
    finally:
        app.dependency_overrides.pop(github_agent.get_git_operations, None)
