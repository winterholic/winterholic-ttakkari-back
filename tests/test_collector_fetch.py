# ruff: noqa: RUF059  (polite() returns a tuple; each test uses the parts it needs)
"""CP-S2-04: safe fetch, robots, pacing, cooldown and SSRF negatives against a local fixture server.

No public network. The loopback guard in collector_support lets fixture hostnames reach 127.0.0.1; every SSRF test
that must be rejected uses the default guard or a resolver that answers with a non-public address.
"""
from __future__ import annotations

import asyncio
import gzip
import ipaddress
import zlib
from datetime import UTC, datetime, timedelta

import pytest
from aiohttp import web
from collector_support import FixtureServer, fake_resolver, loopback_guard, recorder

from ttakkari.collector import robots
from ttakkari.collector.fetcher import (
    FetchError,
    FetchLimits,
    SafeFetcher,
    parse_retry_after,
)
from ttakkari.collector.netguard import NetGuard, UrlRejected, normalize_url
from ttakkari.collector.policy import SourcePolicySpec
from ttakkari.collector.polite import MemoryHostState, PolicyBlocked, PoliteFetcher

record = recorder()
UA = "TtakkariCollector/1.0 (+test)"


def spec_for(server: FixtureServer, **over) -> SourcePolicySpec:
    d = {
        "allowed_hosts": ["docs.test"], "schemes": ["http"], "ports": [server.port],
        "permissions": {"store": "allowed"}, "license_id": "CC-BY-4.0", "policy_url": "https://example.test/terms",
        "checked_at": "2026-10-21T00:00:00+09:00", "basis": "fixture terms section 1", "min_delay_seconds": 0.0,
    }
    d.update(over)
    return SourcePolicySpec.from_dict(d)


def parsed(server: FixtureServer, path: str, host: str = "docs.test"):
    return normalize_url(server.url(path, host), allowed_schemes=("http",), allowed_ports=(server.port,))


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


@pytest.fixture
async def server():
    async with FixtureServer() as s:
        yield s


def fetcher(*hosts: str, limits: FetchLimits | None = None, calls: list[str] | None = None) -> SafeFetcher:
    return SafeFetcher(loopback_guard(*hosts, calls=calls), user_agent=UA, limits=limits)


async def fetch(f: SafeFetcher, server: FixtureServer, path: str, **kw):
    kw.setdefault("allowed_schemes", ("http",))
    kw.setdefault("allowed_ports", (server.port,))
    try:
        return await f.fetch(parsed(server, path), **kw)
    finally:
        pass


# ---- happy path and request hygiene --------------------------------------------------------------------------

async def test_fetch_sends_user_agent_no_cookies_and_ignores_environment_proxy(server, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")  # would fail the test if the proxy were honored
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")

    async def with_cookie(request):
        return web.Response(text="hello world", headers={"Set-Cookie": "sid=secret; Path=/"})

    server.routes["/a"] = with_cookie
    f = fetcher("docs.test")
    try:
        first = await fetch(f, server, "/a")
        await fetch(f, server, "/a")
    finally:
        await f.aclose()
    assert first.body == b"hello world" and first.status == 200
    for _, _, headers in server.hits:
        assert headers["User-Agent"] == UA
        assert "Cookie" not in headers  # the cookie jar is cleared after every response
        assert "Authorization" not in headers


async def test_conditional_request_and_304(server):
    async def handler(request):
        if request.headers.get("If-None-Match") == '"v1"':
            return web.Response(status=304, headers={"ETag": '"v1"'})
        return web.Response(text="body text", headers={"ETag": '"v1"', "Last-Modified": "Wed, 21 Oct 2026 07:28:00 GMT"})

    server.routes["/c"] = handler
    f = fetcher("docs.test")
    try:
        full = await fetch(f, server, "/c")
        again = await fetch(f, server, "/c", etag=full.etag, last_modified=full.last_modified)
    finally:
        await f.aclose()
    assert full.etag == '"v1"' and not full.not_modified
    assert again.not_modified and again.status == 304 and again.body == b""
    assert server.hits[-1][2]["If-None-Match"] == '"v1"'
    assert server.hits[-1][2]["If-Modified-Since"] == "Wed, 21 Oct 2026 07:28:00 GMT"


# ---- bounded reads ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["too_large_identity", "gzip_bomb", "deflate_bomb", "empty_200", "br_unsupported", "truncated_gzip"])
async def test_body_limits_fail_explicitly(server, name):
    limits = FetchLimits(max_bytes=200_000)
    zeros = b"\0" * 50_000_000

    async def big(request):
        return web.Response(body=b"x" * 300_000, content_type="text/plain")

    async def gz(request):
        return web.Response(body=gzip.compress(zeros), headers={"Content-Encoding": "gzip"}, content_type="text/plain")

    async def df(request):
        return web.Response(body=zlib.compress(zeros), headers={"Content-Encoding": "deflate"}, content_type="text/plain")

    async def empty(request):
        return web.Response(body=b"", content_type="text/html")

    async def br(request):
        return web.Response(body=b"abc", headers={"Content-Encoding": "br"}, content_type="text/plain")

    async def trunc(request):
        return web.Response(body=gzip.compress(b"hello " * 5000)[:30], headers={"Content-Encoding": "gzip"}, content_type="text/plain")

    server.routes["/p"] = {"too_large_identity": big, "gzip_bomb": gz, "deflate_bomb": df, "empty_200": empty,
                           "br_unsupported": br, "truncated_gzip": trunc}[name]
    expected = {"too_large_identity": "response_too_large", "gzip_bomb": "response_too_large",
                "deflate_bomb": "response_too_large", "empty_200": "empty_response",
                "br_unsupported": "content_encoding_unsupported", "truncated_gzip": "content_encoding_invalid"}[name]
    f = fetcher("docs.test", limits=limits)
    try:
        with pytest.raises(FetchError) as err:
            await fetch(f, server, "/p")
    finally:
        await f.aclose()
    assert err.value.code == expected and not err.value.retryable
    record("fetch-negative", {"case": name, "expected": expected, "observed": err.value.code, "passed": True})


async def test_total_timeout_is_retryable(server):
    async def slow(request):
        await asyncio.sleep(2)
        return web.Response(text="late")

    server.routes["/slow"] = slow
    f = fetcher("docs.test", limits=FetchLimits(total_timeout=0.3))
    try:
        with pytest.raises(FetchError) as err:
            await fetch(f, server, "/slow")
    finally:
        await f.aclose()
    assert err.value.code == "timeout" and err.value.retryable
    record("fetch-negative", {"case": "timeout", "expected": "timeout", "observed": err.value.code, "passed": True})


@pytest.mark.parametrize(("status", "code", "retryable"), [
    (404, "not_found", False), (410, "not_found", False), (403, "access_denied", False), (401, "access_denied", False),
    (429, "rate_limited", True), (503, "server_error", True), (500, "server_error", True), (418, "unexpected_status", False),
])
async def test_status_mapping(server, status, code, retryable):
    async def handler(request):
        return web.Response(status=status, text="x", headers={"Retry-After": "120"} if status in (429, 503) else {})

    server.routes["/s"] = handler
    f = fetcher("docs.test")
    try:
        with pytest.raises(FetchError) as err:
            await fetch(f, server, "/s")
    finally:
        await f.aclose()
    assert (err.value.code, err.value.retryable) == (code, retryable)
    if status in (429, 503):
        assert err.value.retry_after == 120
    record("fetch-negative", {"case": f"http_{status}", "expected": code, "observed": err.value.code, "passed": True})


def test_retry_after_parsing_caps_and_dates():
    now = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
    assert parse_retry_after("90") == 90
    assert parse_retry_after("999999999") == 24 * 3600
    assert parse_retry_after("Wed, 21 Oct 2026 12:05:00 GMT", now) == 300
    assert parse_retry_after("Wed, 21 Oct 2026 11:00:00 GMT", now) == 0
    assert parse_retry_after("soon") is None and parse_retry_after(None) is None


# ---- SSRF ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.1.2.3", "172.16.0.9", "192.168.1.1", "169.254.169.254", "0.0.0.0", "100.64.0.1", "224.0.0.1",
    "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "2002:7f00:1::1", "64:ff9b::7f00:1", "::",
])
async def test_non_public_addresses_are_rejected_as_literals_and_as_dns_answers(address):
    guard = NetGuard(resolver=fake_resolver({"evil.test": [address]}))
    with pytest.raises(UrlRejected) as lit:
        await guard.resolve(address, 443)
    with pytest.raises(UrlRejected) as dns:
        await guard.resolve("evil.test", 443)
    assert lit.value.code == dns.value.code == "address_not_public"
    record("ssrf-negative", {"case": f"address {address}", "expected": "address_not_public", "observed": dns.value.code, "passed": True})


async def test_one_private_record_among_public_ones_rejects_the_host():
    guard = NetGuard(resolver=fake_resolver({"mixed.test": ["93.184.216.34", "10.0.0.7"]}))
    with pytest.raises(UrlRejected) as err:
        await guard.resolve("mixed.test", 443)
    assert err.value.code == "address_not_public"
    ok = NetGuard(resolver=fake_resolver({"fine.test": ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]}))
    assert await ok.resolve("fine.test", 443) == ["93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"]


@pytest.mark.parametrize(("url", "code"), [
    ("file:///etc/passwd", "scheme_not_allowed"), ("gopher://x.test/", "scheme_not_allowed"),
    ("ftp://x.test/f", "scheme_not_allowed"), ("javascript:alert(1)", "scheme_not_allowed"),
    ("https://user:pw@x.test/", "userinfo_not_allowed"), ("https://x.test@evil.test/", "userinfo_not_allowed"),
    ("https://x.test:8443/", "port_not_allowed"), ("https://2130706433/", "host_ambiguous_ip"),
    ("https://0x7f.1/", "host_ambiguous_ip"), ("https://127.1/", "host_ambiguous_ip"), ("", "url_invalid"),
    ("https://x.test/\r\nHost: evil", "url_invalid"), ("https://" + "a" * 70 + ".test/", "url_invalid"),
])
def test_url_rejections_happen_before_any_dns_lookup(url, code):
    with pytest.raises(UrlRejected) as err:
        normalize_url(url)
    assert err.value.code == code
    record("ssrf-negative", {"case": f"url {url[:40]!r}", "expected": code, "observed": err.value.code, "passed": True})


async def test_default_guard_refuses_loopback_server_and_makes_no_request(server):
    f = SafeFetcher(NetGuard(), user_agent=UA)  # production guard: loopback is not public
    try:
        with pytest.raises(UrlRejected) as err:
            await f.fetch(normalize_url(f"http://127.0.0.1:{server.port}/", allowed_schemes=("http",), allowed_ports=(server.port,)),
                          allowed_schemes=("http",), allowed_ports=(server.port,))
    finally:
        await f.aclose()
    assert err.value.code == "address_not_public" and server.hits == []


async def test_dns_is_resolved_once_per_connection_and_the_validated_address_is_used(server):
    """Rebinding: the guard validated 127.0.0.1; a second DNS answer would be 10.0.0.1. Only one lookup may happen."""
    answers = iter([["127.0.0.1"], ["10.0.0.1"], ["10.0.0.1"]])
    lookups: list[str] = []

    async def resolver(host, port):
        lookups.append(host)
        return next(answers)

    guard = NetGuard(resolver=resolver, allow_networks=(ipaddress.ip_network("127.0.0.0/8"),))
    f = SafeFetcher(guard, user_agent=UA)
    server.page("/r", "ok body text")
    try:
        res = await fetch(f, server, "/r")
    finally:
        await f.aclose()
    assert res.body == b"ok body text" and lookups == ["docs.test"]
    record("ssrf-negative", {"case": "dns_rebinding_single_lookup", "expected": "1 lookup, validated address used",
                             "observed": f"{len(lookups)} lookup", "passed": True})


@pytest.mark.parametrize(("target", "code", "use_policy"), [
    # The guard must stop these even when the source policy would allow the host (no host_check here).
    ("http://internal.test:{port}/x", "address_not_public", False),
    ("http://169.254.169.254:{port}/latest/meta-data/", "address_not_public", False),
    ("http://[::1]:{port}/", "address_not_public", False),
    ("file:///etc/passwd", "scheme_not_allowed", False),
    # And the source policy must stop a public-looking host outside its allowlist.
    ("http://evil.test:{port}/x", "host_not_allowed", True),
])
async def test_redirect_targets_are_revalidated(server, target, code, use_policy):
    async def redirect(request):
        return web.Response(status=302, headers={"Location": target.format(port=server.port)})

    server.routes["/go"] = redirect
    guard = NetGuard(resolver=fake_resolver({"docs.test": ["127.0.0.1"], "internal.test": ["10.0.0.9"], "evil.test": ["127.0.0.1"]}),
                     allow_networks=(ipaddress.ip_network("127.0.0.0/8"),))
    f = SafeFetcher(guard, user_agent=UA)
    spec = spec_for(server)
    try:
        with pytest.raises(UrlRejected) as err:
            await f.fetch(parsed(server, "/go"), allowed_schemes=("http",), allowed_ports=(server.port,),
                          host_check=spec.check_url if use_policy else None)
    finally:
        await f.aclose()
    assert err.value.code == code
    assert [p for _, p, _ in server.hits] == ["/go"]  # the redirect target was never contacted
    record("ssrf-negative", {"case": f"redirect to {target.split('{')[0][:38]}", "expected": code, "observed": err.value.code, "passed": True})


async def test_redirect_loop_and_limit(server):
    async def loop(request):
        return web.Response(status=302, headers={"Location": "/loop2"})

    async def loop2(request):
        return web.Response(status=302, headers={"Location": "/loop"})

    server.routes["/loop"], server.routes["/loop2"] = loop, loop2
    f = fetcher("docs.test", limits=FetchLimits(max_redirects=3))
    try:
        with pytest.raises(FetchError) as err:
            await fetch(f, server, "/loop")
    finally:
        await f.aclose()
    assert err.value.code == "too_many_redirects" and len(server.hits) == 4


async def test_allowed_redirect_is_followed_and_reported(server):
    async def go(request):
        return web.Response(status=301, headers={"Location": "/final?utm_source=x"})

    server.routes["/old"] = go
    server.page("/final", "final body")
    f = fetcher("docs.test")
    try:
        res = await fetch(f, server, "/old")
    finally:
        await f.aclose()
    assert res.body == b"final body" and res.redirects == (parsed(server, "/old").url,)
    assert res.final_url.endswith("/final?utm_source=x")


# ---- robots + pacing + cooldown --------------------------------------------------------------------------------

def polite(server, spec_hosts=("docs.test",), clock: Clock | None = None, state: MemoryHostState | None = None):
    clock = clock or Clock()
    state = state or MemoryHostState()
    f = SafeFetcher(loopback_guard(*spec_hosts), user_agent=UA)
    return PoliteFetcher(f, state, product_token="TtakkariCollector", clock=clock, sleep=clock.sleep), clock, state, f


async def test_robots_disallow_blocks_without_fetching_the_page(server):
    server.page("/robots.txt", "User-agent: *\nAllow: /\n\nUser-agent: TtakkariCollector\nDisallow: /private\nCrawl-delay: 3\n", "text/plain")
    server.page("/private/doc", "secret body " * 50)
    server.page("/public/doc", "public body " * 50)
    server.page("/public/doc2", "public body two " * 50)
    pf, clock, state, f = polite(server)
    spec = spec_for(server)
    try:
        with pytest.raises(PolicyBlocked) as blocked:
            await pf.fetch(parsed(server, "/private/doc"), spec)
        ok = await pf.fetch(parsed(server, "/public/doc"), spec)
        await pf.fetch(parsed(server, "/public/doc2"), spec)
    finally:
        await f.aclose()
    assert blocked.value.code == "robots_disallowed" and server.count("/private/doc") == 0
    assert ok.robots == "allowed" and server.count("/robots.txt") == 1  # cached for the second URL
    assert server.hits[0][2]["User-Agent"] == UA
    # Crawl-delay 3 outranks the policy's 0s minimum: the second page fetch waits for its slot
    assert any(abs(s - 3.0) < 1e-6 for s in clock.sleeps)
    record("robots", {"case": "disallow + crawl-delay", "expected": "blocked, 1 robots fetch, delay 3s", "observed": "ok", "passed": True})


@pytest.mark.parametrize(("status", "outcome"), [
    (404, "allowed"), (410, "allowed"), (403, "robots_forbidden"), (401, "robots_forbidden"),
    (500, "robots_unreachable"), (503, "robots_unreachable"), (429, "robots_unreachable"),
])
async def test_robots_status_handling(server, status, outcome):
    async def robots_handler(request):
        return web.Response(status=status, text="no")

    server.routes["/robots.txt"] = robots_handler
    server.page("/page", "page body " * 30)
    pf, clock, state, f = polite(server)
    try:
        if outcome == "allowed":
            res = await pf.fetch(parsed(server, "/page"), spec_for(server))
            assert res.robots == "allowed"
        elif outcome == "robots_forbidden":
            with pytest.raises(PolicyBlocked) as err:
                await pf.fetch(parsed(server, "/page"), spec_for(server))
            assert err.value.code == outcome
        else:
            with pytest.raises(FetchError) as err:
                await pf.fetch(parsed(server, "/page"), spec_for(server))
            assert err.value.code == outcome and err.value.retryable
            # the cooldown is durable: the next attempt stops before any network access
            hits = len(server.hits)
            with pytest.raises(FetchError) as again:
                await pf.fetch(parsed(server, "/page"), spec_for(server))
            assert again.value.code == "host_cooldown" and len(server.hits) == hits
    finally:
        await f.aclose()
    assert server.count("/page") == (1 if outcome == "allowed" else 0)
    record("robots", {"case": f"robots.txt {status}", "expected": outcome, "observed": outcome, "passed": True})


async def test_robots_cache_expires_and_empty_robots_means_no_rules(server):
    server.page("/robots.txt", "", "text/plain")  # an empty file is a valid "no restrictions" answer
    server.page("/page", "page body " * 30)
    pf, clock, state, f = polite(server)
    try:
        await pf.fetch(parsed(server, "/page"), spec_for(server))
        await pf.fetch(parsed(server, "/page"), spec_for(server))
        assert server.count("/robots.txt") == 1
        clock.now += timedelta(hours=25)
        await pf.fetch(parsed(server, "/page"), spec_for(server))
    finally:
        await f.aclose()
    assert server.count("/robots.txt") == 2


async def test_429_stores_cooldown_from_retry_after_and_blocks_the_next_attempt_offline(server):
    server.page("/robots.txt", "User-agent: *\nAllow: /\n", "text/plain")

    async def limited(request):
        return web.Response(status=429, headers={"Retry-After": "600"})

    server.routes["/busy"] = limited
    pf, clock, state, f = polite(server)
    try:
        with pytest.raises(FetchError) as first:
            await pf.fetch(parsed(server, "/busy"), spec_for(server))
        hits = len(server.hits)
        with pytest.raises(FetchError) as second:
            await pf.fetch(parsed(server, "/busy"), spec_for(server))
        until = await state.get_cooldown(parsed(server, "/busy").origin)
    finally:
        await f.aclose()
    assert first.value.code == "rate_limited" and first.value.retry_after == 600
    assert second.value.code == "host_cooldown" and len(server.hits) == hits
    assert until == clock.now + timedelta(seconds=600)
    clock.now += timedelta(seconds=601)
    server.page("/busy", "now available " * 20)
    pf2, _, _, f2 = polite(server, clock=clock, state=state)
    try:
        assert (await pf2.fetch(parsed(server, "/busy"), spec_for(server))).result.body.startswith(b"now available")
    finally:
        await f2.aclose()
    record("cooldown", {"case": "429 Retry-After 600", "expected": "durable cooldown 600s, no network while cooling", "observed": "ok", "passed": True})


async def test_min_delay_paces_consecutive_requests(server):
    server.page("/robots.txt", "User-agent: *\nAllow: /\n", "text/plain")
    server.page("/a", "aaa " * 50)
    server.page("/b", "bbb " * 50)
    pf, clock, state, f = polite(server)
    spec = spec_for(server, min_delay_seconds=2.0)
    try:
        await pf.fetch(parsed(server, "/a"), spec)
        before = len(clock.sleeps)
        await pf.fetch(parsed(server, "/b"), spec)
    finally:
        await f.aclose()
    assert clock.sleeps[before:] and abs(clock.sleeps[before] - 2.0) < 1e-6


async def test_policy_host_scheme_port_and_path_limits_apply_before_the_network(server):
    pf, clock, state, f = polite(server, spec_hosts=("docs.test", "other.test"))
    spec = spec_for(server, excluded_path_prefixes=["/admin"])
    try:
        for path, host, code in [("/x", "other.test", "host_not_allowed"), ("/admin/page", "docs.test", "path_excluded_by_policy")]:
            with pytest.raises(PolicyBlocked) as err:
                await pf.fetch(parsed(server, path, host), spec)
            assert err.value.code == code
    finally:
        await f.aclose()
    assert server.hits == []


# ---- robots parser corner cases (RFC 9309) ----------------------------------------------------------------------

def test_robots_longest_match_allow_wins_ties_and_wildcards():
    groups = robots.parse(
        "User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /*.pdf$\nDisallow: /tie\nAllow: /tie\n"
        "User-agent: TtakkariCollector\nDisallow: /only-us\n"
    )
    star = robots.rules_for(groups, "SomeoneElse/2.0")
    ours = robots.rules_for(groups, "TtakkariCollector/1.0")
    assert not star.allowed("/a") and star.allowed("/a/b")
    assert not star.allowed("/doc.pdf") and star.allowed("/doc.pdf?x=1") and star.allowed("/doc.pdfx")
    assert star.allowed("/tie")  # equal length: Allow wins
    assert not ours.allowed("/only-us") and ours.allowed("/a")  # a specific group replaces `*`, it is not merged
    assert star.allowed("/robots.txt")


def test_robots_unavailable_denies_everything():
    assert not robots.UNAVAILABLE.allowed("/anything") and not robots.UNAVAILABLE.allowed("/robots.txt")
    assert robots.classify_status(404) == "no_rules" and robots.classify_status(403) == "forbidden"
    assert robots.classify_status(429) == "unreachable" and robots.classify_status(503) == "unreachable"


def test_robots_percent_encoding_and_size_cap():
    groups = robots.parse("User-agent: *\nDisallow: /%7Euser\nDisallow: /caf%C3%A9\n")
    rules = robots.rules_for(groups, "x")
    assert not rules.allowed("/~user/page") and not rules.allowed("/café") and rules.allowed("/cafe")
    huge = "User-agent: *\n" + "Allow: /ok\n" * 100_000 + "Disallow: /late\n"
    assert robots.rules_for(robots.parse(huge), "x").allowed("/late")  # beyond the 500 KiB cap: not parsed
