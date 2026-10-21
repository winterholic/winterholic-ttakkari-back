"""Claude 사용량 측정 실패가 낡은 0% 로 보이던 문제(2026-10-19 x3: 401 이 20시간 이어졌는데 화면은 'api 0%' 오류 없음).

원인 두 가지를 고정한다.
1. active 세대의 coordinator(load_latest(refresh=True))가 실패한 조회 결과를 며칠 전 DB 샘플로 되돌렸다.
2. 사용량 API 오류(401 만료 토큰 등)의 사유가 남지 않았고, 만료 토큰을 갱신하지 않았다.
"""
import time
from uuid import uuid4

import httpx
import pytest

from ttakkari.runs import usage, usage_collector


def _old_sample(hours: float = 20) -> usage.Usage:
    return usage.Usage(windows=[usage.Window("five_hour", "5시간", 0, None), usage.Window("seven_day", "주간", 0, 1792933199)],
                       source="api", updated_at=time.time() - hours * 3600)


def test_reseed_does_not_replace_a_failed_live_fetch_with_an_old_sample():
    aid = uuid4()
    usage.clear_cache()
    old = _old_sample()
    usage.seed(aid, old)  # 재시작 직후 DB 에서 채운 값
    usage._cache[aid] = (time.time(), usage.Usage(error="사용량 API 401"))  # 방금 실패한 조회

    usage.seed(aid, old, refresh=True)  # coordinator 가 poll 마다 부르는 재시드

    got = usage.cached(aid)
    assert got is not None and got.error == "사용량 API 401" and not got.windows


def test_reseed_still_adopts_a_sample_newer_than_the_cache():
    aid = uuid4()
    usage.clear_cache()
    usage._cache[aid] = (time.time() - 600, usage.Usage(error="사용량 API 401"))
    newer = usage.Usage(windows=[usage.Window("five_hour", "5시간", 12, None)], source="api", updated_at=time.time() - 5)

    usage.seed(aid, newer, refresh=True)  # 다른 세대가 더 새로 측정해 DB 에 남긴 값

    assert usage.cached(aid).windows[0].percent == 12


def _mock_api(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(usage.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def _oauth(token: str, expires_in: float) -> dict:
    return {"accessToken": token, "expiresAt": int((time.time() + expires_in) * 1000)}


async def test_expired_token_failure_names_the_reason(monkeypatch):
    async def oauth(_):
        return _oauth("expired", -20 * 3600)

    async def skipped(_):
        return "최근에 갱신을 시도함"

    monkeypatch.setattr(usage, "_claude_oauth", oauth)
    monkeypatch.setattr(usage, "_refresh_claude_login", skipped)
    _mock_api(monkeypatch, lambda r: httpx.Response(401, json={"type": "error", "error": {"message": "OAuth token has expired."}}))

    u = await usage._fetch_claude("/x")

    assert not u.windows and u.source is None
    assert "사용량 API 401: OAuth token has expired." in u.error
    assert "20시간 전 만료" in u.error and "최근에 갱신을 시도함" in u.error


async def test_expired_token_is_refreshed_through_the_cli_then_read(monkeypatch):
    state = {"token": "expired", "refreshed": 0}

    async def oauth(_):
        return _oauth(state["token"], 3600 if state["token"] == "fresh" else -3600)

    async def refresh(_):
        state["refreshed"] += 1
        state["token"] = "fresh"  # CLI 가 키체인의 토큰을 갱신했다

    seen = []

    def api(request):
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"limits": [{"kind": "session", "percent": 14, "resets_at": None},
                                                    {"kind": "weekly_all", "percent": 11, "resets_at": None}]})

    monkeypatch.setattr(usage, "_claude_oauth", oauth)
    monkeypatch.setattr(usage, "_refresh_claude_login", refresh)
    _mock_api(monkeypatch, api)

    u = await usage._fetch_claude("/x")

    assert state["refreshed"] == 1 and seen == ["Bearer fresh"]
    assert u.error is None and [(w.key, w.percent) for w in u.windows] == [("five_hour", 14), ("seven_day", 11)]


async def test_rejected_token_with_remaining_lifetime_is_refreshed_once_and_retried(monkeypatch):
    state = {"token": "revoked", "refreshed": 0}

    async def oauth(_):
        return _oauth(state["token"], 3600)

    async def refresh(_):
        state["refreshed"] += 1
        state["token"] = "fresh"

    def api(request):
        if request.headers["authorization"] == "Bearer fresh":
            return httpx.Response(200, json={"limits": [{"kind": "session", "percent": 3, "resets_at": None}]})
        return httpx.Response(401, json={"error": {"message": "OAuth access token is invalid."}})

    monkeypatch.setattr(usage, "_claude_oauth", oauth)
    monkeypatch.setattr(usage, "_refresh_claude_login", refresh)
    _mock_api(monkeypatch, api)

    u = await usage._fetch_claude("/x")

    assert state["refreshed"] == 1 and u.windows[0].percent == 3


async def test_rate_limited_response_is_reported_verbatim(monkeypatch):
    async def oauth(_):
        return _oauth("ok", 3600)

    monkeypatch.setattr(usage, "_claude_oauth", oauth)
    _mock_api(monkeypatch, lambda r: httpx.Response(429, headers={"retry-after": "120"},
                                                    json={"error": {"message": "Rate limited. Please try again later."}}))

    u = await usage._fetch_claude("/x")

    assert u.error == "사용량 API 429: Rate limited. Please try again later. · 120초 뒤 재시도 가능"


async def test_refresh_attempts_are_spaced_per_account(monkeypatch):
    usage._login_refresh_at.clear()
    usage._login_refresh_at["/x"] = time.time()

    assert await usage._refresh_claude_login("/x") == "최근에 갱신을 시도함"


def _claude_account():
    from ttakkari.models import EngineAccount

    return EngineAccount(id=uuid4(), name="회사 클로드", engine="claude", pool="work", enabled=True, work_enabled=False, max_used_percent=100)


def _patch_profiles(monkeypatch):
    from ttakkari.api import accounts as api

    monkeypatch.setattr(api.profiles, "identity", lambda *_: ("ok", None))
    monkeypatch.setattr(api.profiles, "cooldown_remaining", lambda *_: 0)
    monkeypatch.setattr(api.profiles, "execution_ready", lambda *_: True)
    return api


def test_old_claude_value_is_marked_stale_with_the_reason_not_shown_as_current(monkeypatch):
    api = _patch_profiles(monkeypatch)

    out = api._usage_out(0, _claude_account(), {}, _old_sample())

    assert out.stale and out.remaining_to_limit_percent is None
    assert {w.state for w in out.windows} == {"stale"}
    assert out.error and "새 사용량을 읽지 못했어요" in out.error and "20시간" in out.error


def test_failed_claude_fetch_shows_the_provider_error_text(monkeypatch):
    api = _patch_profiles(monkeypatch)

    out = api._usage_out(0, _claude_account(), {}, usage.Usage(error="사용량 API 401: OAuth token has expired."))

    assert not out.windows and out.error == "사용량 API 401: OAuth token has expired."


def test_fresh_claude_value_is_current(monkeypatch):
    api = _patch_profiles(monkeypatch)
    fresh = usage.Usage(windows=[usage.Window("five_hour", "5시간", 14, None)], source="api", updated_at=time.time())

    out = api._usage_out(0, _claude_account(), {}, fresh)

    assert not out.stale and out.error is None and out.windows[0].state == "current"


def test_claude_window_past_its_reset_is_unavailable_not_a_percent(monkeypatch):
    api = _patch_profiles(monkeypatch)
    fresh = usage.Usage(windows=[usage.Window("five_hour", "5시간", 92, int(time.time()) - 60)], source="api", updated_at=time.time())

    out = api._usage_out(0, _claude_account(), {}, fresh)

    assert out.windows[0].state == "unavailable"


@pytest.mark.parametrize(("engine", "expected"), [("claude", True), ("muse", True), ("codex", False)])
async def test_collector_retries_at_once_when_a_run_leaves_a_newer_observation(monkeypatch, engine, expected):
    """Claude 토큰은 그 계정으로 Run 이 돌 때 갱신된다. 실패 백오프 중이라도 새 스트림 관측이 생기면 바로 다시 읽는다."""
    from ttakkari.runs import profiles

    aid = uuid4()
    usage.clear_cache()
    now = time.time()
    fetched = []

    async def fetch(account_id, eng, cdir):
        fetched.append(account_id)
        return usage.Usage(error="사용량 API 401")

    class Row:
        id = aid
        config_dir = None
        enabled = True
        work_enabled = False
        pool = "personal"

    Row.engine = engine

    async def accounts(_db):
        return [Row]

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def counts():
        return {}

    monkeypatch.setattr(usage_collector, "sessionmaker", lambda: FakeSession)
    monkeypatch.setattr(profiles, "accounts", accounts)
    monkeypatch.setattr(profiles, "running_counts_async", counts)
    monkeypatch.setattr(profiles, "limited_until", lambda *_: None)

    async def noop():
        return None

    from ttakkari.runs import manager
    monkeypatch.setattr(manager, "_reconsider_quota_waits", noop)
    collector = usage_collector.Collector(fetch)
    collector._state[aid] = usage_collector._State(next_due=now + 1800, failures=3, last_fetch=now - 120)
    usage._stream[aid] = usage.Usage(windows=[usage.Window("five_hour", "5시간", 20, None)], source="stream", updated_at=now - 5)

    await collector.tick(now)

    assert bool(fetched) is expected
    usage.clear_cache()


async def test_a_hung_provider_fetch_ends_with_an_error_and_frees_the_inflight_slot(monkeypatch):
    """조회가 영영 끝나지 않으면 이후 모든 조회가 같은 작업을 기다려 화면이 '읽는 중'에서 멈춘다."""
    import asyncio

    aid = uuid4()
    usage.clear_cache()
    monkeypatch.setitem(usage.FETCH_DEADLINE, "claude", 0.05)

    async def hang(_config_dir):
        await asyncio.sleep(60)

    monkeypatch.setattr(usage, "_fetch_claude", hang)

    first = await usage.usage_for(aid, "claude", "/x", refresh=True)

    assert "0초 안에 끝나지 않았어요(claude)" in first.error and not first.windows
    assert not usage.fetching(aid)
    assert usage.cached(aid).error == first.error


async def test_flush_does_not_spin_on_a_finished_task_whose_discard_callback_never_ran():
    """닫힌 루프의 끝난 Task 가 목록에 남으면 예전 flush 는 끝난 작업만 gather 하며 영영 끝나지 않았다."""
    import asyncio

    from ttakkari.runs import usage_store

    async def one():
        return 1

    def finished_task_of_a_closed_loop():
        loop = asyncio.new_event_loop()
        task = loop.create_task(one())
        loop.run_until_complete(task)
        loop.close()
        return task

    stale = await asyncio.to_thread(finished_task_of_a_closed_loop)  # 돌고 있는 루프 안에서는 다른 루프를 못 돌린다
    usage_store._tasks.add(stale)
    try:
        await asyncio.wait_for(usage_store.flush(), 5)
        assert stale not in usage_store._tasks
    finally:
        usage_store._tasks.discard(stale)


# ---- Codex: 사용량 API 실패 사유가 조용히 세션 로그로 덮이던 문제(2026-10-21 내 codex, 마지막 측정 3시간 28분 전)

def _jwt(expires_in: float) -> str:
    import base64
    import json

    def part(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part({'exp': int(time.time() + expires_in)})}.sig"


_CODEX_BODY = {"plan_type": "plus", "rate_limit": {
    "primary_window": {"used_percent": 7, "limit_window_seconds": 18000, "reset_at": 1792609134},
    "secondary_window": {"used_percent": 46, "limit_window_seconds": 604800, "reset_at": 1793090605}}}


def _codex_token(monkeypatch, state: dict):
    """state['token'] 을 auth.json 의 access token 대신 쓴다(실제 파일은 읽지 않는다)."""
    monkeypatch.setattr(usage, "_codex_headers", lambda _d: {"Authorization": f"Bearer {state['token']}"})


async def test_codex_401_failure_names_the_reason_and_relogin(monkeypatch):
    _codex_token(monkeypatch, {"token": _jwt(3600)})

    async def skipped(_):
        return "최근에 갱신을 시도함"

    monkeypatch.setattr(usage, "_refresh_codex_login", skipped)
    _mock_api(monkeypatch, lambda r: httpx.Response(401, json={"error": {"code": "token_expired", "message": "secret-ish"}}))

    u = await usage._fetch_codex("/x")

    assert not u.windows and u.source is None
    assert "사용량 API 401(token_expired)" in u.error and "재로그인" in u.error
    assert "최근에 갱신을 시도함" in u.error and "secret-ish" not in u.error


async def test_codex_expired_token_is_refreshed_through_the_cli_then_read(monkeypatch):
    state = {"token": _jwt(-3600), "refreshed": 0}
    _codex_token(monkeypatch, state)

    async def refresh(_):
        state["refreshed"] += 1
        state["token"] = _jwt(3600)  # codex CLI 가 auth.json 을 갱신했다

    monkeypatch.setattr(usage, "_refresh_codex_login", refresh)
    seen = []

    def handler(r: httpx.Request):
        seen.append(r.headers["Authorization"])
        return httpx.Response(200, json=_CODEX_BODY)

    _mock_api(monkeypatch, handler)

    u = await usage._fetch_codex("/x")

    assert state["refreshed"] == 1 and len(seen) == 1 and seen[0].endswith(state["token"])
    assert u.source == "api" and [(w.key, w.percent) for w in u.windows] == [("five_hour", 7), ("seven_day", 46)]


async def test_codex_server_401_with_valid_looking_token_refreshes_once_and_retries(monkeypatch):
    state = {"token": _jwt(3600), "refreshed": 0}
    _codex_token(monkeypatch, state)
    old = state["token"]

    async def refresh(_):
        state["refreshed"] += 1
        state["token"] = _jwt(7200)

    monkeypatch.setattr(usage, "_refresh_codex_login", refresh)
    _mock_api(monkeypatch, lambda r: httpx.Response(200, json=_CODEX_BODY) if not r.headers["Authorization"].endswith(old)
              else httpx.Response(401, json={}))

    u = await usage._fetch_codex("/x")

    assert state["refreshed"] == 1 and u.source == "api" and u.windows


async def test_codex_401_when_cli_leaves_the_token_unchanged_says_so(monkeypatch):
    _codex_token(monkeypatch, {"token": _jwt(3600)})

    async def refresh(_):
        return "codex 종료 코드 1"

    monkeypatch.setattr(usage, "_refresh_codex_login", refresh)
    _mock_api(monkeypatch, lambda r: httpx.Response(401, json={}))

    u = await usage._fetch_codex("/x")

    assert not u.windows and "토큰이 갱신되지 않았어요(codex 종료 코드 1)" in u.error and "재로그인" in u.error


async def test_codex_network_failure_and_missing_login_are_named(monkeypatch):
    _codex_token(monkeypatch, {"token": _jwt(3600)})

    def boom(r: httpx.Request):
        raise httpx.ConnectTimeout("t")

    _mock_api(monkeypatch, boom)
    assert "사용량 API 실패(ConnectTimeout)" in (await usage._fetch_codex("/x")).error

    monkeypatch.setattr(usage, "_codex_headers", lambda _d: None)
    assert "auth.json" in (await usage._fetch_codex("/x")).error


def _log_usage(age_seconds: float) -> usage.Usage:
    return usage.Usage(windows=[usage.Window("five_hour", "5시간", 99, None)], source="session_log",
                       updated_at=time.time() - age_seconds)


async def test_stale_session_log_fallback_carries_the_api_failure_reason(monkeypatch):
    async def failed(_):
        return usage.Usage(error="사용량 API 401(token_expired)")

    monkeypatch.setattr(usage, "_fetch_codex", failed)
    monkeypatch.setattr(usage, "codex_usage", lambda _d: _log_usage(3.5 * 3600))

    u = await usage._codex_usage_or_log("/x")

    assert u.source == "session_log" and u.windows
    assert u.error == "사용량 API 401(token_expired) · 세션 로그의 마지막 값"


async def test_fresh_session_log_fallback_stays_clean_and_api_error_beats_empty_log(monkeypatch):
    async def failed(_):
        return usage.Usage(error="사용량 API 401(token_expired)")

    monkeypatch.setattr(usage, "_fetch_codex", failed)
    monkeypatch.setattr(usage, "codex_usage", lambda _d: _log_usage(60))
    assert (await usage._codex_usage_or_log("/x")).error is None  # 방금 쓴 로그는 현재 값이다

    monkeypatch.setattr(usage, "codex_usage", lambda _d: usage.Usage(error="아직 따까리에서 쓴 기록이 없어요"))
    assert (await usage._codex_usage_or_log("/x")).error == "사용량 API 401(token_expired)"
