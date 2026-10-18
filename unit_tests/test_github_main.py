"""실제 앱(ttakkari.main)에 GitHub 라우터와 오류 핸들러가 등록됐는지(정적 확인). HTTP 동작은 tests/test_github_integration.py."""
from __future__ import annotations

from ttakkari.github.errors import GithubError
from ttakkari.main import app


def test_router_registered_read_only_and_error_handler_installed():
    paths = app.openapi()["paths"]  # app.routes 는 중첩 라우터를 펼쳐 주지 않는다
    assert "/api/github/accounts" in paths and "/api/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs" in paths
    assert all(list(methods) == ["get"] for path, methods in paths.items() if path.startswith("/api/github"))  # 읽기 전용
    assert GithubError in app.exception_handlers
