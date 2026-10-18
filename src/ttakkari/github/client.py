"""GitHub REST 읽기 전용 호출 계층(ADR 0042 §5·§6, dev-api-integration).

- 호스트는 api.github.com 고정, GET 만, 경로는 아래 템플릿(ROUTES)에 맞는 것만. 사용자 입력 URL은 여기까지 오지 않는다.
- 전체 예산(기본 15초)은 호출 한 번이 아니라 라우트(요청) 하나의 마감(`current_deadline`)이다. 계정 확인·큐 대기·토큰 획득·
  중첩된 모든 호출·시도·백오프가 한 마감을 나눠 쓴다. 마감이 없는 직접 호출(테스트·스크립트)만 호출마다 새 예산을 갖는다.
  시도 3회, 5xx·전송 오류만 재시도.
- 계정별 캐시(ETag 재검증)·stale-if-error(최대 STALE_MAX_AGE, 접근 상실 응답을 본 키는 폐기)·한도 상태·동시성 상한(계정 4, 전체 8). 상류 401 은 토큰을 한 번 다시 받아 재시도하고 끝내 401 이면 502.
- 응답은 스트림으로 읽으며 상한을 넘으면 끊는다. 상류 오류 본문은 분류에만 쓰고 전달하지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx

from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.cache import TTLCache
from ttakkari.github.errors import GithubError
from ttakkari.github.schemas import Rate

log = logging.getLogger("ttakkari.github")

API_BASE = "https://api.github.com"
GH_HOST = "github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "ttakkari-github-explorer"
DEFAULT_ACCEPT = "application/vnd.github+json"
BUDGET = 15.0
BACKOFF = (1.0, 2.0)
MAX_ATTEMPTS = 3
MAX_JSON = 2 * 1024 * 1024
ERROR_BODY_CAP = 64 * 1024
LOW_REMAINING = 100
_KEEP = frozenset({"etag", "link", "location", "retry-after", "x-ratelimit-limit", "x-ratelimit-remaining",
                   "x-ratelimit-reset", "x-ratelimit-resource", "x-github-sso", "x-oauth-scopes",
                   "x-accepted-oauth-scopes"})
STALE_OK = frozenset({"gh_rate_limited", "gh_timeout", "gh_upstream_error"})
# 마지막으로 상류에서 받았거나 304 로 다시 확인한 뒤 이 초가 지나면 상류 장애에도 stale 로 내지 않는다(비공개 데이터 노출 창을 닫는다).
# 10분: 일시 장애·짧은 한도 소진은 넘기되, 권한 철회가 가려지는 시간이 전체 SHA 고정 TTL(10분)보다 길지 않게 둔다.
STALE_MAX_AGE = 600.0
# 이 오류를 본 키의 캐시는 버린다: 접근 권한이 사라졌거나(401·403·404) 항목이 사라진 것(410·451)이라 이전 응답을 다시 내면 안 된다.
# 한도(429·소진 403)·5xx·타임아웃은 제외: 접근 상실이 아니라 일시 장애다.
DROP_ON = frozenset({"gh_not_found", "gh_forbidden", "gh_gone", "gh_unavailable_legal", "gh_auth_expired"})

# 라우트 하나의 마감(asyncio loop.time() 기준 절대 시각). routes_common.get_gh 가 요청 시작에 열고 끝에 닫는다.
current_deadline: ContextVar[float | None] = ContextVar("gh_deadline", default=None)

_S = r"[A-Za-z0-9._~%\-]+"
_N = r"[0-9]+"
_R = rf"/repos/{_S}/{_S}"
ROUTES: dict[str, re.Pattern[str]] = {k: re.compile(v) for k, v in {
    "user": r"/user",
    "user_orgs": r"/user/orgs",
    "user_repos": r"/user/repos",
    "users": rf"/users/{_S}",
    "orgs": rf"/orgs/{_S}",
    "org_repos": rf"/orgs/{_S}/repos",
    "users_repos": rf"/users/{_S}/repos",
    "repo": _R,
    "branches": rf"{_R}/branches",
    "matching_refs": rf"{_R}/git/matching-refs/(?:heads|tags)/{_S}",
    "git_ref": rf"{_R}/git/ref/(?:heads|tags)/{_S}(?:/{_S})*",
    "commit": rf"{_R}/commits/[0-9A-Fa-f]{{7,40}}",
    "contents": rf"{_R}/contents(?:/{_S})*",
    "readme": rf"{_R}/readme",
    "pulls": rf"{_R}/pulls",
    "pull": rf"{_R}/pulls/{_N}",
    "pull_files": rf"{_R}/pulls/{_N}/files",
    "issues": rf"{_R}/issues",
    "issue": rf"{_R}/issues/{_N}",
    "issue_comments": rf"{_R}/issues/{_N}/comments",
    "runs": rf"{_R}/actions/runs",
    "run": rf"{_R}/actions/runs/{_N}",
    "run_jobs": rf"{_R}/actions/runs/{_N}/jobs",
    "job_logs": rf"{_R}/actions/jobs/{_N}/logs",
}.items()}
_REPO_MOVED = re.compile(r"^/repositories/[0-9]+(?:/[A-Za-z0-9._~%\-]+)*$")
_NEXT = re.compile(r'<([^>]*)>\s*;\s*rel="next"')
_IMPLIED = {  # 부여된 scope 가 암묵적으로 포함하는 scope
    "repo": {"public_repo", "repo:status", "repo_deployment", "repo:invite", "security_events"},
    "admin:org": {"write:org", "read:org"}, "write:org": {"read:org"},
    "user": {"read:user", "user:email", "user:follow"}, "workflow": set(),
}


def next_page(link: str | None) -> int | None:
    m = _NEXT.search(link or "")
    if not m:
        return None
    value = parse_qs(urlsplit(m.group(1)).query).get("page", [""])[0]
    return int(value) if value.isdigit() and 0 < int(value) <= 100_000 else None


@dataclass(slots=True)
class Upstream:
    status: int
    body: bytes
    etag: str | None
    next_page: int | None
    fetched_at: datetime
    location: str | None = None
    truncated: bool = False
    cached: bool = False
    stale: bool = False
    rate: Rate | None = None

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (ValueError, RecursionError) as e:  # JSONDecodeError·UnicodeDecodeError·2MB 안에서도 가능한 깊은 중첩
            raise GithubError("gh_schema_changed") from e


@dataclass(slots=True)
class _Raw:
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool


def _check_path(kind: str, path: str) -> None:
    pattern = ROUTES[kind]  # 모르는 kind 는 KeyError(프로그래머 오류)
    if not pattern.fullmatch(path) or any(unquote(seg) in (".", "..") for seg in path.split("/")):
        raise GithubError("invalid_target", "허용되지 않은 GitHub 경로입니다.")


def _int(headers: dict[str, str], key: str) -> int | None:
    v = headers.get(key, "")
    return int(v) if v.isdigit() else None


def _scopes(value: str | None) -> set[str]:
    return {s.strip() for s in (value or "").split(",") if s.strip()}


def _expand(granted: set[str]) -> set[str]:
    out = set(granted)
    for g in granted:
        out |= _IMPLIED.get(g, set())
    return out


async def under_deadline(awaitable: Awaitable[Any]) -> Any:
    """라우트 마감이 열려 있으면 그 안에서만 기다린다(client.get 밖의 계정 확인 같은 대기용). 넘으면 gh_timeout."""
    at = current_deadline.get()
    if at is None:
        return await awaitable
    try:
        async with asyncio.timeout_at(at):
            return await awaitable
    except TimeoutError as e:
        raise GithubError("gh_timeout") from e


class GithubClient:
    def __init__(self, registry: AccountRegistry, *, transport: httpx.AsyncBaseTransport | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 rng: Callable[[], float] = random.random, budget: float = BUDGET,
                 backoff: tuple[float, ...] = BACKOFF, per_login: int = 4, total: int = 8,
                 cache: TTLCache | None = None) -> None:
        self._reg = registry
        self._transport = transport
        self._wall = wall
        self._sleep = sleep
        self._rng = rng
        self._budget = budget
        self._backoff = backoff
        self._per_login_limit = per_login
        self._cache = cache or TTLCache(clock=clock)
        self._total = asyncio.Semaphore(total)
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._rates: dict[str, Rate] = {}
        self._http_client: httpx.AsyncClient | None = None
        registry.on_removed(self._forget)

    def _forget(self, login: str) -> None:
        self._cache.purge_login(login)
        self._rates.pop(login, None)
        self._sems.pop(login, None)

    @property
    def registry(self) -> AccountRegistry:
        return self._reg

    @property
    def budget(self) -> float:
        return self._budget

    def rate_of(self, login: str) -> Rate | None:
        return self._rates.get(login)

    def _http(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                base_url=API_BASE, transport=self._transport, follow_redirects=False, trust_env=False,
                headers={"User-Agent": USER_AGENT, "X-GitHub-Api-Version": API_VERSION},
                timeout=httpx.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8))
        return self._http_client

    async def aclose(self) -> None:
        if self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    # ---- 공개 진입점 ----

    async def get(self, login: str, kind: str, path: str, *, params: dict[str, Any] | None = None,
                  accept: str | None = None, ttl: float = 30.0, max_bytes: int = MAX_JSON, truncate: bool = False,
                  statuses: tuple[int, ...] = (200,), budget: float | None = None) -> Upstream:
        _check_path(kind, path)
        loop = asyncio.get_running_loop()
        started = loop.time()
        scoped = current_deadline.get()
        if scoped is None:
            deadline = started + (budget if budget is not None else self._budget)
        else:  # 라우트 마감 안의 호출은 새 예산을 시작하지 않는다(명시한 budget 은 더 줄이는 용도로만 쓴다)
            deadline = scoped if budget is None else min(scoped, started + budget)
        held: dict[str, Any] = {}
        failure: GithubError
        try:
            async with asyncio.timeout_at(deadline):
                info = await self._reg.require(login)
                login = info.login
                norm = tuple(sorted((k, str(v)) for k, v in (params or {}).items() if v is not None))
                key = (GH_HOST, login, kind, path, norm, accept or "")
                held["key"] = key
                entry, fresh = self._cache.get(key)
                held["entry"] = entry
                if entry is not None and fresh:
                    return self._served(entry.payload, login, cached=True, stale=False)
                if loop.time() >= deadline:
                    raise TimeoutError  # 앞선 호출이 마감을 다 썼다: 기다리지 않고 timeout(stale 허용 여부는 아래에서)
                self._gate(login)
                async with self._login_sem(login), self._total:
                    up = await self._attempts(login, key, path, norm, accept, ttl, max_bytes, truncate, statuses,
                                              entry.payload if entry else None, deadline)
                log.info("github get kind=%s status=%s cached=%s ms=%d", kind, up.status, up.cached,
                         (loop.time() - started) * 1000)
                return up
        except TimeoutError:
            failure = GithubError("gh_timeout")
        except GithubError as e:
            failure = e
        entry = held.get("entry")
        if failure.code in DROP_ON and "key" in held:
            self._drop_after_access_loss(login, kind, path, held["key"])
            entry = None
        if entry is not None and failure.code in STALE_OK and self._cache.age(entry) <= STALE_MAX_AGE:
            log.warning("github stale kind=%s code=%s", kind, failure.code)
            return self._served(entry.payload, login, cached=True, stale=True)
        log.warning("github error kind=%s code=%s", kind, failure.code)
        raise failure

    def _drop_after_access_loss(self, login: str, kind: str, path: str, key: tuple) -> None:
        """접근 상실을 본 키를 버린다. 저장소 자체(`repo`)를 잃었으면 그 저장소 아래 같은 계정의 캐시도 함께 버린다."""
        self._cache.drop(key)
        if kind == "repo":
            base = path.lower()
            self._cache.drop_where(lambda k: k[1] == login and (k[3].lower() == base or k[3].lower().startswith(base + "/")))

    # ---- 내부 ----

    def _login_sem(self, login: str) -> asyncio.Semaphore:
        return self._sems.setdefault(login, asyncio.Semaphore(self._per_login_limit))

    def _served(self, stored: Upstream, login: str, *, cached: bool, stale: bool) -> Upstream:
        return replace(stored, cached=cached, stale=stale, rate=self._rates.get(login))

    def _gate(self, login: str) -> None:
        rate = self._rates.get(login)
        if rate and rate.remaining == 0 and rate.reset and self._wall() < rate.reset:
            raise GithubError("gh_rate_limited", reset=rate.reset, retry_after=max(1, int(rate.reset - self._wall())))

    def _note_rate(self, login: str, headers: dict[str, str]) -> None:
        if not any(k.startswith("x-ratelimit-") for k in headers):
            return
        remaining = _int(headers, "x-ratelimit-remaining")
        self._rates[login] = Rate(
            limit=_int(headers, "x-ratelimit-limit"), remaining=remaining, reset=_int(headers, "x-ratelimit-reset"),
            resource=headers.get("x-ratelimit-resource"), low=remaining is not None and remaining <= LOW_REMAINING)

    async def _attempts(self, login: str, key: tuple, path: str, norm: tuple, accept: str | None, ttl: float,
                        max_bytes: int, truncate: bool, statuses: tuple[int, ...], stored: Upstream | None,
                        deadline: float) -> Upstream:
        loop = asyncio.get_running_loop()
        token = await self._reg.token(login)
        target, params = path, dict(norm) or None
        refreshed = redirected = False
        attempt = 0
        etag = stored.etag if stored else None
        while True:
            attempt += 1
            raw: _Raw | None = None
            try:
                raw = await self._once(token, target, params, accept, etag, max_bytes, truncate)
            except httpx.HTTPError:
                raw = None  # 전송 오류·타임아웃: 재시도 대상
            if raw is not None:
                self._note_rate(login, raw.headers)
                status = raw.status
                if status == 200 or status in statuses:
                    up = Upstream(status=status, body=raw.body, etag=raw.headers.get("etag"),
                                  next_page=next_page(raw.headers.get("link")),
                                  fetched_at=datetime.now(UTC), location=raw.headers.get("location"),
                                  truncated=raw.truncated, rate=self._rates.get(login))
                    if status == 200 and not raw.truncated:
                        self._cache.put(key, up, size=len(raw.body) + 256, ttl=ttl, login=login)
                    return up
                if status == 304 and stored is not None:
                    self._cache.touch(key, ttl)
                    return self._served(stored, login, cached=True, stale=False)
                if status == 401:
                    if refreshed:
                        raise GithubError("gh_auth_expired")
                    refreshed = True
                    self._reg.invalidate(login)
                    token = await self._reg.token(login, fresh=True)
                    attempt -= 1
                    continue
                if status in (301, 302, 303, 307, 308):
                    moved = self._moved_target(raw.headers.get("location"))
                    if moved is None or redirected or status in (302, 303):
                        raise GithubError("gh_upstream_error", "예상하지 못한 리다이렉트입니다.")
                    redirected, target, params = True, moved, None
                    attempt -= 1
                    continue
                if status not in (500, 502, 503, 504):
                    raise self._error(status, raw)
            # 재시도 가능: 전송 오류, 5xx
            if attempt >= MAX_ATTEMPTS or attempt > len(self._backoff):
                raise GithubError("gh_upstream_error")
            delay = self._backoff[attempt - 1] * (0.5 + self._rng())
            if delay >= deadline - loop.time():
                raise GithubError("gh_upstream_error")  # 남은 예산보다 길게 기다리지 않는다
            await self._sleep(delay)

    @staticmethod
    def _moved_target(location: str | None) -> str | None:
        """저장소 이름 변경 301 만 따른다: api.github.com 의 /repositories/<id>/... 로 한 번."""
        if not location:
            return None
        parts = urlsplit(location)
        if parts.scheme != "https" or (parts.hostname or "").lower() != "api.github.com" or parts.port is not None:
            return None
        if not _REPO_MOVED.fullmatch(parts.path) or any(unquote(s) in (".", "..") for s in parts.path.split("/")):
            return None
        return parts.path + (f"?{parts.query}" if parts.query else "")

    async def _once(self, token, target: str, params: dict[str, str] | None, accept: str | None, etag: str | None,
                    max_bytes: int, truncate: bool) -> _Raw:
        headers = {"Authorization": f"Bearer {token.reveal()}", "Accept": accept or DEFAULT_ACCEPT}
        if etag:
            headers["If-None-Match"] = etag
        async with self._http().stream("GET", target, params=params, headers=headers) as r:
            hdrs = {k.lower(): v for k, v in r.headers.items() if k.lower() in _KEEP}
            ok = r.status_code == 200
            length = r.headers.get("content-length", "")
            if ok and not truncate and length.isdigit() and int(length) > max_bytes:
                raise GithubError("gh_too_large")
            limit = max_bytes if ok else ERROR_BODY_CAP
            buf = bytearray()
            truncated = False
            async for chunk in r.aiter_bytes():
                room = limit - len(buf)
                if len(chunk) > room:
                    if not ok:
                        buf += chunk[:room]
                        break
                    if truncate:
                        buf += chunk[:room]
                        truncated = True
                        break
                    raise GithubError("gh_too_large")
                buf += chunk
            return _Raw(r.status_code, hdrs, bytes(buf), truncated)

    def _error(self, status: int, raw: _Raw) -> GithubError:
        message = ""
        try:
            data = json.loads(raw.body)
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                message = data["message"].lower()
        except (ValueError, RecursionError):
            pass
        if status in (403, 429):
            return self._forbidden(status, raw.headers, message)
        if status == 404:
            return GithubError("gh_empty_repository") if "repository is empty" in message else GithubError("gh_not_found")
        if status == 410:
            return GithubError("gh_gone")
        if status == 451:
            return GithubError("gh_unavailable_legal")
        if status == 409:
            return GithubError("gh_empty_repository") if "empty" in message else GithubError("gh_unprocessable")
        if status in (400, 406, 422):
            return GithubError("gh_unprocessable")
        return GithubError("gh_upstream_error")

    def _forbidden(self, status: int, h: dict[str, str], message: str) -> GithubError:
        remaining, retry, reset = _int(h, "x-ratelimit-remaining"), _int(h, "retry-after"), _int(h, "x-ratelimit-reset")
        if status == 429 or retry is not None or remaining == 0 or "rate limit" in message:
            now = self._wall()
            if retry is None:
                retry = max(1, reset - int(now)) if reset is not None and reset > now else 60
            return GithubError("gh_rate_limited", reset=reset if reset is not None and reset > now else int(now) + retry,
                               retry_after=retry)
        sso = h.get("x-github-sso")
        if sso:
            m = re.search(r"url=(\S+)", sso)
            extra = {"sso_url": m.group(1).rstrip(";")} if m and m.group(1).startswith("https://github.com/") else {}
            return GithubError("gh_forbidden", reason="sso_required", extra=extra)
        accepted = _scopes(h.get("x-accepted-oauth-scopes"))
        granted = h.get("x-oauth-scopes")
        # X-Accepted-OAuth-Scopes 는 "이 중 하나면 된다"는 목록이다.
        if granted is not None and accepted and not (accepted & _expand(_scopes(granted))):
            return GithubError("gh_forbidden", reason="scope_missing")
        return GithubError("gh_forbidden", reason="policy")
