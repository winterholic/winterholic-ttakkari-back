"""토스증권 읽기 전용 어댑터·API 단위 검사. 브라우저·DB 없이 메모리 가짜로 돌린다."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from datetime import UTC, datetime

# 운영 설정 파일을 읽지 않는다. tests/conftest가 먼저 뜨는 전체 실행에서는 그 값이 우선이다.
os.environ.setdefault("TTAKKARI_NO_ENV_FILE", "1")

import httpx
import pytest
from fastapi import FastAPI

from ttakkari.api import stocks as api
from ttakkari.db import get_db
from ttakkari.models import TossInvestSyncRun
from ttakkari.security.auth import require_user
from ttakkari.stocks import tossinvest as ad


def _utcnow():
    return datetime.now(UTC)


class FakeDb:
    """메모리 행 목록. API가 쓰는 scalar/scalars/add/commit/refresh만 흉내낸다."""

    def __init__(self) -> None:
        self.rows: list[TossInvestSyncRun] = []

    async def scalar(self, _q=None):
        return self.rows[-1] if self.rows else None

    async def scalars(self, _q=None):
        return list(reversed(self.rows))

    async def execute(self, _q):
        pass  # 실제 프로세스 간 lock은 DB 통합 테스트에서 검증한다.

    def add(self, row: TossInvestSyncRun) -> None:
        if not row.id:
            row.id = uuid.uuid4()
        if row.created_at is None:
            row.created_at = _utcnow()
        if row.updated_at is None:
            row.updated_at = _utcnow()
        if row.api_paths is None:
            row.api_paths = []
        self.rows.append(row)

    async def commit(self) -> None:
        pass

    async def refresh(self, _row) -> None:
        pass


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setattr(ad, "_profile_dir", lambda: tmp_path / "toss-chrome")
    db = FakeDb()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    app.dependency_overrides[get_db] = lambda: db
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )
    return http, db, app


def _fake_obs(monkeypatch, state="login_required", logged_in=False):
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


async def test_overview_lists_tossinvest_provider(world):
    http, _, _ = world
    r = await http.get("/api/stocks/overview")
    assert r.status_code == 200, r.text
    providers = r.json()
    assert [p["id"] for p in providers] == ["tossinvest"]
    assert providers[0]["detail"] == "/stocks/tossinvest"
    assert providers[0]["state"] == "never_checked"


async def test_status_then_check_then_runs(world, monkeypatch):
    http, _, _ = world
    r = await http.get("/api/stocks/tossinvest/status")
    assert r.status_code == 200 and r.json()["state"] == "never_checked"
    _fake_obs(monkeypatch)
    r = await http.post("/api/stocks/tossinvest/check")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["kind"] == "check" and body["status"] == "login_required"
    assert body["logged_in"] is False and body["api_paths"] == []
    r = await http.get("/api/stocks/tossinvest/status")
    assert r.json()["state"] == "login_required"
    assert r.json()["last_run"]["id"] == body["id"]
    r = await http.get("/api/stocks/tossinvest/runs?limit=5")
    assert r.status_code == 200 and len(r.json()) == 1


async def test_rate_limit_guards_manual_trigger(world, monkeypatch):
    from ttakkari.config import get_settings

    if get_settings().tossinvest_check_interval_seconds <= 0:
        pytest.skip("간격 제한이 꺼져 있다")
    http, _, _ = world
    _fake_obs(monkeypatch)
    assert (await http.post("/api/stocks/tossinvest/check")).status_code == 202
    second = await http.post("/api/stocks/tossinvest/check")
    assert second.status_code == 429, second.text
    assert "Retry-After" in second.headers


async def test_sync_summary_holds_no_amounts(world, monkeypatch):
    http, _, _ = world
    _fake_obs(monkeypatch)
    r = await http.post("/api/stocks/tossinvest/sync")
    assert r.status_code == 202, r.text
    assert r.json()["kind"] == "sync"
    st, summary = await ad.sync_snapshot()
    assert st.state == "login_required"
    assert set(summary) == {"state", "logged_in", "title", "api_paths", "load_ms"}


async def test_login_help_has_no_secrets(world):
    http, _, _ = world
    r = await http.get("/api/stocks/tossinvest/login-help")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["steps"] and body["open_command"][0] == "open"
    assert body["cookie_hosts"] == []
    assert "toss-chrome" in body["profile_dir"]
    dumped = json.dumps(body, ensure_ascii=False).lower()
    assert "nid_aut" not in dumped and "token" not in dumped


async def test_login_help_has_no_keep_logged_in_mandate(world):
    # 실제 화면에 유지 옵션이 없다(2026-10-18 사용자 보고). 필수로 안내하지 않는다.
    http, _, _ = world
    r = await http.get("/api/stocks/tossinvest/login-help")
    assert r.status_code == 200, r.text
    help_text = json.dumps(r.json(), ensure_ascii=False)
    assert "약 1주일" in help_text
    assert "만료되면 다시 로그인" in help_text
    assert "창을 닫습니다" in help_text


async def test_limit_validation(world):
    http, _, _ = world
    assert (await http.get("/api/stocks/tossinvest/runs?limit=0")).status_code == 422
    assert (await http.get("/api/stocks/tossinvest/runs?limit=101")).status_code == 422


async def test_unauthenticated_is_rejected(world):
    http, _, app = world
    app.dependency_overrides.pop(require_user)
    try:
        assert (await http.get("/api/stocks/overview")).status_code in (401, 403)
        assert (await http.post("/api/stocks/tossinvest/check")).status_code in (
            401,
            403,
        )
    finally:
        app.dependency_overrides[require_user] = lambda: "owner"


def test_mcp_tool_is_read_only():
    from ttakkari.orchestrator.tool_policy import READ_ONLY_TOOLS

    assert "tossinvest_status" in READ_ONLY_TOOLS


def _fake_login_window(monkeypatch, state="waiting", message="열렸습니다."):
    calls: list[None] = []

    def _open():
        calls.append(None)
        return ad.LoginState(state=state, message=message)

    monkeypatch.setattr(ad, "login_chrome_pids", list)
    monkeypatch.setattr(ad, "headless_check_running", lambda: False)
    monkeypatch.setattr(ad, "open_login_window", _open)
    return calls


async def test_login_window_opens_with_fixed_args(world, monkeypatch):
    http, _, _ = world
    calls = _fake_login_window(monkeypatch)
    r = await http.post("/api/stocks/tossinvest/login-window")
    assert r.status_code == 202, r.text
    assert r.json() == {"state": "waiting", "message": "열렸습니다."}
    assert calls == [None]


async def test_login_window_message_explains_session_duration(world, monkeypatch):
    http, _, _ = world
    _fake_login_window(monkeypatch)
    monkeypatch.setattr(
        ad,
        "open_login_window",
        lambda: ad.LoginState(
            state="waiting",
            message="로그인 후 창을 닫아 주세요. 로그인은 약 1주일 유지되며, 만료되면 다시 로그인해 주세요.",
        ),
    )
    r = await http.post("/api/stocks/tossinvest/login-window")
    assert r.status_code == 202, r.text
    assert "로그인 후 창을 닫" in r.json()["message"]
    assert "약 1주일" in r.json()["message"]
    assert "만료되면 다시 로그인" in r.json()["message"]


async def test_login_window_rejects_competing_open_while_first_is_launching(
    world, monkeypatch
):
    http, _, _ = world
    entered = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(ad, "login_chrome_pids", list)
    monkeypatch.setattr(ad, "headless_check_running", lambda: False)

    def _open():
        entered.set()
        assert release.wait(timeout=3)
        return ad.LoginState(state="waiting", message="열렸습니다.")

    monkeypatch.setattr(ad, "open_login_window", _open)
    first = asyncio.create_task(http.post("/api/stocks/tossinvest/login-window"))
    try:
        assert await asyncio.to_thread(entered.wait, timeout=2)
        second = await http.post("/api/stocks/tossinvest/login-window")
        assert second.status_code == 409, second.text
        assert second.json()["detail"]["code"] == "login_window_open"
    finally:
        release.set()
    first_response = await first
    assert first_response.status_code == 202, first_response.text


async def test_login_window_ignores_external_input(world, monkeypatch):
    http, _, _ = world
    calls = _fake_login_window(monkeypatch)
    r = await http.post(
        "/api/stocks/tossinvest/login-window",
        json={"url": "https://evil.example.com/", "profile": "/tmp/evil"},
    )
    assert r.status_code == 202, r.text
    assert calls == [None]


async def test_login_window_conflicts_when_window_open(world, monkeypatch):
    http, _, _ = world
    monkeypatch.setattr(ad, "login_chrome_pids", lambda: [1234])
    monkeypatch.setattr(ad, "headless_check_running", lambda: False)
    r = await http.post("/api/stocks/tossinvest/login-window")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "login_window_open"


async def test_login_window_conflicts_when_check_running(world, monkeypatch):
    http, _, _ = world
    monkeypatch.setattr(ad, "login_chrome_pids", list)
    monkeypatch.setattr(ad, "headless_check_running", lambda: True)
    r = await http.post("/api/stocks/tossinvest/login-window")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["code"] == "check_running"


async def test_login_window_failure_is_500(world, monkeypatch):
    http, _, _ = world
    monkeypatch.setattr(ad, "login_chrome_pids", list)
    monkeypatch.setattr(ad, "headless_check_running", lambda: False)
    monkeypatch.setattr(
        ad,
        "open_login_window",
        lambda: ad.LoginState(state="failed", message="Chrome 없음"),
    )
    r = await http.post("/api/stocks/tossinvest/login-window")
    assert r.status_code == 500, r.text


async def test_login_window_requires_auth(world, monkeypatch):
    http, _, app = world
    _fake_login_window(monkeypatch)
    app.dependency_overrides.pop(require_user)
    try:
        assert (await http.post("/api/stocks/tossinvest/login-window")).status_code in (
            401,
            403,
        )
    finally:
        app.dependency_overrides[require_user] = lambda: "owner"
