"""계정 사용량 주기 수집·저장·추세(runs/usage_collector.py, usage_store.py)와 account_usage 의 새 필드."""
from __future__ import annotations

import random
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from conftest import agent_calls
from sqlalchemy import func, select
from test_account_selection import _three_accounts
from test_orchestrator import _orch, _session

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import AccountUsageSample
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles, usage_store
from ttakkari.runs import usage as usage_mod
from ttakkari.runs import usage_collector as col


@pytest.fixture(autouse=True)
def _poll_defaults(monkeypatch):
    # conftest 는 수집기를 끄려고 주기를 0 으로 둔다. 주기 계산은 기본값으로 시험한다.
    st = get_settings()
    monkeypatch.setattr(st, "usage_poll_seconds", 300)
    monkeypatch.setattr(st, "usage_poll_busy_seconds", 90)


def _w(key: str, percent: int, resets_in: int = 3600) -> usage_mod.Window:
    return usage_mod.Window(key, usage_mod.label_of(key), percent, int(time.time()) + resets_in)


def _u(*windows, source="api") -> usage_mod.Usage:
    return usage_mod.Usage(windows=list(windows), source=source, updated_at=time.time())


async def _samples(account_id) -> list[AccountUsageSample]:
    await usage_store.flush()
    async with sessionmaker()() as db:
        return list(await db.scalars(select(AccountUsageSample).where(AccountUsageSample.account_id == account_id)
                                     .order_by(AccountUsageSample.measured_at)))


# ---- 주기

def test_interval_normal_busy_low_remaining_and_backoff():
    s = get_settings().model_copy(update={"usage_poll_seconds": 300, "usage_poll_busy_seconds": 90, "usage_poll_jitter": 0.0,
                                          "usage_low_remaining_percent": 20, "usage_backoff_max_seconds": 1800})
    assert col.interval_for(s, running=0, remaining=80, failures=0) == 300
    assert col.interval_for(s, running=1, remaining=80, failures=0) == 90
    assert col.interval_for(s, running=0, remaining=19, failures=0) == 90
    assert col.interval_for(s, running=0, remaining=20, failures=0) == 300
    assert col.interval_for(s, running=0, remaining=None, failures=0) == 300
    assert [col.interval_for(s, running=0, remaining=50, failures=n) for n in (1, 2, 3, 9)] == [600, 1200, 1800, 1800]


def test_interval_jitter_stays_within_bounds():
    s = get_settings().model_copy(update={"usage_poll_seconds": 300, "usage_poll_jitter": 0.15})
    rng = random.Random(1)
    vals = [col.interval_for(s, running=0, remaining=50, failures=0, rng=rng) for _ in range(200)]
    assert min(vals) >= 300 * 0.85 and max(vals) <= 300 * 1.15 and len(set(vals)) > 50


def test_lowest_remaining_ignores_model_windows_and_new_windows():
    now = time.time()
    u = _u(_w("five_hour", 90), _w("seven_day", 40), _w("seven_day_fable", 99))
    assert col.lowest_remaining(u, now) == 10
    u = _u(usage_mod.Window("five_hour", "5시간", 100, int(now) - 5), _w("seven_day", 40))
    assert col.lowest_remaining(u, now) == 60  # 초기화가 지난 5시간 창은 새로 시작됐다
    assert col.lowest_remaining(None, now) is None


# ---- 수집기

async def _accounts(client, auth, make_dir):
    accs = await _three_accounts(client, auth, make_dir)
    return {k: uuid.UUID(v["id"]) for k, v in accs.items()}


async def test_tick_collects_enabled_accounts_only_and_respects_schedule(client, auth, make_dir, monkeypatch):
    ids = await _accounts(client, auth, make_dir)
    r = await client.patch(f"/api/accounts/{ids['codex']}", json={"enabled": False}, headers=auth)
    assert r.status_code == 200, r.text
    monkeypatch.setattr(col, "GAP_SECONDS", (0, 0))
    calls: list[uuid.UUID] = []

    async def fetch(aid, engine, cdir):
        calls.append(aid)
        return _u(_w("five_hour", 30), _w("seven_day", 10))

    c = col.Collector(fetch)
    now = time.time()
    assert await c.tick(now) == []  # 첫 수집은 몇 초 뒤로 엇갈려 잡는다
    done = await c.tick(now + 60)
    assert set(done) == {ids["claude"], ids["claude-2"]} and ids["codex"] not in calls
    assert await c.tick(now + 61) == []  # 방금 읽었다
    assert set(await c.tick(now + 60 + 300 * 1.2)) == {ids["claude"], ids["claude-2"]}


async def test_tick_busy_account_is_polled_faster_and_failure_backs_off(client, auth, make_dir, monkeypatch):
    ids = await _accounts(client, auth, make_dir)
    for n in ("claude-2", "codex"):
        await client.patch(f"/api/accounts/{ids[n]}", json={"enabled": False}, headers=auth)
    monkeypatch.setattr(col, "GAP_SECONDS", (0, 0))
    s = get_settings()
    monkeypatch.setattr(s, "usage_poll_jitter", 0.0)
    ok = {"v": True}

    async def fetch(aid, engine, cdir):
        return _u(_w("five_hour", 30), _w("seven_day", 10)) if ok["v"] else usage_mod.Usage(error="사용량 API 429")

    c = col.Collector(fetch)
    t0 = time.time()
    await c.tick(t0)
    assert await c.tick(t0 + 10) == [ids["claude"]]
    t1 = c._state[ids["claude"]].next_due
    assert 295 <= t1 - (t0 + 10) <= 305  # 놀고 있으면 평소 주기
    rid = uuid.uuid4()
    profiles._in_use[rid] = ids["claude"]
    try:
        await c.tick(t1)
        assert 85 <= c._state[ids["claude"]].next_due - t1 <= 95  # Run 이 쓰는 중이면 busy 주기
    finally:
        profiles.release(rid)
    ok["v"] = False
    t = c._state[ids["claude"]].next_due
    await c.tick(t)
    assert c._state[ids["claude"]].failures == 1
    assert 595 <= c._state[ids["claude"]].next_due - t <= 605  # 실패하면 두 배
    ok["v"] = True
    t = c._state[ids["claude"]].next_due
    await c.tick(t)
    assert c._state[ids["claude"]].failures == 0


async def test_tick_refetches_right_after_known_reset_time(client, auth, make_dir, monkeypatch):
    ids = await _accounts(client, auth, make_dir)
    for n in ("claude-2", "codex"):
        await client.patch(f"/api/accounts/{ids[n]}", json={"enabled": False}, headers=auth)
    n_calls = {"n": 0}

    async def fetch(aid, engine, cdir):
        n_calls["n"] += 1
        return _u(_w("five_hour", 100, 3600))

    c = col.Collector(fetch)
    t0 = time.time()
    await c.tick(t0)
    await c.tick(t0 + 10)
    assert n_calls["n"] == 1
    # 측정(캐시) 이후 초기화 시각이 지났다면 다음 주기를 기다리지 않는다.
    usage_mod._cache[ids["claude"]] = (t0, usage_mod.Usage(windows=[usage_mod.Window("five_hour", "5시간", 100, int(t0 + 100))],
                                                           source="api", updated_at=t0 + 50))
    await c.tick(t0 + 150)
    assert n_calls["n"] == 2


# ---- 저장

async def test_record_dedupes_and_load_latest_seeds_cache(client, auth, make_dir):
    ids = await _accounts(client, auth, make_dir)
    aid = ids["claude"]
    u = _u(_w("five_hour", 30), _w("seven_day", 10))
    assert await usage_store.record(aid, u) is True
    assert await usage_store.record(aid, u) is False  # 같은 값
    assert await usage_store.record(aid, _u(_w("five_hour", 31), _w("seven_day", 10))) is False  # 너무 잦다
    usage_store.reset()
    assert await usage_store.record(aid, _u()) is False  # 창이 없으면 남기지 않는다
    rows = await _samples(aid)
    assert len(rows) == 1 and rows[0].source == "api"
    assert [(w["key"], w["percent"]) for w in rows[0].windows] == [("five_hour", 30), ("seven_day", 10)]
    assert "token" not in str(rows[0].windows)

    usage_mod.clear_cache()
    assert usage_mod.cached(aid) is None
    async with sessionmaker()() as db:
        assert await usage_store.load_latest(db) == 1
    got = usage_mod.cached(aid)
    assert got and [(w.key, w.percent) for w in got.windows] == [("five_hour", 30), ("seven_day", 10)]
    assert abs(got.updated_at - rows[0].measured_at.timestamp()) < 1


async def test_sink_saves_stream_and_api_values(client, auth, make_dir, monkeypatch):
    ids = await _accounts(client, auth, make_dir)
    usage_store.install()
    usage_mod.remember_stream(ids["claude"], [_w("five_hour", 55)])
    rows = await _samples(ids["claude"])
    assert [r.source for r in rows] == ["stream"] and rows[0].windows[0]["percent"] == 55

    async def api(_d):
        return _u(_w("five_hour", 12), _w("seven_day", 3))

    monkeypatch.setattr(usage_mod, "_fetch_claude", api)
    await usage_mod.usage_for(ids["claude-2"], "claude", None, refresh=True)
    rows = await _samples(ids["claude-2"])
    assert [r.source for r in rows] == ["api"]


async def test_run_stream_event_is_saved_while_running(client, auth, make_dir):
    from helpers import run_to_end

    ids = await _accounts(client, auth, make_dir)
    chat = await _session(client, auth, make_dir, engine="claude")
    usage_store.install()
    await run_to_end(client, auth, chat["id"], "hi")
    assert agent_calls()
    rows = await _samples(ids["claude"])
    assert rows and rows[0].source == "stream"
    assert [(w["key"], w["percent"]) for w in rows[0].windows] == [("five_hour", 42), ("seven_day", 10)]


async def test_prune_keeps_recent_and_newest_per_account(client, auth, make_dir):
    ids = await _accounts(client, auth, make_dir)
    old = datetime.now(UTC) - timedelta(days=30)
    async with sessionmaker()() as db:
        for aid, ages in ((ids["claude"], (30, 20, 1)), (ids["claude-2"], (30, 25))):
            for d in ages:
                db.add(AccountUsageSample(account_id=aid, measured_at=datetime.now(UTC) - timedelta(days=d), source="api", plan=None,
                                          windows=[{"key": "five_hour", "percent": 1, "resets_at": None}]))
        await db.commit()
        assert old
        n = await usage_store.prune(db, 14)
    assert n == 3
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(AccountUsageSample)) == 2
    # 오래됐어도 계정의 마지막 측정값은 남는다.
    assert len(await _samples(ids["claude-2"])) == 1


def test_burn_rate_uses_same_reset_cycle_and_needs_span():
    now = time.time()
    cur = usage_mod.Window("five_hour", "5시간", 40, int(now) + 3600)
    P = usage_store.Point
    pts = [P(now - 7000, 90, int(now) - 3600), P(now - 3600, 10, int(now) + 3600), P(now - 1800, 25, int(now) + 3600),
           P(now, 40, int(now) + 3600)]
    assert usage_store.burn_rate(pts, "five_hour", cur, now) == 30.0  # 지난 주기의 90% 는 뺀다
    assert usage_store.burn_rate(pts[-1:], "five_hour", cur, now) is None
    assert usage_store.burn_rate([P(now - 60, 38, int(now) + 3600), P(now, 40, int(now) + 3600)], "five_hour", cur, now) is None
    assert usage_store.burn_rate([P(now - 3600, 50, int(now) + 3600), P(now, 40, int(now) + 3600)], "five_hour", cur, now) is None


# ---- account_usage 도구

async def test_account_usage_includes_burn_rate_and_min_remaining(client, auth, make_dir):
    ids = await _accounts(client, auth, make_dir)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    now = time.time()
    resets = int(now) + 3 * 3600
    async with sessionmaker()() as db:
        for ago, pct in ((3000, 10), (1800, 25), (0, 40)):
            db.add(AccountUsageSample(account_id=ids["claude"], measured_at=datetime.fromtimestamp(now - ago, UTC), source="api", plan=None,
                                      windows=[{"key": "five_hour", "percent": pct, "resets_at": resets},
                                               {"key": "seven_day", "percent": 20, "resets_at": resets + 86400}]))
        await db.commit()
    usage_mod.remember_stream(ids["claude"], [usage_mod.Window("five_hour", "5시간", 40, resets),
                                              usage_mod.Window("seven_day", "주간", 20, resets + 86400)])
    body = (await client.get("/api/orchestrator/accounts/usage", headers=h)).json()
    c = next(a for a in body["accounts"] if a["name"] == "claude")["usage"]
    f = c["five_hour"]
    assert f["used_percent"] == 40 and c["min_remaining_percent"] == 60
    assert 10799 <= f["resets_in_seconds"] <= 10800
    assert f["burn_percent_per_hour"] == 36.0  # 3000초 동안 +30
    assert f["exhausts_in_seconds"] == 6000 and f["exhausts_before_reset"] is True
    assert c["weekly"]["burn_percent_per_hour"] is None or c["weekly"]["burn_percent_per_hour"] == 0.0
    assert c["stale"] is False
    # 이력이 없는 계정은 속도를 모른다고 알린다.
    usage_mod.remember_stream(ids["claude-2"], [usage_mod.Window("five_hour", "5시간", 5, resets)])
    body = (await client.get("/api/orchestrator/accounts/usage", headers=h)).json()
    c2 = next(a for a in body["accounts"] if a["name"] == "claude-2")["usage"]
    assert c2["five_hour"]["burn_percent_per_hour"] is None and c2["five_hour"]["exhausts_in_seconds"] is None
    assert "5 min" in body["note"]


async def test_stale_threshold_follows_setting(client, auth, make_dir, monkeypatch):
    ids = await _accounts(client, auth, make_dir)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    usage_mod.remember_stream(ids["claude"], [_w("five_hour", 5)])
    usage_mod._stream[ids["claude"]].updated_at = time.time() - 200
    monkeypatch.setattr(get_settings(), "usage_stale_seconds", 100)
    body = (await client.get("/api/orchestrator/accounts/usage", headers=h)).json()
    assert next(a for a in body["accounts"] if a["name"] == "claude")["usage"]["stale"] is True
