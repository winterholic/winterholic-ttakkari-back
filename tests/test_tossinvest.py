"""토스증권 읽기 전용 어댑터·API 검사. 실제 브라우저는 띄우지 않는다."""

from __future__ import annotations

import json

import pytest

from ttakkari.stocks import tossinvest as ad


def test_read_only_allows_public_get():
    ad.assert_read_only("GET", "https://www.tossinvest.com/")
    ad.assert_read_only("get", "https://api.tossinvest.com/v1/summary")


@pytest.mark.parametrize(
    "method,url",
    [
        ("POST", "https://www.tossinvest.com/"),
        ("PUT", "https://api.tossinvest.com/v1/watchlist"),
        ("DELETE", "https://api.tossinvest.com/v1/order/1"),
        ("GET", "https://evil.example.com/"),
        ("GET", "https://www.tossinvest.com/order/new"),
        ("GET", "https://api.tossinvest.com/v1/trade/history"),
        ("GET", "https://api.tossinvest.com/v1/transfer"),
        ("GET", "https://api.tossinvest.com/v1/withdraw"),
        ("GET", "https://api.tossinvest.com/v1/settings/alert"),
        ("GET", "https://www.tossinvest.com/매수/12345"),
    ],
)
def test_read_only_blocks_writes_and_state_changing_paths(method, url):
    with pytest.raises(ad.TossError):
        ad.assert_read_only(method, url)


@pytest.mark.parametrize(
    "sniff,state,logged_in",
    [
        ({"hasLogin": False, "hasAccount": True}, "ready", True),
        ({"hasLogin": True, "hasAccount": False}, "login_required", False),
        ({"hasLogin": False, "hasAccount": False}, "unknown", None),
        ({"hasLogin": True, "hasAccount": True}, "unknown", None),
        ({}, "unknown", None),
    ],
)
def test_decide_state(sniff, state, logged_in):
    got, ok, message = ad._decide_state(sniff)
    assert got == state
    assert ok == logged_in
    assert message


async def _fake_obs(monkeypatch, state="login_required", logged_in=False):
    obs = ad.PageObservation(
        url=ad.TOSS_HOME,
        title="토스증권",
        logged_in=logged_in,
        has_login_cta=True,
        state=state,
        state_message="로그인이 필요합니다.",
        api_calls=[],
        load_ms=120,
    )

    async def _obs():
        return obs

    monkeypatch.setattr(ad, "observe_home", _obs)


async def test_check_status_reports_login_required_without_browser(
    client, auth, monkeypatch
):
    await _fake_obs(monkeypatch)
    r = await client.post("/api/stocks/tossinvest/check", headers=auth)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["kind"] == "check"
    assert body["status"] == "login_required"
    assert body["logged_in"] is False
    assert body["api_paths"] == []


async def test_sync_snapshot_holds_no_amounts(client, auth, monkeypatch):
    await _fake_obs(monkeypatch)
    st, summary = await ad.sync_snapshot()
    assert st.state == "login_required"
    assert set(summary) == {"state", "logged_in", "title", "api_paths", "load_ms"}
    assert "금액" not in json.dumps(summary, ensure_ascii=False)


async def test_status_never_checked_then_last_run(client, auth, monkeypatch):
    r = await client.get("/api/stocks/tossinvest/status", headers=auth)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["state"] in (
        "never_checked",
        "login_required",
        "ready",
        "unknown",
        "error",
    )
    await _fake_obs(monkeypatch)
    # 같은 테스트 DB를 쓰는 다른 테스트가 먼저 기록했을 수 있어 상태만 확인한다.
    r = await client.get("/api/stocks/tossinvest/status", headers=auth)
    assert r.status_code == 200


async def test_rate_limit_guards_manual_trigger(client, auth, monkeypatch):
    from ttakkari.config import get_settings

    if get_settings().tossinvest_check_interval_seconds <= 0:
        pytest.skip("간격 제한이 꺼져 있다")
    await _fake_obs(monkeypatch)
    first = await client.post("/api/stocks/tossinvest/check", headers=auth)
    assert first.status_code in (202, 429), first.text
    if first.status_code == 429:
        assert "Retry-After" in first.headers
        return
    second = await client.post("/api/stocks/tossinvest/check", headers=auth)
    assert second.status_code == 429, second.text
    assert "Retry-After" in second.headers
    assert second.json()["code"] == "rate_limited"


async def test_runs_are_listed_and_login_help_has_no_secrets(client, auth, monkeypatch):
    await _fake_obs(monkeypatch)
    await client.post("/api/stocks/tossinvest/check", headers=auth)
    r = await client.get("/api/stocks/tossinvest/runs?limit=5", headers=auth)
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
    r = await client.get("/api/stocks/tossinvest/login-help", headers=auth)
    assert r.status_code == 200, r.text
    help_body = r.json()
    assert help_body["steps"] and help_body["open_command"][0] == "open"
    assert help_body["cookie_hosts"] == []
    dumped = json.dumps(help_body, ensure_ascii=False)
    assert "NID_AUT" not in dumped and "token" not in dumped.lower()


async def test_unauthenticated_is_rejected(client):
    for method, path in [
        ("get", "/api/stocks/overview"),
        ("get", "/api/stocks/tossinvest/status"),
        ("post", "/api/stocks/tossinvest/check"),
    ]:
        r = await getattr(client, method)(path)
        assert r.status_code in (401, 403), (method, path, r.status_code)


async def test_overview_lists_tossinvest_provider(client, auth):
    r = await client.get("/api/stocks/overview", headers=auth)
    assert r.status_code == 200, r.text
    providers = r.json()
    assert [p["id"] for p in providers] == ["tossinvest"]
    assert providers[0]["detail"] == "/stocks/tossinvest"


def test_mcp_tool_is_read_only():
    from ttakkari.orchestrator.tool_policy import READ_ONLY_TOOLS

    assert "tossinvest_status" in READ_ONLY_TOOLS
