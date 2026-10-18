"""Antigravity 사용량: 실측 응답 형태(2026-10-18), 조회 실패 시 마지막 관측값 유지, 창별 state, 그룹별 독립 소진."""
from __future__ import annotations

import time
import uuid

import pytest

from ttakkari.api import accounts as accounts_api
from ttakkari.models import EngineAccount
from ttakkari.runs import account_status, profiles, usage, usage_collector

CLAUDE = "claude-sonnet-5-5-high"
GEMINI = "gemini-3.8-flash-high"
ARGON = "gemini-4-argon-high"


def _iso(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _body(groups: list[dict]) -> dict:
    return {"status": "SUCCESS", "command": {"name": "usage", "data": {"groups": groups}}}


def _bucket(window: str, remaining: float, reset: float | None = None, **extra) -> dict:
    b = {"window": window, "remaining_fraction": remaining, **extra}
    if reset is not None:
        b["reset_time"] = _iso(reset)
    return b


def _w(key: str, percent: int, resets_at: int | None, remaining: float | None = None) -> usage.Window:
    return usage.Window(key, key, percent, resets_at, 100.0 - percent if remaining is None else remaining)


def _observed(now: float, *windows: usage.Window, age: float = 10) -> usage.Usage:
    return usage.Usage(windows=list(windows), source="antigravity_cli", updated_at=now - age, credits_balance=0)


def _account(aid: uuid.UUID, name: str = "agy") -> EngineAccount:
    return EngineAccount(id=aid, name=name, engine="antigravity", config_dir=None, enabled=True, priority=0,
                         max_used_percent=100, pool="personal", work_enabled=True)


@pytest.fixture
def aid():
    account_id = uuid.uuid4()
    yield account_id
    usage.forget(account_id)
    usage._agy_cache_roots.pop(account_id, None)
    usage.set_sink(None)


# ---- 공급자 원시 응답 → 창

def test_disabled_five_hour_bucket_is_not_a_zero_percent_window():
    """주간 한도가 찬 그룹의 5시간 버킷은 disabled/remaining 1/reset 없음으로 온다. 0% 사용 창이 되면 안 된다."""
    now = time.time()
    windows = usage.windows_from_antigravity(_body([
        {"name": "Claude and GPT models", "buckets": [
            _bucket("weekly", 0, now + 4 * 86400),
            _bucket("5h", 1, None, disabled=True),
        ]},
    ]))
    assert [(w.key, w.percent, w.remaining_percent) for w in windows] == [("agy_claude_and_gpt_models_weekly", 100, 0.0)]


def test_weekly_only_tier_has_no_invented_five_hour_window():
    now = time.time()
    windows = usage.windows_from_antigravity(_body([
        {"name": "Gemini Models", "buckets": [_bucket("weekly", 0.266, now + 6 * 86400)]},
        {"name": "Claude and GPT models", "buckets": [_bucket("weekly", 1, now + 7 * 86400)]},
    ]))
    assert [w.key for w in windows] == ["agy_gemini_models_weekly", "agy_claude_and_gpt_models_weekly"]


def test_missing_reset_time_is_kept_as_unknown_not_zero_or_dropped():
    windows = usage.windows_from_antigravity(_body([
        {"name": "Gemini Models", "buckets": [_bucket("5h", 0.97)]},
    ]))
    assert [(w.key, w.percent, w.resets_at) for w in windows] == [("agy_gemini_models_5h", 3, None)]


def test_nearly_exhausted_window_is_not_rounded_up_to_a_limit_hit():
    now = time.time()
    windows = usage.windows_from_antigravity(_body([
        {"name": "Gemini Models", "buckets": [_bucket("weekly", 0.003, now + 86400), _bucket("5h", 0, now + 3600)]},
    ]))
    by = {w.key: w for w in windows}
    assert (by["agy_gemini_models_weekly"].percent, by["agy_gemini_models_weekly"].remaining_percent) == (99, 0.3)
    assert (by["agy_gemini_models_5h"].percent, by["agy_gemini_models_5h"].remaining_percent) == (100, 0.0)


def test_groups_are_mapped_by_name_independent_of_order():
    now = time.time()
    windows = usage.windows_from_antigravity(_body([
        {"name": "Gemini 4 Argon", "buckets": [_bucket("weekly", 0, now + 86400)]},
        {"name": "Gemini Models", "buckets": [_bucket("weekly", 0.5, now + 86400)]},
    ]))
    assert {w.key: w.percent for w in windows} == {"agy_gemini_4_argon_weekly": 100, "agy_gemini_models_weekly": 50}
    assert profiles.quota_group(ARGON) == "agy_gemini_4_argon" and profiles.quota_group(GEMINI) == "agy_gemini_models"


# ---- 크레딧은 쿼터와 별개

@pytest.mark.asyncio
async def test_missing_credit_info_is_not_reported_as_a_failure_nor_as_zero(monkeypatch):
    now = time.time()
    usage_body = _body([{"name": "Gemini Models", "buckets": [_bucket("weekly", 0.75, now + 86400)]}])

    async def fake(name, config_dir=None):
        if name == "usage":
            return usage_body
        raise usage.AgyCommandError("agy credits exited 1", "/credits failed: retrieving credits: no credits info found")

    monkeypatch.setattr(usage, "_agy_command", fake)
    result = await usage._fetch_antigravity()
    assert result.credits_balance is None
    assert result.credits_error == "이 계정에는 Antigravity AI 크레딧 정보가 없어요"
    assert result.windows[0].remaining_percent == 75  # 크레딧 실패가 쿼터를 지우지 않는다


@pytest.mark.asyncio
async def test_credit_command_crash_is_a_failure_and_quota_survives(monkeypatch):
    now = time.time()
    usage_body = _body([{"name": "Gemini Models", "buckets": [_bucket("weekly", 0.75, now + 86400)]}])

    async def fake(name, config_dir=None):
        if name == "usage":
            return usage_body
        raise TimeoutError

    monkeypatch.setattr(usage, "_agy_command", fake)
    result = await usage._fetch_antigravity()
    assert (result.credits_balance, result.credits_error) == (None, "Antigravity 크레딧 조회 실패")
    assert result.windows


def test_error_detail_is_read_from_cli_json_and_truncated():
    assert usage._agy_error_detail(b'{"status":"ERROR","error":"no credits info found"}') == "no credits info found"
    assert usage._agy_error_detail(b"not json") == ""
    assert len(usage._agy_error_detail(('{"error":"' + "x" * 500 + '"}').encode())) == 200


# ---- 조회 실패: 낡은 값을 현재값처럼 쓰지도, 지우지도 않는다

async def _failing(*_a, **_k):
    return usage.Usage(error="Antigravity quota 조회 실패(TimeoutError)")


@pytest.mark.asyncio
async def test_failed_refresh_keeps_last_observation_with_error_and_does_not_re_record(aid, monkeypatch):
    now = time.time()
    emitted = []
    usage.set_sink(lambda account_id, u: emitted.append(u))
    prior = _observed(now, _w("agy_gemini_models_weekly", 40, int(now + 86400)))
    usage.seed(aid, prior)
    monkeypatch.setattr(usage, "_fetch_antigravity", _failing)

    result = await usage._fetch_live(aid, "antigravity", None)

    assert [w.key for w in result.windows] == ["agy_gemini_models_weekly"]
    assert result.updated_at == prior.updated_at  # 관측 시각을 새로 찍지 않는다
    assert "마지막 관측값" in result.error and "TimeoutError" in result.error
    assert emitted == []  # 실패한 조회를 새 측정으로 저장하지 않는다
    assert usage.cached(aid).windows


@pytest.mark.asyncio
async def test_failed_refresh_without_any_prior_stays_empty_not_zero(aid, monkeypatch):
    monkeypatch.setattr(usage, "_fetch_antigravity", _failing)
    result = await usage._fetch_live(aid, "antigravity", None)
    assert result.windows == [] and result.error


@pytest.mark.asyncio
async def test_failed_refresh_keeps_exhausted_group_blocked_and_unknown_headroom(aid, monkeypatch):
    now = time.time()
    usage.seed(aid, _observed(now, _w("agy_claude_and_gpt_models_weekly", 100, int(now + 86400), 0.0),
                              _w("agy_gemini_models_weekly", 30, int(now + 86400))))
    monkeypatch.setattr(usage, "_fetch_antigravity", _failing)
    await usage._fetch_live(aid, "antigravity", None)

    claude = profiles.Profile(aid, "a", "antigravity", model=CLAUDE)
    gemini = profiles.Profile(aid, "a", "antigravity", model=GEMINI)
    assert profiles.usage_limit_until(claude, now) == pytest.approx(now + 86400, abs=2)  # 소진은 초기화까지 유지
    assert profiles.usage_limit_until(gemini, now) is None  # 다른 그룹은 막지 않는다
    assert profiles._usage_headroom(gemini, now) is None  # 실패 뒤 값은 현재 잔여량이 아니다


@pytest.mark.asyncio
async def test_success_after_failure_replaces_retained_value(aid, monkeypatch):
    now = time.time()
    usage.seed(aid, _observed(now, _w("agy_gemini_models_weekly", 40, int(now + 86400))))
    monkeypatch.setattr(usage, "_fetch_antigravity", _failing)
    await usage._fetch_live(aid, "antigravity", None)

    async def ok(*_a, **_k):
        return usage.Usage(windows=[_w("agy_gemini_models_weekly", 10, int(now + 86400))], source="antigravity_cli",
                           updated_at=time.time())

    monkeypatch.setattr(usage, "_fetch_antigravity", ok)
    result = await usage._fetch_live(aid, "antigravity", None)
    assert result.error is None and result.windows[0].percent == 10


@pytest.mark.asyncio
async def test_one_account_failure_does_not_touch_another_accounts_values(monkeypatch):
    now = time.time()
    a, b = uuid.uuid4(), uuid.uuid4()
    usage.seed(a, _observed(now, _w("agy_gemini_models_weekly", 10, int(now + 86400))))
    usage.seed(b, _observed(now, _w("agy_gemini_models_weekly", 90, int(now + 86400))))
    monkeypatch.setattr(usage, "_fetch_antigravity", _failing)
    try:
        await usage._fetch_live(a, "antigravity", None)
        assert usage.cached(a).error and usage.cached(b).error is None
        assert usage.cached(a).windows[0].percent == 10 and usage.cached(b).windows[0].percent == 90
    finally:
        usage.forget(a)
        usage.forget(b)
        usage._agy_cache_roots.pop(a, None)


@pytest.mark.asyncio
async def test_retention_is_not_used_for_a_changed_account_folder(aid, monkeypatch):
    now = time.time()
    usage.seed(aid, _observed(now, _w("agy_gemini_models_weekly", 40, int(now + 86400))))
    usage._agy_cache_roots[aid] = "/old/profile"
    monkeypatch.setattr(usage, "_fetch_antigravity", _failing)
    result = await usage._fetch_live(aid, "antigravity", "/new/profile")
    assert result.windows == []


# ---- API 창별 state

def _dto(aid, u: usage.Usage):
    return accounts_api._usage_out(0, _account(aid), {}, u)


def test_api_marks_expired_window_unavailable_and_keeps_the_other_window_current(aid):
    now = time.time()
    u = _observed(now, _w("agy_gemini_models_5h", 60, int(now - 5)), _w("agy_gemini_models_weekly", 20, int(now + 86400)))
    dto = _dto(aid, u)
    by = {w.key: w for w in dto.windows}
    assert by["agy_gemini_models_5h"].state == "unavailable" and by["agy_gemini_models_5h"].remaining_percent is None
    assert by["agy_gemini_models_5h"].percent == 0  # 낡은 60% 를 내보내지 않는다
    assert by["agy_gemini_models_weekly"].state == "current" and by["agy_gemini_models_weekly"].remaining_percent == 80
    assert dto.stale is True  # 지난 창이 있으면 계정 단위로는 낡음


def test_api_exhausted_window_past_reset_is_not_shown_as_100_percent(aid):
    now = time.time()
    dto = _dto(aid, _observed(now, _w("agy_claude_and_gpt_models_weekly", 100, int(now - 1), 0.0)))
    [w] = dto.windows  # 창을 숨기지 않는다
    assert (w.state, w.percent, w.remaining_percent) == ("unavailable", 0, None)
    assert dto.error and "초기화" in dto.error


def test_api_failed_refresh_shows_stale_values_with_error_and_observation_time(aid):
    now = time.time()
    u = _observed(now, _w("agy_gemini_models_weekly", 40, int(now + 86400)))
    u.error = "Antigravity quota 조회 실패(TimeoutError) · 마지막 관측값"
    dto = _dto(aid, u)
    assert dto.windows[0].state == "stale" and dto.windows[0].percent == 40
    assert dto.stale is True and dto.error and dto.updated_at == u.updated_at


def test_api_old_measurement_without_error_is_stale_even_before_reset(aid):
    now = time.time()
    u = _observed(now, _w("agy_gemini_models_weekly", 40, int(now + 86400)), age=3600)
    dto = _dto(aid, u)
    assert dto.windows[0].state == "stale" and dto.stale is True


def test_api_fresh_measurement_is_current_and_unknown_reset_is_not_hidden(aid):
    now = time.time()
    dto = _dto(aid, _observed(now, _w("agy_gemini_models_5h", 3, None), _w("agy_gemini_models_weekly", 20, int(now + 86400))))
    assert [w.state for w in dto.windows] == ["current", "current"]
    assert dto.windows[0].resets_at is None and dto.windows[0].percent == 3
    assert dto.stale is False and dto.credits_balance == 0


def test_api_credits_balance_and_quota_stay_separate(aid):
    now = time.time()
    u = _observed(now, _w("agy_claude_and_gpt_models_weekly", 100, int(now + 86400), 0.0))
    u.credits_balance = 7
    dto = _dto(aid, u)
    assert dto.credits_balance == 7 and dto.windows[0].remaining_percent == 0.0


# ---- 오케스트레이터용 account_usage

def test_account_status_quota_state_per_window_and_failure_is_stale(aid):
    now = time.time()
    u = _observed(now, _w("agy_gemini_models_5h", 60, int(now - 5)), _w("agy_gemini_models_weekly", 20, int(now + 86400)))
    usage.seed(aid, u)
    view = account_status._usage(aid, now, engine="antigravity")
    states = {q["key"]: q["state"] for q in view["model_quotas"]}
    assert states == {"agy_gemini_models_5h": "unavailable", "agy_gemini_models_weekly": "current"}
    expired = next(q for q in view["model_quotas"] if q["key"] == "agy_gemini_models_5h")
    assert expired["used_percent"] is None and expired["remaining_percent"] is None and expired["reset_since_measured"]

    u.error = "조회 실패 · 마지막 관측값"
    view = account_status._usage(aid, now, engine="antigravity")
    assert view["stale"] is True
    assert {q["key"]: q["state"] for q in view["model_quotas"]}["agy_gemini_models_weekly"] == "stale"


# ---- 그룹별 독립 소진·경계

def test_exhausted_group_blocks_only_its_models_and_other_accounts(aid):
    now = time.time()
    other = uuid.uuid4()
    usage.seed(aid, _observed(now, _w("agy_claude_and_gpt_models_weekly", 100, int(now + 3 * 86400), 0.0),
                              _w("agy_gemini_models_5h", 20, int(now + 3600)),
                              _w("agy_gemini_models_weekly", 50, int(now + 86400)),
                              _w("agy_gemini_4_argon_weekly", 100, int(now + 2 * 86400), 0.0)))
    usage.seed(other, _observed(now, _w("agy_claude_and_gpt_models_weekly", 10, int(now + 3 * 86400))))
    try:
        for model, blocked in ((CLAUDE, True), (GEMINI, False), (ARGON, True)):
            until = profiles.usage_limit_until(profiles.Profile(aid, "a", "antigravity", model=model), now)
            assert (until is not None) is blocked, model
        assert profiles.usage_limit_until(profiles.Profile(other, "b", "antigravity", model=CLAUDE), now) is None
    finally:
        usage.forget(other)


def test_exhaustion_lifts_exactly_at_reset_boundary(aid):
    now = time.time()
    usage.seed(aid, _observed(now, _w("agy_claude_and_gpt_models_weekly", 100, int(now) + 100, 0.0)))
    p = profiles.Profile(aid, "a", "antigravity", model=CLAUDE)
    assert profiles.usage_limit_until(p, now) == int(now) + 100
    assert profiles.usage_limit_until(p, int(now) + 100) is None  # 초기화 시각 도달
    assert profiles.usage_limit_until(p, int(now) + 101) is None


def test_exhausted_window_without_reset_time_is_rechecked_soon_not_forever(aid):
    now = time.time()
    usage.seed(aid, _observed(now, _w("agy_gemini_models_5h", 100, None, 0.0)))
    until = profiles.usage_limit_until(profiles.Profile(aid, "a", "antigravity", model=GEMINI), now)
    assert now < until <= now + 91


def test_headroom_ignores_other_group_exhaustion_and_expired_windows(aid):
    now = time.time()
    usage.seed(aid, _observed(now, _w("agy_claude_and_gpt_models_weekly", 100, int(now + 86400), 0.0),
                              _w("agy_gemini_models_5h", 90, int(now - 1)),
                              _w("agy_gemini_models_weekly", 50, int(now + 86400))))
    room = profiles._usage_headroom(profiles.Profile(aid, "a", "antigravity", model=GEMINI), now)
    assert room is not None and room[0] == pytest.approx(50)  # 지난 5시간 창(90%)과 claude 그룹 소진은 반영하지 않는다
    assert profiles._usage_headroom(profiles.Profile(aid, "a", "antigravity", model=CLAUDE), now)[0] == 0


# ---- 캐시·수집기

def test_cache_refetches_when_any_window_reset_but_not_after_a_failed_read(aid):
    now = time.time()
    u = _observed(now, _w("agy_gemini_models_5h", 60, int(now - 5)), _w("agy_gemini_models_weekly", 20, int(now + 86400)))
    usage._cache[aid] = (now, u)
    usage._agy_cache_roots[aid] = None
    assert usage._ready_value(aid, "antigravity", None, False) is None  # 성공한 측정 + 지난 창 → 새로 읽는다
    u.error = "조회 실패 · 마지막 관측값"
    assert usage._ready_value(aid, "antigravity", None, False) is u  # 실패 뒤엔 TTL 동안 하위 프로세스를 다시 띄우지 않는다


async def test_collector_reset_refetch_follows_backoff_while_failures_continue(client, monkeypatch):
    """실패 뒤 유지한 값에는 지난 창이 남는다. 그 창이 30초마다 재조회를 부르면 백오프가 무의미해진다."""
    from ttakkari.config import get_settings
    from ttakkari.db import sessionmaker

    # conftest 는 수집기를 끄려고 주기를 0 으로 둔다. 백오프 계산은 기본값으로 시험한다.
    monkeypatch.setattr(get_settings(), "usage_poll_seconds", 300)
    monkeypatch.setattr(get_settings(), "usage_poll_busy_seconds", 90)
    monkeypatch.setattr(usage_collector, "GAP_SECONDS", (0, 0))
    account_id = uuid.uuid4()
    async with sessionmaker()() as db:
        db.add(EngineAccount(id=account_id, name="agy-collector", engine="antigravity", config_dir=None, enabled=True,
                             priority=99, max_used_percent=100, pool="personal", work_enabled=False))
        await db.commit()
    calls = []

    async def fetch(aid, engine, cdir):
        if aid == account_id:
            calls.append(time.time())
        return usage.Usage(error="Antigravity quota 조회 실패(TimeoutError)")

    collector = usage_collector.Collector(fetch)
    t0 = time.time()
    try:
        await collector.tick(t0)
        await collector.tick(t0 + 10)
        assert len(calls) == 1  # 첫 수집
        state = collector._state[account_id]
        assert state.failures == 1
        # 실패 중 유지된 값: 측정 뒤 5시간 창이 지났다.
        usage._cache[account_id] = (t0, _observed(t0, _w("agy_gemini_models_5h", 60, int(t0 + 100)), age=-1))
        await collector.tick(t0 + 150)
        assert len(calls) == 1, "백오프 중에는 지난 창 때문에 다시 읽지 않는다"
        # 성공하면 지난 창은 새 값으로 바뀌고, 이후 지난 창이 다시 생기면 즉시 재조회한다.
        state.failures = 0
        await collector.tick(t0 + 200)
        assert len(calls) == 2
    finally:
        usage.forget(account_id)
        async with sessionmaker()() as db:
            await db.delete(await db.get(EngineAccount, account_id))
            await db.commit()
