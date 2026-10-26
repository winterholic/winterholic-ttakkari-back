"""Collector Platform: policy-, robots- and pacing-aware fetch on top of SafeFetcher (CP-S2-03/04).

Order for every URL: source policy (host/scheme/port/path) -> durable host cooldown -> robots.txt (cached, RFC 9309)
-> paced GET. Each step can only make the fetch stricter. robots.txt allowing a path is politeness, not a grant: the
source policy must separately allow the purpose before the pipeline calls this at all.

Failure mapping the caller relies on:
- PolicyBlocked: policy or robots forbids this URL. Never retried, becomes a `blocked` item.
- FetchError(retryable=True): timeout / 429 / 5xx / unreachable robots. A host cooldown is stored first, so the next
  attempt (even by another process) waits instead of hammering the host.
- FetchError(retryable=False) / UrlRejected: this URL cannot be fetched; recorded on the item.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from ttakkari.collector import robots
from ttakkari.collector.fetcher import FetchError, FetchResult, SafeFetcher
from ttakkari.collector.netguard import ParsedUrl, UrlRejected, normalize_url
from ttakkari.collector.policy import SourcePolicySpec

ROBOTS_TTL_SECONDS = {"parse": 86400, "no_rules": 86400, "forbidden": 3600, "unreachable": 600}
DEFAULT_COOLDOWN_SECONDS = 60.0
UNREACHABLE_COOLDOWN_SECONDS = 600.0


class PolicyBlocked(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class CachedRobots:
    status: str  # parse | no_rules | forbidden | unreachable
    body: str | None
    fetched_at: datetime
    expires_at: datetime


class HostState(Protocol):
    async def get_robots(self, origin: str) -> CachedRobots | None: ...

    async def put_robots(self, origin: str, cached: CachedRobots) -> None: ...

    async def get_cooldown(self, origin: str) -> datetime | None: ...

    async def set_cooldown(self, origin: str, until: datetime, reason: str) -> None: ...

    async def reserve_slot(self, origin: str, interval: float, now: datetime) -> float:
        """Reserve the next request slot and return how many seconds to wait for it."""
        ...


class MemoryHostState:
    """In-process HostState for unit tests and single-process runs. The DB version is store.DbHostState."""

    def __init__(self) -> None:
        self.robots: dict[str, CachedRobots] = {}
        self.cooldowns: dict[str, tuple[datetime, str]] = {}
        self.next_allowed: dict[str, datetime] = {}

    async def get_robots(self, origin):
        return self.robots.get(origin)

    async def put_robots(self, origin, cached):
        self.robots[origin] = cached

    async def get_cooldown(self, origin):
        entry = self.cooldowns.get(origin)
        return entry[0] if entry else None

    async def set_cooldown(self, origin, until, reason):
        current = self.cooldowns.get(origin)
        if current is None or until > current[0]:
            self.cooldowns[origin] = (until, reason)

    async def reserve_slot(self, origin, interval, now):
        slot = max(now, self.next_allowed.get(origin, now))
        self.next_allowed[origin] = slot + timedelta(seconds=interval)
        return (slot - now).total_seconds()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class PoliteResult:
    result: FetchResult
    robots: str  # allowed | not_applicable


class PoliteFetcher:
    def __init__(
        self,
        fetcher: SafeFetcher,
        host_state: HostState,
        *,
        product_token: str,
        clock: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.fetcher = fetcher
        self.host_state = host_state
        self.product_token = product_token
        self.clock = clock
        self.sleep = sleep

    async def fetch(
        self,
        url: ParsedUrl,
        policy: SourcePolicySpec,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
    ) -> PoliteResult:
        if (reason := policy.check_url(url)) is not None:
            raise PolicyBlocked(reason, url.url)
        now = self.clock()
        until = await self.host_state.get_cooldown(url.origin)
        if until is not None and until > now:
            raise FetchError("host_cooldown", url.origin, retryable=True, retry_after=(until - now).total_seconds())

        robots_state = "not_applicable"

        async def pace(hop: ParsedUrl) -> None:
            nonlocal robots_state
            hop_now = self.clock()
            hop_until = await self.host_state.get_cooldown(hop.origin)
            if hop_until is not None and hop_until > hop_now:
                raise FetchError("host_cooldown", hop.origin, retryable=True, retry_after=(hop_until - hop_now).total_seconds())

            delay = policy.min_delay_seconds
            if policy.robots_required:
                rules = await self._rules(hop, policy)
                if rules.unavailable:
                    raise PolicyBlocked("robots_forbidden", hop.origin)
                if not rules.allowed(hop.target):
                    raise PolicyBlocked("robots_disallowed", hop.target)
                robots_state = "allowed"
                delay = max(delay, rules.crawl_delay or 0.0)

            wait = await self.host_state.reserve_slot(hop.origin, delay, self.clock())
            if wait > 0:
                await self.sleep(wait)

        kwargs = {"accept": accept} if accept else {}
        try:
            result = await self.fetcher.fetch(
                url, allowed_schemes=policy.schemes, allowed_ports=policy.ports, host_check=policy.check_url,
                before_request=pace, etag=etag, last_modified=last_modified, **kwargs,
            )
        except FetchError as e:
            if e.retryable:
                await self._cool(url.origin, e)
            raise
        return PoliteResult(result, robots_state)

    async def _cool(self, origin: str, error: FetchError) -> None:
        if error.code == "host_cooldown":
            return
        seconds = max(error.retry_after or 0.0, DEFAULT_COOLDOWN_SECONDS)
        await self.host_state.set_cooldown(origin, self.clock() + timedelta(seconds=seconds), error.code)

    async def _rules(self, url: ParsedUrl, policy: SourcePolicySpec) -> robots.RobotsRules:
        now = self.clock()
        cached = await self.host_state.get_robots(url.origin)
        if cached is None or cached.expires_at <= now:
            cached = await self._fetch_robots(url, policy)
        if cached.status == "unreachable":
            await self.host_state.set_cooldown(
                url.origin, cached.fetched_at + timedelta(seconds=UNREACHABLE_COOLDOWN_SECONDS), "robots_unreachable"
            )
            raise FetchError("robots_unreachable", url.origin, retryable=True, retry_after=UNREACHABLE_COOLDOWN_SECONDS)
        if cached.status == "forbidden":
            return robots.UNAVAILABLE
        if cached.status == "no_rules" or not cached.body:
            return robots.ALLOW_ALL
        return robots.rules_for(robots.parse(cached.body), self.product_token)

    async def _fetch_robots(self, url: ParsedUrl, policy: SourcePolicySpec) -> CachedRobots:
        robots_url = normalize_url(
            f"{url.scheme}://{url.origin.split('://', 1)[1]}/robots.txt",
            allowed_schemes=policy.schemes, allowed_ports=policy.ports, strip_tracking=False,
        )

        async def pace(hop: ParsedUrl) -> None:
            wait = await self.host_state.reserve_slot(hop.origin, policy.min_delay_seconds, self.clock())
            if wait > 0:
                await self.sleep(wait)

        status, body = "unreachable", None
        def robots_hop(target: ParsedUrl) -> str | None:
            # A file-scoped grant still needs the origin's control file. This
            # exception applies only to robots acquisition, never document fetch.
            if target.origin == robots_url.origin and target.target == "/robots.txt":
                return None
            return policy.check_url(target)

        try:
            fetched = await self.fetcher.fetch(
                robots_url, allowed_schemes=policy.schemes, allowed_ports=policy.ports, host_check=robots_hop,
                before_request=pace, accept="text/plain,*/*;q=0.1", max_bytes=robots.MAX_ROBOTS_BYTES,
            )
            status = "parse"
            body = fetched.body.decode("utf-8", errors="replace")
        except FetchError as e:
            if e.code == "empty_response":
                status = "no_rules"  # an empty robots.txt is a valid "no restrictions"
            elif e.code == "response_too_large":
                status = "unreachable"  # a truncated rule file is not trusted; the fetch waits for a smaller one
            elif e.status is not None:
                status = robots.classify_status(e.status)
            else:
                status = "unreachable"
        except UrlRejected:
            status = "unreachable"
        now = self.clock()
        cached = CachedRobots(status, body, now, now + timedelta(seconds=ROBOTS_TTL_SECONDS[status]))
        await self.host_state.put_robots(url.origin, cached)
        return cached
