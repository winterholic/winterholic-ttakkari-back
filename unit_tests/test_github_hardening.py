"""독립 검증(Run e614387a)이 찾은 결함의 회귀 테스트. 항목 번호(V01 등)는 검증 보고서의 것이다.

검증자의 반례 테스트는 증거일 뿐 정본이 아니다. 여기서는 구현 선택(근본 원인)에 맞춰 다시 썼다.
V01 ref 해석 상한 / V02·V03 권한 철회·stale 나이 / V04 라우트 전체 단일 예산 / V05 깊은 JSON / V06 /me/orgs 스키마 /
V09f gh 일시 실패 / V11 로그 마스킹 / V14 HEAD·약식 SHA.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from github_fakes import FakeGh, make_client, ok

from ttakkari.api import github as api
from ttakkari.github import accounts as accounts_mod
from ttakkari.github import client as gc
from ttakkari.github import logs as logs_mod
from ttakkari.github import service as svc_mod
from ttakkari.github.accounts import AccountRegistry
from ttakkari.github.cache import TTLCache
from ttakkari.github.client import GithubClient
from ttakkari.github.errors import GithubError
from ttakkari.github.logs import LogFetcher
from ttakkari.security.auth import require_user

pytestmark = [pytest.mark.security]


FIX = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "github" / "upstream"


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def _nosleep(_: float) -> None:
    return None


REPO = {"id": 1, "name": "widget", "full_name": "acme/widget", "private": True,
        "owner": {"login": "acme", "type": "Organization"}, "default_branch": "main",
        "html_url": "https://github.com/acme/widget", "visibility": "private"}
A = {"account": "alice"}


def build(handler, gh: FakeGh | None = None, *, budget: float = 5.0, log_budget: float | None = None, dl_handler=None):
    gh = gh or FakeGh([("alice", "success"), ("bob", "success")])
    clock = Clock()
    reg = AccountRegistry(runner=gh, gh_bin="gh", clock=clock)
    client = GithubClient(reg, transport=httpx.MockTransport(handler), clock=clock, budget=budget, sleep=_nosleep,
                          cache=TTLCache(clock=clock), rng=lambda: 0.0)
    logs = LogFetcher(client, transport=httpx.MockTransport(dl_handler or handler), clock=clock,
                      **({"budget": log_budget} if log_budget else {}))
    service = svc_mod.GithubService(registry=reg, client=client, logs=logs)
    app = FastAPI()
    app.include_router(api.router)
    api.install(app)

    @app.exception_handler(RequestValidationError)
    async def _validation(request, exc):
        return JSONResponse({"code": "validation", "message": "x"}, status_code=422)

    app.dependency_overrides[require_user] = lambda: "owner"
    app.dependency_overrides[svc_mod.get_service] = lambda: service
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")
    return http, service, gh, clock


async def err(coro) -> GithubError:
    with pytest.raises(GithubError) as e:
        await coro
    return e.value


# ---------------------------------------------------------------- V01 ref 해석: 100개 상한

def _refs_handler(heads: list[str], *, tags: list[str] = (), per_page: int = 100, calls: list | None = None,
                  exact_ok: bool = True):
    """matching-refs 는 per_page 단위로 Link 를 내고, git/ref 는 정확 조회(없으면 404)를 한다."""

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if calls is not None:
            calls.append(p)
        for kind, names in (("heads", heads), ("tags", list(tags))):
            pre = f"/repos/acme/widget/git/matching-refs/{kind}/"
            if p.startswith(pre):
                prefix = p[len(pre):]
                hit = sorted(n for n in names if n.startswith(prefix))
                page = int(req.url.params.get("page", "1"))
                chunk = hit[(page - 1) * per_page: page * per_page]
                more = len(hit) > page * per_page
                hdr = {"link": f'<https://api.github.com/x?page={page + 1}&per_page={per_page}>; rel="next"'} if more else {}
                return ok([{"ref": f"refs/{kind}/{n}"} for n in chunk], hdr)
            ex = f"/repos/acme/widget/git/ref/{kind}/"
            if p.startswith(ex):
                name = p[len(ex):]
                if exact_ok and name in names:
                    return ok({"ref": f"refs/{kind}/{name}", "object": {"sha": "a" * 40, "type": "commit"}})
                return httpx.Response(404, json={"message": "Not Found"})
        if p == "/repos/acme/widget":
            return ok(REPO)
        return httpx.Response(404, json={"message": "Not Found"})

    return handler


async def test_V01_branch_on_second_page_is_found_by_following_link_without_exact_lookup():
    names = [f"dependabot/npm_and_yarn/pkg{i:03d}" for i in range(150)]
    calls: list[str] = []
    http, *_ = build(_refs_handler(names, calls=calls))
    r = await http.get("/api/github/resolve", params={
        "q": f"https://github.com/acme/widget/blob/{names[-1]}/src/a.py", "account": "alice"})
    assert r.status_code == 200, r.text
    assert (r.json()["ref"], r.json()["path"]) == (names[-1], "src/a.py")
    assert not any("/git/ref/" in c for c in calls)


async def test_V01_branch_beyond_the_page_cap_is_resolved_by_exact_lookup():
    names = [f"dependabot/npm_and_yarn/pkg{i:03d}" for i in range(450)]
    http, *_ = build(_refs_handler(names))
    r = await http.get("/api/github/resolve", params={
        "q": f"https://github.com/acme/widget/blob/{names[-1]}/src/a.py", "account": "alice"})
    assert r.status_code == 200, r.text
    assert (r.json()["ref"], r.json()["path"]) == (names[-1], "src/a.py")


async def test_V01_complete_listing_needs_no_exact_probe():
    calls: list[str] = []
    http, *_ = build(_refs_handler(["feature/login-ui"], calls=calls))
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/feature/login-ui/src", "account": "alice"})
    assert r.status_code == 200 and r.json()["ref"] == "feature/login-ui"
    assert not any("/git/ref/" in c for c in calls)  # 목록이 끝까지 왔으면 정확 조회는 하지 않는다


async def test_V01_truncated_listing_and_nonexistent_branch_is_ref_not_found_only_after_exact_probes():
    names = [f"dependabot/x/pkg{i:03d}" for i in range(450)]
    calls: list[str] = []
    http, *_ = build(_refs_handler(names, calls=calls))
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/dependabot/nope/src", "account": "alice"})
    assert r.status_code == 404 and r.json()["code"] == "ref_not_found"
    assert any("/git/ref/heads/dependabot/nope" in c for c in calls)  # 상한에서 포기한 게 아니라 정확 조회로 확인했다


async def test_V01_full_page_without_readable_link_is_still_treated_as_possibly_truncated():
    names = [f"dependabot/x/pkg{i:03d}" for i in range(100)]  # 정확히 한 페이지가 꽉 참, Link 없음
    calls: list[str] = []
    http, *_ = build(_refs_handler(names, per_page=1000, calls=calls))
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/dependabot/other/src", "account": "alice"})
    assert r.status_code == 404 and r.json()["code"] == "ref_not_found"
    assert any("/git/ref/heads/dependabot/other" in c for c in calls)


async def test_V01_truncated_listing_with_too_deep_path_reports_limit_not_missing():
    names = [f"dependabot/x/pkg{i:03d}" for i in range(450)]
    http, *_ = build(_refs_handler(names))
    deep = "/".join(f"s{i}" for i in range(20))
    r = await http.get("/api/github/resolve", params={"q": f"acme/widget/tree/dependabot/{deep}", "account": "alice"})
    assert r.status_code == 422 and r.json()["code"] == "gh_unprocessable", r.text
    assert r.json()["reason"] == "ref_resolution_limit"


async def test_V01_exact_probe_accepts_array_response_with_exact_name_only():
    names = [f"a/b{i:03d}" for i in range(450)]

    def handler(req):
        p = req.url.path
        if "/git/ref/heads/" in p:  # 일부 응답은 접두사 일치 배열을 준다: 정확히 같은 이름만 인정
            return ok([{"ref": "refs/heads/a/b249x"}])
        return _refs_handler(names)(req)

    http, *_ = build(handler)
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/a/b449/src", "account": "alice"})
    assert r.status_code == 404 and r.json()["code"] == "ref_not_found"


# ---------------------------------------------------------------- V02/V03 stale 폐기와 최대 나이

def _client(handler, now):
    async def no_sleep(_):
        return None

    return make_client(handler, clock=lambda: now[0], sleep=no_sleep)


async def _get(client, login="alice", kind="repo", path="/repos/o/r", **kw):
    return await client.get(login, kind, path, ttl=10, **kw)


@pytest.mark.parametrize("revoked", [
    httpx.Response(404, json={"message": "Not Found"}),
    httpx.Response(403, json={"message": "Resource not accessible"}),
    httpx.Response(410, json={"message": "Gone"}),
    httpx.Response(451, json={"message": "legal"}),
], ids=["404", "403", "410", "451"])
async def test_V02_access_loss_observed_drops_cached_entry_so_later_5xx_cannot_resurrect_it(revoked):
    now = [0.0]
    mode = {"m": "ok"}

    def handler(req):
        return {"ok": ok({"n": 1}), "revoked": revoked, "down": httpx.Response(503)}[mode["m"]]

    client, _, _ = _client(handler, now)
    assert (await _get(client)).json() == {"n": 1}
    now[0] = 11.0
    mode["m"] = "revoked"
    await err(_get(client))
    mode["m"] = "down"
    e = await err(_get(client))
    assert e.code in ("gh_upstream_error", "gh_timeout")  # 이전 비공개 데이터가 stale 로 다시 나오지 않는다
    await client.aclose()


async def test_V02_persistent_401_drops_entry_too():
    now = [0.0]
    mode = {"m": "ok"}

    def handler(req):
        return {"ok": ok({"n": 1}), "401": httpx.Response(401, json={}), "down": httpx.Response(503)}[mode["m"]]

    client, _, _ = _client(handler, now)
    await _get(client)
    now[0] = 11.0
    mode["m"] = "401"
    assert (await err(_get(client))).code == "gh_auth_expired"
    mode["m"] = "down"
    assert (await err(_get(client))).code in ("gh_upstream_error", "gh_timeout")
    await client.aclose()


async def test_V02_rate_limit_and_5xx_still_serve_stale_and_do_not_drop():
    now = [0.0]
    mode = {"m": "ok"}

    def handler(req):
        return {"ok": ok({"n": 1}), "429": httpx.Response(429, headers={"retry-after": "9"}),
                "down": httpx.Response(503)}[mode["m"]]

    client, _, _ = _client(handler, now)
    await _get(client)
    now[0] = 11.0
    mode["m"] = "429"
    assert (await _get(client)).stale is True
    mode["m"] = "down"
    assert (await _get(client)).stale is True
    await client.aclose()


async def test_V02_drop_is_per_account_and_repo_loss_drops_that_repos_other_cached_keys():
    now = [0.0]
    seen = {"alice_revoked": False}

    def handler(req):
        if "alice" in req.headers["authorization"] and seen["alice_revoked"]:
            return httpx.Response(404, json={"message": "Not Found"}) if req.url.path == "/repos/o/r" else httpx.Response(503)
        return ok({"n": req.url.path})

    client, _, _ = _client(handler, now)
    await _get(client, "alice")
    await _get(client, "alice", "branches", "/repos/o/r/branches")
    await _get(client, "bob")
    await _get(client, "bob", "branches", "/repos/o/r/branches")
    now[0] = 11.0
    seen["alice_revoked"] = True
    await err(_get(client, "alice"))  # 저장소 자체가 안 보임
    # 같은 저장소의 다른 캐시(alice 것)도 비공개 데이터이므로 5xx 때 stale 로 나오면 안 된다
    assert (await err(_get(client, "alice", "branches", "/repos/o/r/branches"))).code in ("gh_upstream_error", "gh_timeout")
    # bob 의 캐시는 영향이 없다(상류 장애 시 stale 제공)
    assert client._cache.get(("github.com", "bob", "repo", "/repos/o/r", (), ""))[0] is not None
    await client.aclose()


async def test_V03_stale_max_age_boundary_is_inclusive_and_304_revalidation_resets_it():
    now = [0.0]
    mode = {"m": "ok"}

    def handler(req):
        if mode["m"] == "down":
            return httpx.Response(503)
        if req.headers.get("if-none-match") == '"v1"':
            return httpx.Response(304, headers={"etag": '"v1"'})
        return ok({"n": 1}, {"etag": '"v1"'})

    client, _, _ = _client(handler, now)
    await _get(client)
    mode["m"] = "down"
    now[0] = gc.STALE_MAX_AGE  # 정확히 최대 나이: 아직 제공
    assert (await _get(client)).stale is True
    now[0] = gc.STALE_MAX_AGE + 0.001
    e = await err(_get(client))
    assert e.code in ("gh_upstream_error", "gh_timeout")
    # 304 로 다시 확인되면 그 시점부터 나이를 센다
    mode["m"] = "ok"
    now[0] = 2000.0
    r = await _get(client)
    assert r.cached is True and r.stale is False
    mode["m"] = "down"
    now[0] = 2000.0 + gc.STALE_MAX_AGE - 1
    assert (await _get(client)).stale is True
    now[0] = 2000.0 + gc.STALE_MAX_AGE + 1
    await err(_get(client))
    await client.aclose()


# ---------------------------------------------------------------- V04 라우트 전체 단일 예산

def _slow(delay, routes):
    async def handler(req: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        return routes(req)
    return handler


async def _timed(http, url, **params):
    t0 = time.monotonic()
    r = await http.get(url, params={"account": "alice", **params})
    return r, time.monotonic() - t0


async def test_V04_branches_route_shares_one_budget_across_repo_and_branches_calls():
    def routes(req):
        if req.url.path.endswith("/branches"):
            return ok([{"name": "main", "commit": {"sha": "a" * 40}, "protected": False}])
        return ok(REPO)

    http, *_ = build(_slow(0.4, routes), budget=0.5)
    r, dt = await _timed(http, "/api/github/repos/acme/widget/branches")
    assert r.status_code == 504 and r.json()["code"] == "gh_timeout", (r.status_code, dt)
    assert dt < 0.5 + 0.2, dt  # 두 호출을 합쳐 0.8초가 되지 않는다


async def test_V04_readme_404_then_repo_check_shares_one_budget():
    http, *_ = build(_slow(0.4, lambda req: httpx.Response(404, json={"message": "Not Found"}) if req.url.path.endswith("/readme")
                           else ok(REPO)), budget=0.5)
    r, dt = await _timed(http, "/api/github/repos/acme/widget/readme")
    assert r.status_code == 504 and dt < 0.7, (r.status_code, dt)


async def test_V04_large_file_contents_plus_raw_share_one_budget():
    huge = json.loads((FIX / "contents_file_huge.json").read_text(encoding="utf-8"))
    http, *_ = build(_slow(0.4, lambda req: ok(huge)), budget=0.5)
    r, dt = await _timed(http, "/api/github/repos/acme/widget/contents", path="big.bin")
    assert r.status_code == 504 and dt < 0.7, (r.status_code, r.text[:80], dt)


async def test_V04_resolve_with_heads_tags_repo_calls_shares_one_budget():
    def routes(req):
        return ok([]) if "matching-refs" in req.url.path else ok(REPO)

    http, *_ = build(_slow(0.3, routes), budget=0.5)
    r, dt = await _timed(http, "/api/github/resolve", q="acme/widget/tree/nobranch/x")
    assert r.status_code == 504 and dt < 0.7, (r.status_code, dt)


async def test_V04_budget_is_per_request_not_leaked_between_requests_or_into_direct_calls():
    http, svc, *_ = build(_slow(0.15, lambda req: ok(REPO)), budget=0.5)
    for _ in range(3):  # 각 요청은 새 예산: 0.15초짜리 호출이 누적돼 앞선 요청의 마감에 걸리지 않는다
        r, _dt = await _timed(http, "/api/github/repos/acme/widget")
        assert r.status_code == 200
        assert gc.current_deadline.get() is None
    up = await svc.client.get("alice", "repo", "/repos/acme/widget", ttl=0)  # 라우트 밖 직접 호출은 자기 예산
    assert up.status == 200


async def test_V04_log_route_keeps_one_log_budget_for_registry_302_and_download():
    loc = "https://productionresultssa3.blob.core.windows.net/x/y"

    async def api_handler(req):
        await asyncio.sleep(0.3)
        return httpx.Response(302, headers={"location": loc})

    async def dl_handler(req):
        await asyncio.sleep(0.3)
        return httpx.Response(200, content=b"line\n")

    http, *_ = build(api_handler, budget=5.0, log_budget=0.5, dl_handler=dl_handler)
    r, dt = await _timed(http, "/api/github/repos/acme/widget/actions/jobs/9/logs")
    assert r.status_code == 504 and dt < 0.7, (r.status_code, dt)  # 0.3 + 0.3 = 0.6초가 허용되지 않는다


async def test_V04_registry_wait_counts_toward_route_budget():
    class SlowGh(FakeGh):
        async def __call__(self, args):
            if tuple(args[1:]) == ("auth", "status", "--json", "hosts"):
                await asyncio.sleep(2)
            return await super().__call__(args)

    http, *_ = build(lambda req: ok(REPO), SlowGh(), budget=0.4)
    r, dt = await _timed(http, "/api/github/repos/acme/widget")
    assert r.status_code == 504 and dt < 0.7, (r.status_code, dt)


# ---------------------------------------------------------------- V05 깊은 JSON

DEEP = [b"[" * 200_000 + b"]" * 200_000, b'{"a":' * 100_000 + b"1" + b"}" * 100_000]


@pytest.mark.parametrize("body", DEEP, ids=["array", "object"])
async def test_V05_deeply_nested_json_is_schema_changed(body):
    http, *_ = build(lambda req: ok(body))
    r = await http.get("/api/github/me", params=A)
    assert r.status_code == 502 and r.json()["code"] == "gh_schema_changed"


async def test_V05_deeply_nested_error_body_still_classified_by_status():
    http, *_ = build(lambda req: httpx.Response(404, content=DEEP[0]))
    r = await http.get("/api/github/me", params=A)
    assert r.status_code == 404 and r.json()["code"] == "gh_not_found"


async def test_V05_deep_json_from_gh_status_is_a_status_failure_not_500():
    reg = AccountRegistry(runner=_runner(0, DEEP[0]), gh_bin="gh")
    assert (await err(reg.list_accounts())).code == "no_account"


# ---------------------------------------------------------------- V06 /me/orgs 스키마

@pytest.mark.parametrize("body", [{}, {"message": "x"}, "x", 123, None], ids=["dict", "dict2", "str", "int", "null"])
async def test_V06_me_orgs_wrong_shape_is_schema_changed_not_empty_200(body):
    http, *_ = build(lambda req: ok(body))
    r = await http.get("/api/github/me/orgs", params=A)
    assert r.status_code == 502 and r.json()["code"] == "gh_schema_changed"


async def test_V06_me_orgs_valid_empty_list_is_still_200():
    http, *_ = build(lambda req: ok([]))
    r = await http.get("/api/github/me/orgs", params=A)
    assert r.status_code == 200 and r.json()["data"] == []


# ---------------------------------------------------------------- V09f gh 상태 조회 일시 실패

def _runner(code: int, out: bytes):
    async def run(args):
        return code, out
    return run


class Switchable(FakeGh):
    """auth status 만 실패/성공을 바꿀 수 있는 gh 대역."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.status_mode = "ok"  # ok | fail | garbage | timeout

    async def __call__(self, args):
        if tuple(args[1:]) == ("auth", "status", "--json", "hosts"):
            if self.status_mode == "fail":
                self.calls.append(tuple(args[1:]))
                return 1, b""
            if self.status_mode == "timeout":
                self.calls.append(tuple(args[1:]))
                return 124, b""
            if self.status_mode == "garbage":
                self.calls.append(tuple(args[1:]))
                return 0, b"not json"
        return await super().__call__(args)


def _reg(gh, clock):
    removed: list[str] = []
    reg = AccountRegistry(runner=gh, gh_bin="gh", clock=clock)
    reg.on_removed(removed.append)
    return reg, removed


@pytest.mark.parametrize("mode", ["fail", "timeout", "garbage"])
async def test_V09f_transient_status_failure_keeps_last_known_list_and_does_not_purge(mode):
    clock = Clock()
    gh = Switchable([("alice", "success")])
    reg, removed = _reg(gh, clock)
    assert [a.login for a in await reg.list_accounts()] == ["alice"]
    clock.t += 61  # 목록 TTL 경과 → 다시 읽는다
    gh.status_mode = mode
    assert (await reg.require("alice")).login == "alice"  # 마지막으로 확인된 목록을 유효기간 안에서 사용
    assert removed == []


async def test_V09f_last_known_list_expires_with_clear_error_and_still_no_purge():
    clock = Clock()
    gh = Switchable([("alice", "success")])
    reg, removed = _reg(gh, clock)
    await reg.list_accounts()
    gh.status_mode = "fail"
    clock.t += accounts_mod.STATUS_GRACE + 1
    e = await err(reg.require("alice"))
    assert e.code == "no_account" and e.reason == "gh_status_failed"
    assert "확인하지 못했습니다" in e.message and "로그인된 GitHub 계정이 없습니다" not in e.message
    assert removed == []  # 삭제가 확인된 게 아니므로 캐시·토큰을 비우지 않는다
    gh.status_mode = "ok"
    clock.t += 6
    assert (await reg.require("alice")).login == "alice"


async def test_V09f_first_ever_status_failure_is_clear_error_not_empty_account_list():
    gh = Switchable([("alice", "success")])
    gh.status_mode = "fail"
    reg, _ = _reg(gh, Clock())
    e = await err(reg.require("alice"))
    assert e.code == "no_account" and e.reason == "gh_status_failed"


async def test_V09f_valid_empty_list_still_purges_everything():
    clock = Clock()
    gh = Switchable([("alice", "success")])
    reg, removed = _reg(gh, clock)
    await reg.token("alice")
    gh.accounts = []
    clock.t += 61
    e = await err(reg.require("alice"))
    assert e.code == "no_account" and e.reason is None
    assert removed == ["alice"] and "alice" not in reg._tokens


async def test_V09f_transient_failure_route_keeps_stale_protection():
    """V09f 의 실제 영향: 일시 실패가 캐시를 비워 stale 보호를 잃던 흐름."""
    gh = Switchable([("alice", "success")])
    state = {"down": False}
    http, _, _, clock = build(lambda req: httpx.Response(503) if state["down"] else ok(REPO), gh)
    assert (await http.get("/api/github/repos/acme/widget", params=A)).status_code == 200
    clock.t += 120
    state["down"] = True
    gh.status_mode = "fail"
    r = await http.get("/api/github/repos/acme/widget", params=A)
    assert r.status_code == 200 and r.json()["stale"] is True


# ---------------------------------------------------------------- V11 로그 비밀 마스킹

@pytest.mark.parametrize("line,needle", [
    ("https://x.blob.core.windows.net/a?sv=2020&sig=Abcdef%2Bghij0123456789klmnop%3D&se=2026", "Abcdef%2Bghij"),
    ("curl -H 'x' 'https://h/p?se=1&sig=ZZZZ1111aaaa2222bbbb'", "ZZZZ1111aaaa2222bbbb"),
    ("STRIPE_KEY=sk_live_" + "a" * 24, "sk_live_aaaa"),
    ("key rk_live_" + "B" * 24 + " end", "rk_live_BBBB"),
    ("key sk_test_" + "c" * 24, "sk_test_cccc"),
])
def test_V11_sas_signature_and_stripe_keys_are_masked(line, needle):
    out = logs_mod.mask_lines(["before", line, "after"])
    assert out[0] == "before" and out[2] == "after" and needle not in out[1], out[1]


def test_V11_ordinary_text_with_sig_word_is_not_over_masked():
    out = logs_mod.mask_lines(["signature verified", "sigma=3", "design=ok"])
    assert out == ["signature verified", "sigma=3", "design=ok"]


def test_V11_pgp_private_key_block_is_masked_including_across_window_boundary():
    body = ["x", "-----BEGIN PGP PRIVATE KEY BLOCK-----", "lQOYBF...", "=abcd", "-----END PGP PRIVATE KEY BLOCK-----", "y"]
    out = logs_mod.mask_lines(body)
    assert "lQOYBF" not in "\n".join(out) and out[0] == "x" and out[-1] == "y" and len(out) == len(body)
    # 창 경계: 블록이 1999~2002 줄에 걸친다. 로그 전체를 먼저 가리고 창으로 나눈다(LogFetcher.lines 와 같은 순서).
    big = [f"l{i}" for i in range(1997)] + ["-----BEGIN PGP PRIVATE KEY BLOCK-----"] + [f"SECRETBODY{i}" for i in range(6)] + \
          ["-----END PGP PRIVATE KEY BLOCK-----"] + ["tail"]
    whole = logs_mod.mask_private_keys(big)
    w1 = logs_mod.view_log_window(whole, 1, 1, 2000)
    w2 = logs_mod.view_log_window(whole, 1, 2001, 2000)
    assert "SECRETBODY" not in "\n".join(w1.lines + w2.lines)


def test_V11_secret_split_exactly_at_window_edge_is_masked_per_line():
    sas = "https://x.blob.core.windows.net/a?sv=1&sig=EDGEsecretSIG0123456789"
    lines = [f"l{i}" for i in range(1999)] + [sas, sas]
    w1 = logs_mod.view_log_window(lines, 1, 1, 2000)
    w2 = logs_mod.view_log_window(lines, 1, 2001, 2000)
    assert "EDGEsecret" not in "\n".join(w1.lines + w2.lines)


# ---------------------------------------------------------------- V14 HEAD·약식 SHA

SHA40 = "0123456789abcdef0123456789abcdef01234567"


def _sha_handler(calls=None, *, known=SHA40, branch_names=()):
    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if calls is not None:
            calls.append((p, req.headers.get("accept")))
        if "/git/matching-refs/heads/" in p:
            pre = p.split("/git/matching-refs/heads/")[1]
            return ok([{"ref": f"refs/heads/{n}"} for n in branch_names if n.startswith(pre)])
        if "/git/matching-refs/" in p:
            return ok([])
        if p == "/repos/acme/widget":
            return ok(REPO)
        if p.startswith("/repos/acme/widget/commits/"):
            short = p.rsplit("/", 1)[1].lower()
            if known.startswith(short):
                assert req.headers["accept"] == "application/vnd.github.sha"
                return httpx.Response(200, content=known.encode(), headers={"content-type": "application/vnd.github.sha"})
            return httpx.Response(422, json={"message": "No commit found for SHA"})
        return httpx.Response(404, json={"message": "Not Found"})
    return handler


async def test_V14_tree_HEAD_resolves_to_default_branch():
    http, *_ = build(_sha_handler())
    r = await http.get("/api/github/resolve", params={"q": "https://github.com/acme/widget/tree/HEAD/src/a", "account": "alice"})
    j = r.json()
    assert r.status_code == 200 and (j["ref"], j["path"], j["needs_ref_resolution"]) == ("main", "src/a", False), r.text


async def test_V14_HEAD_for_invisible_repo_is_not_found_error_not_ref_error():
    http, *_ = build(lambda req: httpx.Response(404, json={"message": "Not Found"}))
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/HEAD", "account": "alice"})
    assert r.status_code == 404 and r.json()["code"] == "gh_not_found"


@pytest.mark.parametrize("short", [SHA40[:7], SHA40[:12], SHA40[:39], SHA40[:7].upper()])
async def test_V14_short_sha_is_verified_and_expanded_to_the_full_sha(short):
    calls: list = []
    http, *_ = build(_sha_handler(calls))
    r = await http.get("/api/github/resolve", params={"q": f"acme/widget/blob/{short}/src/a.py", "account": "alice"})
    j = r.json()
    assert r.status_code == 200 and (j["ref"], j["path"], j["is_file"]) == (SHA40, "src/a.py", True), r.text
    assert any(p.endswith(f"/commits/{short}") for p, _ in calls)


@pytest.mark.parametrize("bogus", ["deadbee", "1234567", "abc123", "zzzzzzz"])
async def test_V14_arbitrary_or_unknown_sha_is_not_accepted(bogus):
    http, *_ = build(_sha_handler())
    r = await http.get("/api/github/resolve", params={"q": f"acme/widget/tree/{bogus}/src", "account": "alice"})
    assert r.status_code == 404 and r.json()["code"] == "ref_not_found", r.text


async def test_V14_real_branch_named_like_a_sha_wins_over_sha_interpretation():
    calls: list = []
    http, *_ = build(_sha_handler(calls, branch_names=["abc1234"]))
    r = await http.get("/api/github/resolve", params={"q": "acme/widget/tree/abc1234/src", "account": "alice"})
    assert r.status_code == 200 and r.json()["ref"] == "abc1234"
    assert not any("/commits/" in p for p, _ in calls)


async def test_V14_upstream_sha_that_does_not_extend_the_input_is_schema_changed():
    def handler(req):
        if "/commits/" in req.url.path:
            return httpx.Response(200, content=("f" * 40).encode())
        return _sha_handler()(req)

    http, *_ = build(handler)
    r = await http.get("/api/github/resolve", params={"q": f"acme/widget/tree/{SHA40[:7]}/src", "account": "alice"})
    assert r.status_code == 502 and r.json()["code"] == "gh_schema_changed"


async def test_V14_full_sha_is_still_accepted_without_any_call():
    calls: list = []
    http, *_ = build(_sha_handler(calls))
    r = await http.get("/api/github/resolve", params={"q": f"acme/widget/tree/{SHA40}/src", "account": "alice"})
    assert r.status_code == 200 and r.json()["ref"] == SHA40 and calls == []
