"""Muse /usage must never leave the account stuck in unknown or stale quota (2026-10-18 deadlock).

After the 5-hour window resets, an idle Muse is omitted by the provider (`Currently unavailable`).
Routing used to drop accounts without confirmed headroom, so nothing ever produced a new observation.
These tests pin the invariant (stale values are never spare capacity) and the escape (bounded exploration).
"""
import asyncio
import json
import sys
import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from ttakkari.runs import account_status, muse_usage, profiles, usage, worker_routing

MUSE = "muse-spark-1.3"
CARD = """Subscription · Power
Current {cur}% used · Resets at {reset}
Weekly {week}% used · Resets {wday} at 9:00 AM
as of {asof}"""


def card(cur=5, reset="4:30 PM", week=10, wday="Oct 19", asof="2:00 PM"):
    return CARD.format(cur=cur, reset=reset, week=week, wday=wday, asof=asof)


def ms(dt):
    return int(dt.timestamp() * 1000)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(usage, "MUSE_PING", False)  # 관측 핑은 실제 Muse 와 DB 를 쓴다
    profiles.reset_cooldowns()
    yield
    profiles.reset_cooldowns()
    usage.clear_cache()


def muse_profile(aid, model=MUSE):
    return profiles.Profile(aid, "muse", "muse", model=model)


def seed(aid, *, five=0, week=20, five_reset=3600, week_reset=86400, age=60, error=None, login="ok"):
    now = time.time()
    usage.seed(aid, usage.Usage(
        windows=[usage.Window("five_hour", "5h", five, int(now + five_reset)),
                 usage.Window("seven_day", "7d", week, int(now + week_reset))],
        source="muse_cli", updated_at=now - age, login=login, error=error), refresh=True)


# ---- parsing: reset/time boundaries (collector pins TZ=UTC for the CLI)

def test_reset_at_observation_minute_is_expired_instead_of_tomorrow():
    now = datetime(2026, 10, 18, 14, 0, 20, tzinfo=UTC)
    after = muse_usage.parse_usage_card(card(reset="2:01 PM", asof="2:00 PM"), now)
    assert after['usage']['usage']['window']['resetsAtMs'] == ms(datetime(2026, 10, 18, 14, 1, tzinfo=UTC))
    # At the boundary the CLI may still paint the exhausted window for this minute.
    same = muse_usage.parse_usage_card(card(reset="2:00 PM", asof="2:00 PM"), now)
    assert same['usage']['usage']['window']['resetsAtMs'] == ms(datetime(2026, 10, 18, 14, 0, tzinfo=UTC))


def test_five_hour_reset_never_turns_a_recently_expired_window_into_24_hour_exhaustion():
    now = datetime(2026, 10, 18, 14, 1, 20, tzinfo=UTC)
    body = muse_usage.parse_usage_card(card(cur=100, reset="2:00 PM", asof="2:01 PM"), now)
    assert body['usage']['usage']['window']['resetsAtMs'] == ms(datetime(2026, 10, 18, 14, 0, tzinfo=UTC))


def test_persisted_impossible_muse_reset_does_not_block_after_restart():
    aid = uuid4()
    now = time.time()
    seed(aid, five=100, five_reset=3600, week=82, week_reset=-3600, age=23 * 3600)
    restored = usage.cached(aid)
    assert not any(w.key == "five_hour" for w in restored.windows)
    assert profiles.usage_limit_until(muse_profile(aid), now) is None
    assert profiles._usage_headroom(muse_profile(aid), now) is None
    assert restored.error


def test_observation_and_reset_across_midnight():
    now = datetime(2026, 10, 19, 0, 1, 5, tzinfo=UTC)
    body = muse_usage.parse_usage_card(card(reset="12:03 AM", asof="11:58 PM", wday="Oct 20"), now)
    data = body['usage']['usage']
    assert data['observedAtMs'] == ms(datetime(2026, 10, 18, 23, 58, tzinfo=UTC))
    assert data['window']['resetsAtMs'] == ms(datetime(2026, 10, 19, 0, 3, tzinfo=UTC))


def test_weekly_reset_more_than_a_week_ahead_is_rejected():
    now = datetime(2026, 10, 18, 14, 0, tzinfo=UTC)
    assert muse_usage.parse_usage_card(card(wday="Nov 20"), now) is None


@pytest.mark.parametrize("drop", ["Current", "Weekly", "as of"])
def test_partial_card_missing_one_window_or_time_is_rejected(drop):
    lines = [line for line in card().splitlines() if not line.strip().startswith(drop)]
    assert muse_usage.parse_usage_card("\n".join(lines), datetime(2026, 10, 18, 14, 0, tzinfo=UTC)) is None


@pytest.mark.parametrize("variant", ["Usage currently unavailable", "Currently unavailable"])
def test_both_unavailable_wordings_are_unobserved_not_zero(variant):
    body = muse_usage.parse_usage_card(f"Subscription · Power\n{variant}", datetime.now(UTC))
    assert body['quota_status'] == 'unavailable' and body['usage'] == {}
    assert usage.muse_usage_from_probe(body).windows == []


# ---- native reader failure modes

def test_reader_reports_spawn_failure_without_sending_anything(tmp_path):
    result = muse_usage.read_usage_card(str(tmp_path / "missing-binary"), {}, timeout=1)
    assert result['quota_status'] == 'spawn_failed' and result['builtin_usage_sent'] is False


def test_reader_reports_cli_exit_before_composer(tmp_path):
    fake = tmp_path / "dying-cli"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n")
    fake.chmod(0o700)
    result = muse_usage.read_usage_card(str(fake), {}, timeout=3)
    assert result['quota_status'] == 'exited' and result['builtin_usage_sent'] is False
    assert result['child_reaped'] is True


def test_reader_times_out_when_composer_never_paints(tmp_path):
    fake = tmp_path / "hung-cli"
    fake.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    fake.chmod(0o700)
    result = muse_usage.read_usage_card(str(fake), {}, timeout=.6)
    assert result['quota_status'] == 'timeout' and result['child_reaped'] is True


def test_reader_reports_permission_denied_screen(tmp_path):
    fake = tmp_path / "denied-cli"
    fake.write_text(f"#!{sys.executable}\nimport time\nprint('Operation not permitted', flush=True)\ntime.sleep(3)\n")
    fake.chmod(0o700)
    result = muse_usage.read_usage_card(str(fake), {}, timeout=3)
    assert result['quota_status'] == 'permission_denied' and result['builtin_usage_sent'] is False


# ---- collector -> usage pipeline failure modes (each ends as unobserved, never as a fabricated value)

class _Proc:
    def __init__(self, stdout=b"{}", returncode=0, hang=False):
        self._stdout, self.returncode, self._hang = stdout, returncode, hang
        self.terminate, self.kill = MagicMock(), MagicMock()

    async def communicate(self):
        if self._hang:
            raise TimeoutError
        return self._stdout, b""

    async def wait(self):
        return self.returncode


async def _no_timeout(coro, timeout):
    return await coro


async def _fetch(proc):
    with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
            patch.object(asyncio, "wait_for", _no_timeout):
        return await usage._fetch_muse(None)


async def test_sdk_probe_nonzero_exit_is_a_failure_without_windows():
    result = await _fetch(_Proc(returncode=1))
    assert result.windows == [] and "종료(1)" in result.error


async def test_sdk_probe_invalid_json_is_a_failure_without_windows():
    result = await _fetch(_Proc(stdout=b"not json"))
    assert result.windows == [] and "JSONDecodeError" in result.error


async def test_sdk_probe_non_object_response_is_a_failure_without_windows():
    result = await _fetch(_Proc(stdout=b"[1]"))
    assert result.windows == [] and "형식 오류" in result.error


async def test_sdk_probe_timeout_is_a_failure_without_windows():
    proc = _Proc(hang=True)
    proc.returncode = None
    result = await _fetch(proc)
    assert result.windows == [] and "TimeoutError" in result.error
    proc.terminate.assert_called_once()


@pytest.mark.parametrize("native", [
    {"usage": {}, "quota_status": "helper_exit_7"},
    {"usage": {}, "quota_status": "timeout"},
    {"usage": {}, "quota_status": "unavailable", "plan": "Power"},
])
async def test_logged_in_sdk_without_usage_and_native_failure_stays_unobserved(native):
    sdk = {"account": {"state": "accountLogin"}, "usage": {}}
    with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=_Proc(json.dumps(sdk).encode()))), \
            patch.object(asyncio, "wait_for", _no_timeout), \
            patch.object(usage, "_fetch_muse_native", AsyncMock(return_value=native)):
        result = await usage._fetch_muse(None)
    assert result.windows == [] and result.login == "ok" and native["quota_status"] in result.error


@pytest.mark.parametrize("code,stdout,expected", [
    (3, b"", "helper_exit_3"), (0, b"nope", None), (0, b"[]", "invalid_response")])
async def test_native_helper_failure_shapes(code, stdout, expected):
    proc = _Proc(stdout=stdout, returncode=code)
    with patch.object(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc)), \
            patch.object(asyncio, "wait_for", _no_timeout):
        if expected is None:
            with pytest.raises(ValueError):  # _fetch_muse converts this into an unobserved failure
                await usage._fetch_muse_native("muse", {})
        else:
            assert (await usage._fetch_muse_native("muse", {}))["quota_status"] == expected


# ---- cache / stream / fetch interplay

async def test_failed_refresh_keeps_fresh_complete_observation_but_not_as_current_headroom():
    aid = uuid4()
    seed(aid, five=10, week=20, age=120)
    with patch.object(usage, "_fetch_muse", AsyncMock(return_value=usage.Usage(login="ok", error="Muse /usage unavailable"))):
        result = await usage.usage_for(aid, "muse", None, refresh=True)
    assert [w.percent for w in result.windows] == [10, 20]
    # A refresh that failed is not "current": exhaustion still blocks, spare capacity is unconfirmed.
    assert not usage.muse_quota_current(result, time.time())
    assert profiles._usage_headroom(muse_profile(aid)) is None


async def test_stream_observation_newer_than_failed_fetch_wins_the_race():
    aid = uuid4()
    now = time.time()
    older = usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(now - 60)),
                                 usage.Window("seven_day", "7d", 33, int(now + 86400))],
                        source="muse_cli", updated_at=now - 36000, login="ok")
    usage.remember_observation(aid, older.as_dict())

    async def fetch_while_run_streams(_cfg):
        # A Muse run reports a fresh account-wide window while /usage is still being read and then fails.
        fresh = usage.Usage(windows=[usage.Window("five_hour", "5h", 0, int(now + 17000)),
                                     usage.Window("seven_day", "7d", 33, int(now + 86400))],
                            source="muse_cli", updated_at=now, login="ok")
        usage.remember_observation(aid, fresh.as_dict())
        return usage.Usage(login="ok", error="Muse /usage unavailable")

    with patch.object(usage, "_fetch_muse", fetch_while_run_streams):
        result = await usage.usage_for(aid, "muse", None, refresh=True)
    assert [w.percent for w in result.windows] == [0, 33], "the newer observation must not be discarded"
    assert result.updated_at == pytest.approx(now)


async def test_older_replayed_observation_never_overwrites_a_newer_one():
    aid = uuid4()
    now = time.time()
    newer = usage.Usage(windows=[usage.Window("five_hour", "5h", 3, int(now + 3600))], source="muse_cli", updated_at=now)
    older = usage.Usage(windows=[usage.Window("five_hour", "5h", 90, int(now + 3600))], source="muse_cli", updated_at=now - 600)
    usage.remember_observation(aid, newer.as_dict())
    usage.remember_observation(aid, older.as_dict())
    assert usage.cached(aid).windows[0].percent == 3


async def test_429_exhaustion_recovers_after_reset_when_provider_reports_new_window():
    aid = uuid4()
    now = time.time()
    exhausted = usage.Usage(windows=[usage.Window("five_hour", "5h", 100, int(now - 30)),
                                     usage.Window("seven_day", "7d", 33, int(now + 86400))],
                            source="muse_cli", updated_at=now - 3600, login="ok")
    usage.remember_observation(aid, exhausted.as_dict())
    profile = muse_profile(aid)
    # Reset passed: the stale 100% no longer blocks and is not confirmed capacity either.
    assert profiles.usage_limit_until(profile, now) is None
    assert profiles._usage_headroom(profile, now) is None
    recovered = usage.Usage(windows=[usage.Window("five_hour", "5h", 0, int(now + 17000)),
                                     usage.Window("seven_day", "7d", 33, int(now + 86400))],
                            source="muse_cli", updated_at=now, login="ok")
    with patch.object(usage, "_fetch_muse", AsyncMock(return_value=recovered)):
        result = await usage.usage_for(aid, "muse", None, refresh=True)
    assert usage.muse_quota_current(result, time.time())
    assert profiles._usage_headroom(profile)[0] == 67  # weekly 33% is the tighter window


def test_exhaustion_before_reset_keeps_blocking_even_when_observation_is_old():
    aid = uuid4()
    seed(aid, five=100, five_reset=1800, age=4 * 3600, error="/usage unavailable")
    assert profiles.usage_limit_until(muse_profile(aid)) > time.time()
    assert not profiles.muse_probe_eligible(muse_profile(aid)), "exploration must not bypass a known exhaustion"


# ---- exploration eligibility and rate limit

def test_exploration_eligibility_matrix():
    now = time.time()
    unknown = uuid4()
    seed(unknown, age=3600)
    assert profiles.muse_probe_eligible(muse_profile(unknown), now)
    known = uuid4()
    seed(known, age=30)
    assert not profiles.muse_probe_eligible(muse_profile(known), now), "confirmed headroom ranks normally"
    never_seen = uuid4()
    assert profiles.muse_probe_eligible(muse_profile(never_seen), now)
    logged_out = uuid4()
    seed(logged_out, age=3600, login="missing")
    assert not profiles.muse_probe_eligible(muse_profile(logged_out), now)
    cooling = uuid4()
    seed(cooling, age=3600)
    profiles._cooldown[cooling] = now + 600
    assert not profiles.muse_probe_eligible(muse_profile(cooling), now)
    assert not profiles.muse_probe_eligible(profiles.Profile(unknown, "c", "claude", model="sonnet"), now)


def _codex(percent=10):
    p = profiles.Profile(uuid4(), "other", "codex", model="gpt-6-luna")
    usage.seed(p.id, usage.Usage(windows=[usage.Window("five_hour", "5h", percent, int(time.time() + 3600))],
                                 source="api", updated_at=time.time()), refresh=True)
    return p


def test_choose_gives_an_exploration_reservation_to_the_unconfirmed_muse_only():
    aid = uuid4()
    seed(aid, age=3600)
    pool = [_codex(), muse_profile(aid)]
    # Without a DB reservation (probe_ids) an unconfirmed Muse keeps ranking behind measured accounts.
    assert profiles.choose(pool, uuid4(), balance_usage=True).engine == "codex"
    assert profiles.choose(pool, uuid4(), balance_usage=True, probe_ids={aid}).engine == "muse"


def test_reservation_is_ignored_without_usage_balancing():
    aid = uuid4()
    seed(aid, age=3600)
    assert profiles.choose([_codex(), muse_profile(aid)], uuid4(), probe_ids={aid}).engine == "codex"


def test_confirmed_muse_competes_on_headroom_not_exploration():
    aid = uuid4()
    seed(aid, five=70, age=10)  # 30 left < codex 90 left
    pool = [_codex(10), muse_profile(aid)]
    assert profiles.choose(pool, uuid4(), balance_usage=True, probe_ids={aid}).engine == "codex"


def test_stale_observation_is_never_reported_as_spare_capacity():
    aid = uuid4()
    seed(aid, five=0, week=0, age=3600)
    assert profiles._usage_headroom(muse_profile(aid)) is None
    view = account_status._usage(aid, time.time(), engine="muse")
    assert view["stale"] is True  # routing and caps ignore a stale value; the flag tells consumers
    assert account_status._cap_remaining(aid, 100, view, engine="muse") is None


def test_first_stage_keeps_unconfirmed_muse_only_while_it_holds_a_reservation():
    aid, agy_id = uuid4(), uuid4()
    seed(aid, age=3600)
    agy = profiles.Profile(agy_id, "agy", "antigravity", model="claude-sonnet-5-5-high")
    usage.seed(agy_id, usage.Usage(
        windows=[usage.Window(f"agy_claude_and_gpt_models_{s}", "g", 20, int(time.time() + 3600), 80.0) for s in ("5h", "weekly")],
        source="antigravity_cli", updated_at=time.time()), refresh=True)
    pool = [agy, muse_profile(aid)]
    assert {p.engine for p in worker_routing.first_stage(pool, "general", {aid})} == {"antigravity", "muse"}
    assert {p.engine for p in worker_routing.first_stage(pool, "general")} == {"antigravity"}


def test_capacity_reports_exploration_eligibility_without_claiming_known_quota():
    aid = uuid4()
    seed(aid, age=3600)
    cap = worker_routing.capacity(muse_profile(aid), 0)
    assert cap["quota_known"] is False and cap["exploration_eligible"] is True
    assert cap["headroom_per_run_percent"] is None
