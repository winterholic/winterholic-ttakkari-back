"""상류 호출 계층: 계정별 토큰·캐시/ETag 격리·오류 분류·전체 시간 예산·응답 상한·동시성. ADR 0042 §5·§6."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest
from github_fakes import FakeGh, fake_token, make_client, ok

from ttakkari.github import client as gc
from ttakkari.github.errors import GithubError

PULL = "/repos/o/r/pulls/7"


async def get(client, login="alice", kind="pull", path=PULL, **kw):
    return await client.get(login, kind, path, **kw)


async def err(coro) -> GithubError:
    with pytest.raises(GithubError) as e:
        await coro
    return e.value


# ---- 요청 형태 ----

async def test_request_shape_fixed_host_headers_and_bearer_for_the_selected_account():
    seen: list[httpx.Request] = []

    def handler(req):
        seen.append(req)
        return ok({"number": 7})

    client, _, _ = make_client(handler)
    r = await get(client, params={"per_page": 30})
    assert r.json() == {"number": 7}
    req = seen[0]
    assert req.url.host == "api.github.com" and req.url.scheme == "https" and req.url.port is None
    assert req.method == "GET" and req.url.params["per_page"] == "30"
    assert req.headers["authorization"] == f"Bearer {fake_token('alice')}"
    assert req.headers["x-github-api-version"] == "2022-11-28" and req.headers["user-agent"] == "ttakkari-github-explorer"
    assert req.headers["accept"] == "application/vnd.github+json"
    await client.aclose()


async def test_concurrent_requests_for_two_accounts_never_cross_tokens():
    seen: dict[str, str] = {}

    async def handler(req):
        await asyncio.sleep(0.01)
        seen[req.url.path] = req.headers["authorization"]
        return ok({"number": 1})

    client, _, _ = make_client(handler)
    jobs = []
    for i in range(12):
        login = "alice" if i % 2 == 0 else "bob"
        jobs.append(client.get(login, "pull", f"/repos/{login}/r/pulls/{i + 1}"))
    await asyncio.gather(*jobs)
    for path, auth in seen.items():
        owner = path.split("/")[2]
        assert auth == f"Bearer {fake_token(owner)}", path
    await client.aclose()


async def test_only_template_paths_are_allowed():
    client, _, _ = make_client(lambda r: ok({}))
    for kind, path in [("pull", "/repos/o/r/pulls/7/../../x"), ("pull", "/repos/o/r/pulls/abc"), ("pull", "//evil.com/x"),
                       ("repo", "/repos/o/r/../../../admin"), ("repo", "/repos/o/%2e%2e"), ("repo", "https://evil.com/repos/o/r"),
                       ("pull", "/repos/o/r/issues/7"), ("contents", "/repos/o/r/contents/a/../b")]:
        assert (await err(client.get("alice", kind, path))).code == "invalid_target", (kind, path)
    with pytest.raises(KeyError):
        await client.get("alice", "no-such-kind", "/x")
    assert not hasattr(client, "post") and not hasattr(client, "request")
    await client.aclose()


# ---- 캐시·ETag ----

async def test_fresh_cache_hit_skips_upstream_and_is_isolated_per_account():
    calls: list[str] = []

    def handler(req):
        calls.append(req.headers["authorization"])
        return ok({"n": len(calls)}, {"etag": '"v1"'})

    client, _, _ = make_client(handler)
    a1 = await get(client, "alice", ttl=60)
    a2 = await get(client, "alice", ttl=60)
    assert a1.cached is False and a2.cached is True and a2.json() == {"n": 1} and len(calls) == 1
    b1 = await get(client, "bob", ttl=60)  # 같은 경로라도 다른 계정은 공유하지 않는다
    assert b1.cached is False and b1.json() == {"n": 2} and len(calls) == 2
    assert calls[1] == f"Bearer {fake_token('bob')}"
    await client.aclose()


async def test_expired_entry_revalidates_with_etag_and_304_keeps_cache():
    now = [0.0]
    sent: list[str | None] = []

    def handler(req):
        sent.append(req.headers.get("if-none-match"))
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"x-ratelimit-remaining": "4989"})
        return ok({"n": 1}, {"etag": '"v1"'})

    client, _, _ = make_client(handler, clock=lambda: now[0])
    await get(client, ttl=30)
    now[0] = 31.0
    r = await get(client, ttl=30)
    assert sent == [None, '"v1"'] and r.cached is True and r.json() == {"n": 1} and r.stale is False
    now[0] = 40.0
    await get(client, ttl=30)  # 304 로 TTL 이 갱신되어 다시 신선
    assert len(sent) == 2
    await client.aclose()


async def test_changed_etag_replaces_entry():
    state = {"v": 1}

    def handler(req):
        return ok({"v": state["v"]}, {"etag": f'"v{state["v"]}"'})

    now = [0.0]
    client, _, _ = make_client(handler, clock=lambda: now[0])
    assert (await get(client, ttl=10)).json() == {"v": 1}
    state["v"] = 2
    now[0] = 11.0
    r = await get(client, ttl=10)
    assert r.json() == {"v": 2} and r.cached is False
    await client.aclose()


async def test_account_removed_from_gh_purges_its_cache_and_blocks_requests():
    gh = FakeGh()
    client, reg, _ = make_client(lambda r: ok({"x": 1}), gh)
    await get(client, "bob", ttl=600)
    gh.accounts = [("alice", "success")]
    await reg.list_accounts(force=True)
    assert (await err(get(client, "bob", ttl=600))).code == "account_unavailable"
    await client.aclose()


# ---- 인증 갱신 ----

async def test_upstream_401_refetches_token_once_and_never_returns_401():
    gh = FakeGh()
    used: list[str] = []

    def handler(req):
        tok = req.headers["authorization"]
        used.append(tok)
        return ok({"ok": 1}) if tok.endswith("g1_0123456789abcdef") else httpx.Response(401, json={"message": "Bad credentials"})

    client, reg, _ = make_client(handler, gh)
    await reg.token("alice")
    gh.gen["alice"] = 1  # gh auth refresh 후 새 토큰
    r = await get(client, "alice")
    assert r.json() == {"ok": 1} and used == [f"Bearer {fake_token('alice', 0)}", f"Bearer {fake_token('alice', 1)}"]
    await client.aclose()


async def test_persistent_401_is_gh_auth_expired_502():
    client, _, _ = make_client(lambda r: httpx.Response(401, json={"message": "Bad credentials"}))
    e = await err(get(client))
    assert e.code == "gh_auth_expired" and e.status == 502
    await client.aclose()


# ---- 오류 분류 ----

@pytest.mark.parametrize("status,headers,body,code,extra", [
    (404, {}, {"message": "Not Found"}, "gh_not_found", {}),
    (410, {}, {"message": "Issues are disabled"}, "gh_gone", {}),
    (451, {}, {"message": "DMCA"}, "gh_unavailable_legal", {}),
    (422, {}, {"message": "diff too large"}, "gh_unprocessable", {}),
    (409, {}, {"message": "Git Repository is empty."}, "gh_empty_repository", {}),
    (409, {}, {"message": "other conflict"}, "gh_unprocessable", {}),
    (400, {}, {"message": "bad"}, "gh_unprocessable", {}),
])
async def test_status_mapping(status, headers, body, code, extra):
    client, _, _ = make_client(lambda r: httpx.Response(status, json=body, headers=headers))
    assert (await err(get(client))).code == code
    await client.aclose()


async def test_rate_limit_variants():
    now = 1_000_000.0
    # 1) 소진: remaining=0 + reset
    c1, _, _ = make_client(lambda r: httpx.Response(403, json={"message": "API rate limit exceeded"},
                           headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now) + 90)}), wall=lambda: now)
    e = await err(get(c1))
    assert (e.code, e.status, e.reset, e.retry_after) == ("gh_rate_limited", 429, int(now) + 90, 90)
    assert e.headers() == {"Retry-After": "90"}
    # 2) 보조 한도: retry-after
    c2, _, _ = make_client(lambda r: httpx.Response(403, json={"message": "You have exceeded a secondary rate limit"},
                           headers={"retry-after": "30"}), wall=lambda: now)
    e = await err(get(c2))
    assert (e.code, e.retry_after, e.reset) == ("gh_rate_limited", 30, int(now) + 30)
    # 3) 429 헤더 없음 → 보수적 60초
    c3, _, _ = make_client(lambda r: httpx.Response(429, json={}), wall=lambda: now)
    assert (await err(get(c3))).retry_after == 60
    # 4) 헤더 없는 403 이라도 본문이 보조 한도면 한도 오류
    c4, _, _ = make_client(lambda r: httpx.Response(403, json={"message": "You have exceeded a secondary rate limit. Please wait"}), wall=lambda: now)
    assert (await err(get(c4))).code == "gh_rate_limited"
    for c in (c1, c2, c3, c4):
        await c.aclose()


async def test_rate_limited_never_retries():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "5"}, json={})

    client, _, _ = make_client(handler)
    await err(get(client))
    assert len(calls) == 1
    await client.aclose()


async def test_forbidden_reasons_sso_scope_policy():
    sso = "required; url=https://github.com/orgs/acme/sso?authorization_request=abc"
    c, _, _ = make_client(lambda r: httpx.Response(403, json={"message": "SAML"}, headers={"x-github-sso": sso, "x-ratelimit-remaining": "10"}))
    e = await err(get(c))
    assert (e.code, e.reason) == ("gh_forbidden", "sso_required") and e.extra["sso_url"].startswith("https://github.com/orgs/acme/sso")
    evil, _, _ = make_client(lambda r: httpx.Response(403, headers={"x-github-sso": "required; url=https://evil.com/x"}))
    e = await err(get(evil))
    assert e.reason == "sso_required" and "sso_url" not in e.extra  # github.com 밖 URL 은 전달하지 않는다
    scope, _, _ = make_client(lambda r: httpx.Response(403, json={"message": "Resource not accessible"},
                              headers={"x-oauth-scopes": "gist, read:org", "x-accepted-oauth-scopes": "repo"}))
    assert (await err(get(scope))).reason == "scope_missing"
    implied, _, _ = make_client(lambda r: httpx.Response(403, json={"message": "no"},
                                headers={"x-oauth-scopes": "repo, workflow", "x-accepted-oauth-scopes": "public_repo, repo"}))
    assert (await err(get(implied))).reason == "policy"  # repo 가 public_repo 를 포함하므로 scope 부족이 아니다
    for cl in (c, evil, scope, implied):
        await cl.aclose()


async def test_rate_state_from_headers_and_exhausted_gate_skips_upstream():
    now = [1000.0]
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return ok({"n": 1}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1060"})

    client, _, _ = make_client(handler, wall=lambda: now[0])
    r = await get(client, kind="pull", path="/repos/o/r/pulls/1")
    assert r.rate.remaining == 0 and r.rate.reset == 1060 and r.rate.low is True
    e = await err(get(client, kind="pull", path="/repos/o/r/pulls/2"))  # 캐시 없는 새 요청은 reset 전까지 상류에 안 간다
    assert e.code == "gh_rate_limited" and e.reset == 1060 and e.retry_after == 60 and calls == ["/repos/o/r/pulls/1"]
    cached = await get(client, kind="pull", path="/repos/o/r/pulls/1", ttl=30)
    assert cached.cached is True  # 캐시는 계속 제공
    now[0] = 1061.0
    await get(client, kind="pull", path="/repos/o/r/pulls/2")
    assert len(calls) == 2
    # 다른 계정의 한도와 섞이지 않는다
    now[0] = 1000.0
    other = await get(client, "bob", kind="pull", path="/repos/o/r/pulls/3")
    assert other.rate.remaining == 0 and len(calls) == 3
    await client.aclose()


# ---- 재시도·전체 시간 예산 ----

async def test_5xx_retries_with_backoff_then_succeeds():
    n = {"i": 0}
    sleeps: list[float] = []

    def handler(req):
        n["i"] += 1
        return httpx.Response(503) if n["i"] < 3 else ok({"ok": 1})

    async def fake_sleep(s):
        sleeps.append(s)

    client, _, _ = make_client(handler, sleep=fake_sleep, rng=lambda: 0.5)
    r = await get(client)
    assert r.json() == {"ok": 1} and n["i"] == 3 and sleeps == [1.0, 2.0]  # 1s·2s × (0.5+0.5)
    await client.aclose()


async def test_5xx_gives_up_after_three_attempts_as_upstream_error():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(502)

    async def no_sleep(s):
        pass

    client, _, _ = make_client(handler, sleep=no_sleep)
    e = await err(get(client))
    assert e.code == "gh_upstream_error" and e.status == 502 and n["i"] == 3
    await client.aclose()


async def test_4xx_is_never_retried():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(404, json={"message": "Not Found"})

    client, _, _ = make_client(handler)
    await err(get(client))
    assert n["i"] == 1
    await client.aclose()


async def test_total_budget_includes_retries_and_backoff():
    """15초는 재시도·백오프를 모두 포함한 한 호출의 전체 예산이다(여기서는 0.4초로 줄여 검증)."""
    calls = []

    async def slow(req):
        calls.append(time.monotonic())
        await asyncio.sleep(0.25)
        return httpx.Response(503)

    client, _, _ = make_client(slow, budget=0.4, backoff=(0.01, 0.01))
    t0 = time.monotonic()
    e = await err(get(client))
    elapsed = time.monotonic() - t0
    assert e.code == "gh_timeout" and e.status == 504
    assert elapsed < 0.4 + 0.15, elapsed  # 시도 3회 x 0.25초 = 0.75초가 되지 않는다
    await client.aclose()


async def test_hanging_upstream_times_out_within_budget():
    async def hang(req):
        await asyncio.sleep(30)

    client, _, _ = make_client(hang, budget=0.3)
    t0 = time.monotonic()
    e = await err(get(client))
    assert e.code == "gh_timeout" and time.monotonic() - t0 < 0.6
    await client.aclose()


async def test_backoff_longer_than_remaining_budget_stops_retrying():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(503)

    client, _, _ = make_client(handler, budget=0.5, backoff=(5.0, 5.0))
    t0 = time.monotonic()
    e = await err(get(client))
    assert e.code == "gh_upstream_error" and n["i"] == 1 and time.monotonic() - t0 < 0.4  # 5초를 기다리지 않는다
    await client.aclose()


async def test_transport_errors_are_retried_then_reported():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        raise httpx.ConnectError("boom", request=req)

    async def no_sleep(s):
        pass

    client, _, _ = make_client(handler, sleep=no_sleep)
    e = await err(get(client))
    assert e.code == "gh_upstream_error" and n["i"] == 3
    await client.aclose()


# ---- 응답 상한 ----

async def test_response_size_cap_by_content_length_and_by_stream():
    big = b'{"x":"' + b"a" * 5000 + b'"}'
    c1, _, _ = make_client(lambda r: ok(big))
    assert (await err(get(c1, max_bytes=1000))).code == "gh_too_large"

    def chunked(req):
        async def gen():
            for _ in range(10):
                yield b"a" * 500
        return httpx.Response(200, content=gen(), headers={"x-ratelimit-remaining": "9"})

    c2, _, _ = make_client(chunked)
    assert (await err(get(c2, max_bytes=1000))).code == "gh_too_large"  # Content-Length 없이도 스트림에서 끊는다
    await c1.aclose()
    await c2.aclose()


async def test_truncate_mode_returns_prefix_and_is_not_cached():
    calls = []

    def handler(req):
        calls.append(1)
        return ok(b"x" * 5000)

    client, _, _ = make_client(handler)
    r = await get(client, kind="contents", path="/repos/o/r/contents/big.log", accept="application/vnd.github.raw+json",
                  max_bytes=1000, truncate=True, ttl=60)
    assert r.truncated is True and len(r.body) == 1000
    await get(client, kind="contents", path="/repos/o/r/contents/big.log", accept="application/vnd.github.raw+json",
              max_bytes=1000, truncate=True, ttl=60)
    assert len(calls) == 2  # 잘린 응답은 캐시하지 않는다
    await client.aclose()


async def test_invalid_json_is_schema_changed():
    client, _, _ = make_client(lambda r: ok(b"<html>not json</html>"))
    r = await get(client)
    assert (await err(_json(r))).code == "gh_schema_changed"
    await client.aclose()


async def _json(r):
    return r.json()


# ---- stale-if-error ----

async def test_stale_entry_is_served_when_upstream_fails_or_rate_limited():
    now = [0.0]
    mode = {"m": "ok"}

    def handler(req):
        if mode["m"] == "ok":
            return ok({"n": 1})
        if mode["m"] == "5xx":
            return httpx.Response(500)
        return httpx.Response(429, headers={"retry-after": "9"})

    async def no_sleep(s):
        pass

    client, _, _ = make_client(handler, clock=lambda: now[0], sleep=no_sleep)
    await get(client, ttl=10)
    now[0] = 11.0
    mode["m"] = "5xx"
    r = await get(client, ttl=10)
    assert r.stale is True and r.cached is True and r.json() == {"n": 1}
    mode["m"] = "429"
    assert (await get(client, ttl=10)).stale is True
    mode["m"] = "ok"
    assert (await get(client, ttl=10)).stale is False
    # 404 같은 확정 오류는 stale 로 가리지 않는다
    mode["m"] = "ok"
    now[0] = 100.0
    client2, _, _ = make_client(lambda r: httpx.Response(404, json={}), clock=lambda: now[0])
    await err(get(client2))
    await client.aclose()
    await client2.aclose()


# ---- 리다이렉트 ----

async def test_redirect_policy():
    def moved(req):
        if req.url.path == "/repos/o/old":
            return httpx.Response(301, headers={"location": "https://api.github.com/repositories/123"})
        assert req.url.path == "/repositories/123" and req.headers["authorization"].startswith("Bearer ")
        return ok({"id": 123})

    client, _, _ = make_client(moved)
    assert (await client.get("alice", "repo", "/repos/o/old")).json() == {"id": 123}
    evil, _, _ = make_client(lambda r: httpx.Response(302, headers={"location": "https://evil.com/steal"}))
    assert (await err(evil.get("alice", "repo", "/repos/o/r"))).code == "gh_upstream_error"
    other_path, _, _ = make_client(lambda r: httpx.Response(301, headers={"location": "https://api.github.com/user"}))
    assert (await err(other_path.get("alice", "repo", "/repos/o/r"))).code == "gh_upstream_error"
    for c in (client, evil, other_path):
        await c.aclose()


async def test_job_logs_redirect_is_returned_not_followed_and_carries_no_body():
    seen = []

    def handler(req):
        seen.append(req.url.host)
        return httpx.Response(302, headers={"location": "https://pipelines.actions.githubusercontent.com/abc/logs?sig=SECRETSIG"})

    client, _, _ = make_client(handler)
    r = await client.get("alice", "job_logs", "/repos/o/r/actions/jobs/9/logs", statuses=(302,))
    assert r.status == 302 and r.location.startswith("https://pipelines.actions.githubusercontent.com/") and seen == ["api.github.com"]
    await client.aclose()


# ---- 동시성 상한 ----

async def test_per_login_concurrency_limit_is_exactly_four():
    live = {"per": 0, "max": 0}

    async def handler(req):
        live["per"] += 1
        live["max"] = max(live["max"], live["per"])
        await asyncio.sleep(0.02)
        live["per"] -= 1
        return ok({"n": 1})

    client, _, _ = make_client(handler, per_login=4, total=50, budget=10)
    await asyncio.gather(*[client.get("alice", "pull", f"/repos/alice/r/pulls/{i + 1}") for i in range(20)])
    assert live["max"] == 4  # 상한까지 쓰되 넘지 않는다
    await client.aclose()


async def test_global_concurrency_limit_across_accounts():
    live = {"now": 0, "max": 0}

    async def handler(req):
        live["now"] += 1
        live["max"] = max(live["max"], live["now"])
        await asyncio.sleep(0.02)
        live["now"] -= 1
        return ok({"n": 1})

    client, _, _ = make_client(handler, per_login=10, total=3, budget=10)
    await asyncio.gather(*[client.get("alice" if i % 2 else "bob", "pull", f"/repos/o/r/pulls/{i + 1}") for i in range(20)])
    assert live["max"] == 3
    await client.aclose()


# ---- 페이지네이션 ----

@pytest.mark.parametrize("link,want", [
    ('<https://api.github.com/x?per_page=30&page=3>; rel="next", <https://api.github.com/x?page=9>; rel="last"', 3),
    ('<https://api.github.com/x?page=2>; rel="prev"', None),
    ('<https://api.github.com/x?page=abc>; rel="next"', None),
    ('<https://api.github.com/x?page=99999999999999999999>; rel="next"', None),
    ('<https://api.github.com/x?cursor=zzz>; rel="next"', None),
    ("garbage", None), ("", None),
])
def test_next_page_parsing(link, want):
    assert gc.next_page(link) == want


async def test_next_page_reaches_response():
    client, _, _ = make_client(lambda r: ok([1], {"link": '<https://api.github.com/x?page=2>; rel="next"'}))
    r = await client.get("alice", "user_repos", "/user/repos")
    assert r.next_page == 2
    await client.aclose()


# ---- 비밀 ----

async def test_token_never_in_logs_errors_or_repr(caplog):
    caplog.set_level(logging.DEBUG)
    tok = fake_token("alice")
    client, reg, _ = make_client(lambda r: httpx.Response(403, json={"message": f"echo {tok}"}))
    e = await err(get(client))
    assert tok not in str(e) and tok not in repr(e) and tok not in str(e.detail())
    assert tok not in caplog.text
    assert tok not in repr(client) and tok not in repr(reg)
    await client.aclose()
