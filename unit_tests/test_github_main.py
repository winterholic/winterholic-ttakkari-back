"""실제 앱(ttakkari.main)에 GitHub 라우터와 오류 핸들러가 등록됐는지."""
from __future__ import annotations

import httpx
from github_fakes import FakeGh, ok

from ttakkari.github import service as svc_mod
from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.client import GithubClient
from ttakkari.github.errors import GithubError
from ttakkari.github.logs import LogFetcher
from ttakkari.main import app
from ttakkari.security.auth import require_user


async def test_router_registered_auth_required_and_error_shape_through_real_app():
    paths = app.openapi()["paths"]  # app.routes 는 중첩 라우터를 펼쳐 주지 않는다
    assert "/api/github/accounts" in paths and "/api/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs" in paths
    assert all(list(methods) == ["get"] for path, methods in paths.items() if path.startswith("/api/github"))  # 읽기 전용
    assert GithubError in app.exception_handlers
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as http:
        assert (await http.get("/api/github/accounts")).status_code == 401  # 소유자 인증 없이는 계정 목록도 못 본다
        gh = FakeGh([("alice", "success")])
        reg = AccountRegistry(runner=gh, gh_bin="gh")
        transport = httpx.MockTransport(lambda r: ok({"message": "x"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"}, 403))
        client = GithubClient(reg, transport=transport)
        service = svc_mod.GithubService(registry=reg, client=client, logs=LogFetcher(client, transport=transport))
        app.dependency_overrides[require_user] = lambda: "owner"
        app.dependency_overrides[svc_mod.get_service] = lambda: service
        try:
            r = await http.get("/api/github/me", params={"account": "alice"})
            assert r.status_code == 429 and r.json()["code"] == "gh_rate_limited" and r.json()["reset"] == 9999999999
            assert int(r.headers["retry-after"]) > 0
            r = await http.get("/api/github/me")
            assert r.status_code == 422 and r.json()["code"] == "validation"  # main.py 의 공통 검증 오류 형식
            r = await http.get("/api/github/me", params={"account": "zed"})
            assert r.status_code == 404 and r.json()["code"] == "account_unavailable"
        finally:
            app.dependency_overrides.pop(require_user, None)
            app.dependency_overrides.pop(svc_mod.get_service, None)
            await client.aclose()
