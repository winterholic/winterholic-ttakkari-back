"""GitHub 탐색 테스트 공용 가짜: gh 실행기와 상류 transport. 실제 gh·네트워크·키체인을 쓰지 않는다."""
from __future__ import annotations

import json
from collections.abc import Callable

import httpx

from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.client import GithubClient


def fake_token(login: str, gen: int = 0) -> str:
    return f"gho_FAKE{login.replace('-', '')}g{gen}_0123456789abcdef"


class FakeGh:
    """gh 실행기 대역. 호출 인자를 모두 기록해 허용된 명령만 불렸는지 검사할 수 있다."""

    def __init__(self, accounts: list[tuple[str, str]] | None = None, active: str | None = None) -> None:
        self.accounts = list(accounts if accounts is not None else [("alice", "success"), ("bob", "success")])
        self.active = active or (self.accounts[0][0] if self.accounts else "")
        self.calls: list[tuple[str, ...]] = []
        self.gen: dict[str, int] = {}
        self.token_rc: dict[str, int] = {}
        self.bad_stdout = False

    def token_of(self, login: str) -> str:
        return fake_token(login, self.gen.get(login, 0))

    async def __call__(self, args: list[str]) -> tuple[int, bytes]:
        rest = tuple(args[1:])
        self.calls.append(rest)
        if rest == ("auth", "status", "--json", "hosts"):
            rows = [{"state": st, "active": login == self.active, "host": "github.com", "login": login,
                     "tokenSource": "keyring", "scopes": "gist, read:org, repo, workflow", "gitProtocol": "https"}
                    for login, st in self.accounts]
            return 0, json.dumps({"hosts": {"github.com": rows}}).encode()
        if rest[:5] == ("auth", "token", "--hostname", "github.com", "--user"):
            login = rest[5]
            if self.token_rc.get(login, 0) != 0 or login not in dict(self.accounts):
                return 1, b""
            if self.bad_stdout:
                return 0, b"tok en\nwith newline"
            return 0, (self.token_of(login) + "\n").encode()
        raise AssertionError(f"허용되지 않은 gh 호출: {rest}")


def make_client(handler: Callable[[httpx.Request], httpx.Response], gh: FakeGh | None = None, **kw):
    gh = gh or FakeGh()
    reg = AccountRegistry(runner=gh, gh_bin="gh")
    client = GithubClient(reg, transport=httpx.MockTransport(handler), **kw)
    return client, reg, gh


def ok(body, headers: dict[str, str] | None = None, status: int = 200) -> httpx.Response:
    h = {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "4990", "x-ratelimit-reset": "1792291172",
         "x-ratelimit-resource": "core", **(headers or {})}
    if isinstance(body, bytes):
        return httpx.Response(status, content=body, headers=h)
    return httpx.Response(status, json=body, headers=h)
