"""gh 로그인 계정 목록과 계정별 토큰(ADR 0042 §5).

- 전역 active 계정을 바꾸지 않는다(`gh auth switch` 금지). 요청마다 login 으로 토큰을 따로 받는다.
- 허용 gh 호출은 둘뿐이다: `auth status --json hosts`(메타데이터), `auth token --hostname github.com --user <login>`.
  셸 없이 배열로 실행하고, 인자 형태를 실행 직전에 다시 검사한다.
- 토큰은 `Secret`으로만 다룬다(repr 마스킹, pickle 거부). 메모리에서만 300초 보관한다.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass

from ttakkari.github import targets
from ttakkari.github.errors import GithubError

GH_HOST = "github.com"
GH_TIMEOUT = 5.0
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.]{8,512}$")
_STRIP_ENV = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GH_HOST")
_GH_CANDIDATES = ("/opt/homebrew/bin/gh", "/usr/local/bin/gh", "/usr/bin/gh")

Runner = Callable[[Sequence[str]], Awaitable[tuple[int, bytes]]]


class Secret:
    """토큰 보관 객체. 문자열화·로그·직렬화로 값이 새지 않는다."""

    __slots__ = ("_v",)

    def __init__(self, value: str) -> None:
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:
        return "Secret(***)"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "***"

    def __reduce__(self):
        raise TypeError("Secret 은 직렬화할 수 없습니다.")


@dataclass(frozen=True, slots=True)
class AccountInfo:
    login: str
    host: str
    active: bool
    scopes: list[str]
    state: str


def find_gh() -> str | None:
    override = os.environ.get("TTAKKARI_GH_BIN")
    if override and os.access(override, os.X_OK):
        return override
    found = shutil.which("gh")
    if found:
        return found
    return next((p for p in _GH_CANDIDATES if os.access(p, os.X_OK)), None)


def check_gh_args(args: Sequence[str]) -> None:
    """허용된 gh 호출 형태가 아니면 실행 전에 거부한다."""
    rest = list(args[1:])
    if rest == ["auth", "status", "--json", "hosts"]:
        return
    if (len(rest) == 6 and rest[:5] == ["auth", "token", "--hostname", GH_HOST, "--user"]
            and targets.OWNER_RE.fullmatch(rest[5])):
        return
    raise ValueError("허용되지 않은 gh 호출입니다.")


def build_env(environ: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in environ.items() if k not in _STRIP_ENV}
    env.update({"GH_PROMPT_DISABLED": "1", "NO_COLOR": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    return env


async def default_runner(args: Sequence[str]) -> tuple[int, bytes]:
    check_gh_args(args)
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=build_env(os.environ))
    except OSError as e:
        raise GithubError("no_account", "gh를 실행할 수 없습니다. Mac Studio의 gh 설치를 확인하세요.") from e
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=GH_TIMEOUT)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b""
    return proc.returncode if proc.returncode is not None else 1, out[:65536]


class AccountRegistry:
    def __init__(self, *, runner: Runner = default_runner, gh_bin: str | None = None,
                 clock: Callable[[], float] = time.monotonic, list_ttl: float = 60.0, token_ttl: float = 300.0,
                 min_refresh_gap: float = 5.0) -> None:
        self._runner = runner
        self._gh = gh_bin
        self._clock = clock
        self._list_ttl = list_ttl
        self._token_ttl = token_ttl
        self._gap = min_refresh_gap
        self._accounts: list[AccountInfo] = []
        self._listed_at: float | None = None
        self._lock = asyncio.Lock()
        self._tokens: dict[str, tuple[Secret, float]] = {}
        self._removed: list[Callable[[str], None]] = []

    def on_removed(self, cb: Callable[[str], None]) -> None:
        self._removed.append(cb)

    def _bin(self) -> str:
        gh = self._gh or find_gh()
        if not gh:
            raise GithubError("no_account", "gh를 찾을 수 없습니다. Mac Studio의 gh 설치를 확인하세요.")
        self._gh = gh
        return gh

    @staticmethod
    def _parse(out: bytes) -> list[AccountInfo]:
        try:
            hosts = json.loads(out.decode("utf-8", "replace")).get("hosts", {})
            rows = hosts.get(GH_HOST, []) if isinstance(hosts, dict) else []
        except (ValueError, AttributeError):
            return []
        found: dict[str, AccountInfo] = {}
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            login = row.get("login")
            if not isinstance(login, str) or not targets.OWNER_RE.fullmatch(login):
                continue
            scopes = row.get("scopes")
            found[login.lower()] = AccountInfo(
                login=login, host=GH_HOST, active=row.get("active") is True,
                scopes=[s.strip() for s in scopes.split(",") if s.strip()] if isinstance(scopes, str) else [],
                state="success" if row.get("state") == "success" else "error")
        return list(found.values())

    async def list_accounts(self, *, force: bool = False) -> list[AccountInfo]:
        async with self._lock:
            now = self._clock()
            if not force and self._listed_at is not None and now - self._listed_at < self._list_ttl:
                return list(self._accounts)
            code, out = await self._runner([self._bin(), "auth", "status", "--json", "hosts"])
            fresh = self._parse(out) if code == 0 else []
            gone = {a.login for a in self._accounts} - {a.login for a in fresh}
            self._accounts, self._listed_at = fresh, now
            for login in gone:
                self._tokens.pop(login, None)
                for cb in self._removed:
                    cb(login)
            return list(fresh)

    async def require(self, login: str) -> AccountInfo:
        login = targets.owner(login)
        accounts = await self.list_accounts()
        found = next((a for a in accounts if a.login.lower() == login.lower()), None)
        if found is None and (self._listed_at is None or self._clock() - self._listed_at >= self._gap):
            accounts = await self.list_accounts(force=True)  # 방금 gh auth login 으로 추가된 계정
            found = next((a for a in accounts if a.login.lower() == login.lower()), None)
        if not accounts:
            raise GithubError("no_account")
        if found is None:
            raise GithubError("account_unavailable")
        if found.state != "success":
            raise GithubError("gh_auth_expired")
        return found

    def invalidate(self, login: str) -> None:
        self._tokens.pop(login, None)

    async def token(self, login: str, *, fresh: bool = False) -> Secret:
        info = await self.require(login)
        cached = self._tokens.get(info.login)
        if cached and not fresh and self._clock() < cached[1]:
            return cached[0]
        secret = await self._run_token(info.login)
        if secret is None:
            self._tokens.pop(info.login, None)
            self._listed_at = None  # 로그아웃 가능성: 다음 요청에서 목록을 다시 읽는다
            raise GithubError("gh_auth_expired")
        self._tokens[info.login] = (secret, self._clock() + self._token_ttl)
        return secret

    async def _run_token(self, login: str) -> Secret | None:
        code, out = await self._runner([self._bin(), "auth", "token", "--hostname", GH_HOST, "--user", login])
        value = out.decode("utf-8", "replace").strip()
        if code != 0 or not _TOKEN_RE.fullmatch(value):
            return None
        return Secret(value)

    async def probe(self, login: str) -> bool:
        """토큰을 받을 수 있는지만 본다(종료코드·형식). 값은 버린다."""
        info = await self.require(login)
        return await self._run_token(info.login) is not None
