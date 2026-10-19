"""Muse 는 쓴 뒤에야 사용량을 준다. 안 쓰는 켜진 계정은 'hi' 핑 한 번으로 관측을 받아 배제에서 풀린다(2026-10-19 사용자 지시).

경계: 계정당 30분 한 번(탐색 배정과 같은 DB 예약), 꺼진 계정·실행 중인 계정·알려진 한도는 핑하지 않는다.
실패는 오류 원문 그대로 붙고 여유 있는 값으로 바뀌지 않는다.
"""
import time
import uuid

import pytest

from ttakkari.db import sessionmaker
from ttakkari.models import EngineAccount
from ttakkari.runs import profiles, usage

MUSE = "muse-spark-1.3"
NO_USAGE = "Muse 로그인 확인 · /usage 구독 사용량 미관측: unavailable"


async def make_muse(*, enabled=True, work_enabled=False, pool="personal") -> EngineAccount:
    row = EngineAccount(name=f"muse-{uuid.uuid4().hex[:6]}", engine="muse", config_dir=None, enabled=enabled, priority=0,
                        pool=pool, work_enabled=work_enabled)
    async with sessionmaker()() as db:
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


def observed() -> usage.Usage:
    return usage.Usage(windows=[usage.Window("five_hour", "5시간", 5, int(time.time()) + 3600),
                                usage.Window("seven_day", "주간", 1, int(time.time()) + 86400)],
                       source="muse_cli", updated_at=time.time(), login="ok")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    usage.clear_cache()
    profiles.reset_probes()
    calls = {"probe": 0, "ping": 0}

    async def idle_probe(_config_dir):
        calls["probe"] += 1
        return usage.Usage(error=NO_USAGE, login="ok")

    monkeypatch.setattr(usage, "MUSE_PING", True)
    # 성공한 관측은 DB 저장 작업(usage_store)을 띄운다. 이 시험은 저장을 보지 않으니 끄고, 끝난 시험이 저장 작업을 남기지 않게 한다.
    monkeypatch.setattr(usage, "_sink", None)
    monkeypatch.setattr(usage, "_fetch_muse", idle_probe)
    monkeypatch.setattr(usage, "_login_refresh_at", {})
    return calls


@pytest.fixture
def pings(monkeypatch):
    calls = []

    async def ping_ok(profile):
        calls.append(profile)
        return observed()

    monkeypatch.setattr(usage, "_run_muse_ping", ping_ok)
    return calls


async def test_idle_muse_gets_one_ping_and_its_observation(client, auth, pings):
    row = await make_muse()

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert len(pings) == 1 and pings[0].model == MUSE
    assert u.error is None and [(w.key, w.percent) for w in u.windows] == [("five_hour", 5), ("seven_day", 1)]
    assert usage.cached(row.id).windows[0].percent == 5


async def test_ping_is_rate_limited_to_once_per_interval(client, auth, pings):
    row = await make_muse()
    await usage.usage_for(row.id, "muse", None, refresh=True)
    usage.clear_cache()

    again = await usage.usage_for(row.id, "muse", None, refresh=True)  # 새로고침을 연달아 눌러도 두 번째 핑은 없다
    third = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert len(pings) == 1
    assert not again.windows and NO_USAGE in again.error and "30분에 한 번" in again.error
    assert "30분에 한 번" in third.error


async def test_failed_ping_returns_the_error_and_never_looks_spare(client, auth, monkeypatch):
    row = await make_muse()

    async def broken(profile):
        raise RuntimeError("Muse sandbox is unavailable in this execution environment")

    monkeypatch.setattr(usage, "_run_muse_ping", broken)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert not u.windows and u.source is None
    assert NO_USAGE in u.error and "Muse 핑 실패(RuntimeError: Muse sandbox is unavailable" in u.error
    assert profiles._usage_headroom(profiles.Profile(row.id, "m", "muse", model=MUSE)) is None


async def test_ping_without_observation_reports_why(client, auth, monkeypatch):
    row = await make_muse()

    async def silent(profile):
        return "Muse 핑 뒤에도 구독 사용량이 관측되지 않았어요(종료 코드 1)"

    monkeypatch.setattr(usage, "_run_muse_ping", silent)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert not u.windows and "핑 뒤에도 구독 사용량이 관측되지 않았어요" in u.error


async def test_disabled_account_is_not_pinged(client, auth, pings):
    row = await make_muse(enabled=False)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert pings == [] and "꺼진 계정이라 핑을 보내지 않아요" in u.error


async def test_work_mode_account_counts_as_enabled(client, auth, pings):
    row = await make_muse(enabled=False, work_enabled=True, pool="work")

    await usage.usage_for(row.id, "muse", None, refresh=True)

    assert len(pings) == 1


async def test_account_running_a_run_is_not_pinged(client, auth, pings, monkeypatch):
    row = await make_muse()

    async def busy():
        return {row.id: 1}

    monkeypatch.setattr(profiles, "running_counts_async", busy)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert pings == [] and "실행 중인 Run" in u.error


async def test_known_limit_is_not_pinged(client, auth, pings):
    row = await make_muse()
    profiles._cooldown[row.id] = time.time() + 1800

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert pings == [] and "핑을 보내지 않아요" in u.error
    profiles._cooldown.pop(row.id, None)


async def test_logged_out_account_is_not_pinged(client, auth, pings, monkeypatch):
    row = await make_muse()

    async def logged_out(_config_dir):
        return usage.Usage(error="Muse 구독 로그인이 필요해요", login="missing")

    monkeypatch.setattr(usage, "_fetch_muse", logged_out)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert pings == [] and u.error == "Muse 구독 로그인이 필요해요"


async def test_provider_observation_is_used_without_any_ping(client, auth, pings, monkeypatch):
    row = await make_muse()

    async def has_values(_config_dir):
        return observed()

    monkeypatch.setattr(usage, "_fetch_muse", has_values)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert pings == [] and u.windows[0].percent == 5


async def test_collector_tick_pings_the_idle_account_once(client, auth, pings, monkeypatch):
    """수집기가 주기마다 읽어도 30분 안에는 핑이 한 번이다(무한루프·연속핑 없음)."""
    from ttakkari.runs import usage_collector as col

    row = await make_muse()

    async def fetch(account_id, engine, config_dir):
        if account_id != row.id:  # 시험 DB 의 기본 계정은 실제 공급자를 부르지 않는다
            return usage.Usage(error="skipped")
        return await usage.usage_for(account_id, engine, config_dir, refresh=True)

    collector = col.Collector(fetch)
    now = time.time()
    for step in range(4):
        collector._state.setdefault(row.id, col._State(next_due=0))
        collector._state[row.id].next_due = 0
        usage.clear_cache()
        await collector.tick(now + step * 60)

    assert len(pings) == 1


async def test_effort_of_the_ping_launch_is_pinned_like_every_muse_launch(client, auth, monkeypatch, tmp_path):
    """핑도 Run 과 같은 어댑터를 쓰므로 생각 수준은 max 다."""
    import json
    from pathlib import Path

    from ttakkari.agents.base import LaunchSpec
    from ttakkari.agents.muse import MuseAdapter

    adapter = MuseAdapter()
    spec = LaunchSpec(cwd=tmp_path, prompt="hi", engine_session_id=None, model=MUSE, effort=None, outbox=tmp_path,
                      write_roots=[], system_note="x")
    cmd = adapter.build_command(spec, {"XDG_CONFIG_HOME": str(tmp_path / "cfg")})
    try:
        data = json.loads(Path(cmd[-1]).read_text())
        assert data["effort"] == "max" and data["write_roots"] == [] and data["mcp_servers"] == {}
    finally:
        adapter.cleanup(spec)



async def test_delivered_ping_is_followed_by_one_more_provider_read(client, auth, monkeypatch):
    """스트림에 관측이 없어도 핑이 전달됐으면 공급자의 사용량 조회가 값을 줄 수 있다."""
    row = await make_muse()
    reads = []

    async def probe(_config_dir):
        reads.append(1)  # 첫 조회는 비어 있고(핑 전), 핑 뒤 조회는 값을 준다
        return usage.Usage(error=NO_USAGE, login="ok") if len(reads) == 1 else observed()

    async def delivered(profile):
        return usage.Usage(login="ok")

    monkeypatch.setattr(usage, "_fetch_muse", probe)
    monkeypatch.setattr(usage, "_run_muse_ping", delivered)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert len(reads) == 2 and u.error is None and u.windows[0].percent == 5


async def test_delivered_ping_without_any_observation_says_so(client, auth, monkeypatch):
    row = await make_muse()

    async def delivered(profile):
        return usage.Usage(login="ok")

    monkeypatch.setattr(usage, "_run_muse_ping", delivered)

    u = await usage.usage_for(row.id, "muse", None, refresh=True)

    assert not u.windows and "핑은 전달됐지만 공급자가 아직 구독 사용량을 주지 않았어요" in u.error
    assert profiles._usage_headroom(profiles.Profile(row.id, "m", "muse", model=MUSE)) is None


def _fake_bridge(monkeypatch, tmp_path, lines: list[dict], then_sleep: float = 0.0):
    """실제 Muse 대신 stdout 에 줄을 내보내는 프로세스. 어댑터의 명령 구성만 바꾼다."""
    import json
    import sys

    from ttakkari.agents.muse import MuseAdapter

    script = tmp_path / "bridge.py"
    script.write_text("import sys, time\n" + "".join(f"print({json.dumps(json.dumps(x))}, flush=True)\n" for x in lines)
                      + f"time.sleep({then_sleep})\n")
    monkeypatch.setattr(MuseAdapter, "build_command", lambda self, spec, env: [sys.executable, str(script)])


def _profile():
    return profiles.Profile(uuid.uuid4(), "m", "muse", model=MUSE)


async def test_ping_stops_waiting_once_the_reply_arrived(monkeypatch, tmp_path):
    """답(turn/foregroundCompleted)이 오면 모델 호출은 끝났다. 수십 초 걸리는 정리 단계를 기다리지 않고 종료한다."""
    _fake_bridge(monkeypatch, tmp_path, [{"method": "muse/sessionReady", "params": {"sessionId": "s", "modelId": MUSE}},
                                         {"method": "turn/foregroundCompleted", "params": {"sessionId": "s"}}], then_sleep=60)
    started = time.time()

    r = await usage._run_muse_ping(_profile())

    assert time.time() - started < 10
    assert isinstance(r, usage.Usage) and not r.windows


async def test_ping_returns_the_stream_observation(monkeypatch, tmp_path):
    now_ms = int(time.time() * 1000)
    quota = {"window": {"usedPercent": 7, "resetsAtMs": now_ms + 3_600_000}, "weekly": {"usedPercent": 2, "resetsAtMs": now_ms + 86_400_000},
             "observedAtMs": now_ms, "tier": "Power"}
    _fake_bridge(monkeypatch, tmp_path, [{"method": "usage/changed", "params": quota},
                                         {"method": "turn/foregroundCompleted", "params": {}}])

    r = await usage._run_muse_ping(_profile())

    assert r.source == "muse_cli" and [(w.key, w.percent) for w in r.windows] == [("five_hour", 7), ("seven_day", 2)]


async def test_ping_reports_a_provider_error_verbatim(monkeypatch, tmp_path):
    _fake_bridge(monkeypatch, tmp_path, [{"method": "muse/error", "params": {"message": "API error 429: Subscription quota exhausted"}}])

    r = await usage._run_muse_ping(_profile())

    assert r == "Muse 핑 실패(API error 429: Subscription quota exhausted)"


async def test_ping_that_never_answers_times_out_with_a_reason(monkeypatch, tmp_path):
    _fake_bridge(monkeypatch, tmp_path, [{"method": "muse/sessionReady", "params": {"sessionId": "s"}}], then_sleep=60)
    monkeypatch.setattr(usage, "MUSE_PING_TIMEOUT", 0.5)

    r = await usage._run_muse_ping(_profile())

    assert isinstance(r, str) and "답하지 않았어요" in r
