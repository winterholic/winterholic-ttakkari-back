"""SEC2-LGN-01 S1: 로그인 제한 shadow 관측(docs/security/network-login-session-audit.md 5.5~5.9).

shadow 는 어떤 경우에도 로그인 요청을 거절·지연하지 않고, 같은 시도를 새 설계로 가상 평가해 DB 에 남긴다.
시험은 격리 DB(ttakkari_test)만 쓴다. 시간은 reserve/finish_attempt 의 now 인자로 주입한다.
"""
from __future__ import annotations

import argparse
import asyncio
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from ttakkari.db import dispose_engine, sessionmaker
from ttakkari.models import (
    AppSetting,
    Credential,
    LoginAttempt,
    LoginBucket,
    LoginThrottle,
)
from ttakkari.security import login_throttle as lt
from ttakkari.security.auth import limiter

PASSWORD = "correct-horse-battery"
T0 = datetime(2026, 10, 19, 12, 0, 0, tzinfo=UTC)
SRC = "src:198.51.100.7"


@pytest_asyncio.fixture(autouse=True, loop_scope="session")
async def _isolate():
    lt.reset_state()
    yield
    await lt.drain()
    lt.reset_state()


async def set_mode(mode: str) -> str:
    async with sessionmaker()() as db:
        row = await db.get(AppSetting, lt.MODE_KEY)
        if row is None:
            db.add(AppSetting(key=lt.MODE_KEY, value={"mode": mode}))
        else:
            row.value = {"mode": mode}
        await db.commit()
    return await lt.refresh_mode()


async def attempts() -> list[LoginAttempt]:
    async with sessionmaker()() as db:
        return list((await db.scalars(select(LoginAttempt).order_by(LoginAttempt.reserved_at, LoginAttempt.id))).all())


async def row(key: str = SRC) -> LoginThrottle | None:
    async with sessionmaker()() as db:
        return await db.get(LoginThrottle, key)


async def bucket(key: str = lt.ACCT_KEY) -> LoginBucket | None:
    async with sessionmaker()() as db:
        return await db.get(LoginBucket, key)


async def refill_bucket(tokens: float = lt.BUCKET_CAP, at: datetime | None = None) -> None:
    async with sessionmaker()() as db:
        b = await db.get(LoginBucket, lt.ACCT_KEY)
        if b is None:
            db.add(LoginBucket(key=lt.ACCT_KEY, tokens=tokens, refilled_at=at or T0, blocked_count=0))
        else:
            b.tokens = tokens
            if at is not None:
                b.refilled_at = at
        await db.commit()


async def table_counts() -> tuple[int, int, int]:
    async with sessionmaker()() as db:
        return (
            await db.scalar(select(func.count()).select_from(LoginThrottle)) or 0,
            await db.scalar(select(func.count()).select_from(LoginBucket)) or 0,
            await db.scalar(select(func.count()).select_from(LoginAttempt)) or 0,
        )


async def post_login(client, password: str):
    return await client.post("/api/auth/login", json={"password": password})


# ------------------------------------------------------------ 모드 / 기본값 off

async def test_default_off_writes_nothing(client):
    assert lt.current_mode() == "off"
    r1 = await post_login(client, "wrong-password-xxxx")
    r2 = await post_login(client, PASSWORD)
    assert (r1.status_code, r2.status_code) == (401, 200)
    await lt.drain()
    assert await table_counts() == (0, 0, 0)


@pytest.mark.parametrize("raw", ["enforce_source", "enforce", "ENFORCE", "weird", ""])
async def test_unimplemented_or_unknown_modes_are_treated_as_off(client, raw, caplog):
    assert await set_mode(raw) == "off"
    assert lt.begin("203.0.113.9") is None
    await post_login(client, "wrong-password-xxxx")
    await lt.drain()
    assert await table_counts() == (0, 0, 0)


async def test_cli_rejects_enforce_modes_and_accepts_shadow_off():
    from ttakkari.cli import _cmd_login_throttle_mode
    assert _cmd_login_throttle_mode(argparse.Namespace(mode="enforce")) == 2
    assert _cmd_login_throttle_mode(argparse.Namespace(mode="enforce_source")) == 2
    # 쓰기 경로는 새 프로세스로 확인한다(CLI 가 자기 이벤트 루프·엔진을 만든다). env 는 시험 DB 를 가리킨다.
    async def run_cli(*args: str) -> tuple[int, str, str]:
        proc = await asyncio.create_subprocess_exec(sys.executable, "-m", "ttakkari.cli", *args,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), 60)
        return proc.returncode, out.decode(), err.decode()

    code, out, err = await run_cli("login-throttle-mode", "shadow")
    assert code == 0, err
    assert "shadow" in out
    assert await lt.refresh_mode() == "shadow"
    code, out, _ = await run_cli("login-throttle-mode")
    assert code == 0 and out.strip() == "shadow"
    code, _, err = await run_cli("login-throttle-mode", "enforce")
    assert code == 2 and "구현되지 않았습니다" in err
    assert await lt.refresh_mode() == "shadow"  # 거부된 값은 저장되지 않는다


async def test_mode_cache_is_five_seconds_and_current_mode_never_does_io(monkeypatch):
    await set_mode("shadow")
    assert lt.current_mode() == "shadow"  # 캐시 적중
    # DB 를 off 로 바꿔도 캐시 TTL 안에는 이전 값(세대별 5초 캐시)
    async with sessionmaker()() as db:
        row_ = await db.get(AppSetting, lt.MODE_KEY)
        row_.value = {"mode": "off"}
        await db.commit()
    assert lt.current_mode() == "shadow"
    base = lt._mode_cache[0]
    monkeypatch.setattr(lt.time, "monotonic", lambda: base + lt.MODE_TTL_SECONDS + 0.1)
    assert lt.current_mode() == "shadow"  # 만료 직후 첫 호출은 이전 값을 돌려주고 백그라운드 갱신만 시작
    await lt.drain()
    assert lt.current_mode() == "off"
    # 롤백 확인 기준(5.8.2): off 이후에는 새 시도 행이 생기지 않는다.
    assert lt.begin("203.0.113.9") is None
    await lt.drain()
    assert await table_counts() == (0, 0, 0)


def test_source_key_groups_ipv6_by_64_and_maps_ipv4():
    assert lt.source_key("198.51.100.7") == "src:198.51.100.7"
    a = lt.source_key("2001:db8:1:2:aaaa:bbbb:cccc:dddd")
    b = lt.source_key("2001:db8:1:2:1111:2222:3333:4444")
    assert a == b == "src:2001:db8:1:2::/64"
    assert lt.source_key("2001:db8:1:3::1") != a
    assert lt.source_key("::ffff:198.51.100.7") == "src:198.51.100.7"
    assert lt.source_key(None) == lt.source_key("unknown") == lt.source_key("not-an-ip") == "src:unknown"


# ------------------------------------------------------------ 단계 상태 기계(시간 주입)

async def test_stage_sequence_5_then_1_min_then_probes_5_15_60_cap_and_quiet_reset():
    for i in range(5):
        r = await lt.reserve(SRC, now=T0 + timedelta(seconds=i))
        assert r.would_block is None and r.charged_src and r.charged_acct
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=i, milliseconds=50))
    t_lock = T0 + timedelta(seconds=4)
    s = await row()
    assert (s.budget, s.level) == (0, 1) and s.locked_until == t_lock + timedelta(seconds=60)

    # 잠금 중 요청은 어떤 결정 상태도 바꾸지 않는다(불변식 1)
    before = (s.budget, s.level, s.locked_until, s.last_failure_at, s.lock_trigger)
    for k in range(1, 40):
        r = await lt.reserve(SRC, now=T0 + timedelta(seconds=10 + k))
        assert r.would_block == "src_locked" and not r.charged_src and not r.charged_acct
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=10 + k))
    s = await row()
    assert (s.budget, s.level, s.locked_until, s.last_failure_at, s.lock_trigger) == before

    # 해제 후 1회 탐침 → 5분 → 15분 → 60분 → 60분(상한)
    now = t_lock + timedelta(seconds=61)
    for expected_level, expected_len in ((2, 300), (3, 900), (4, 3600), (4, 3600)):
        await refill_bucket(at=now)
        r = await lt.reserve(SRC, now=now)
        assert r.would_block is None, (expected_level, r)
        await lt.finish_attempt(r, False, now=now)
        s = await row()
        assert s.level == expected_level and s.locked_until == now + timedelta(seconds=expected_len)
        now = s.locked_until + timedelta(seconds=1)

    # 24시간 무실패면 처음 단계로
    await refill_bucket(at=now + timedelta(hours=25))
    r = await lt.reserve(SRC, now=now + timedelta(hours=25))
    assert r.would_block is None
    s = await row()
    assert (s.level, s.budget) == (0, lt.BUDGET - 1)


async def test_success_refunds_only_own_charge_and_resets_only_when_clean():
    # 시나리오 C: 앞선 실패 3건이 모두 끝난 뒤의 성공(순차 이력) → 전체 초기화
    for i in range(3):
        r = await lt.reserve(SRC, now=T0 + timedelta(seconds=i))
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=i))
    ok = await lt.reserve(SRC, now=T0 + timedelta(seconds=10))
    await lt.finish_attempt(ok, True, now=T0 + timedelta(seconds=11))
    s = await row()
    assert (s.budget, s.level, s.locked_until) == (lt.BUDGET, 0, None)
    b = await bucket()
    assert b.tokens == pytest.approx(lt.BUCKET_CAP - 3, abs=0.1)  # 실패 3건 소비는 남고 성공 1건은 자기 몫만 환불


async def test_success_while_others_pending_keeps_their_consumption_and_trigger_rule():
    await refill_bucket(at=T0)
    rs = [await lt.reserve(SRC, now=T0 + timedelta(seconds=i)) for i in range(5)]
    assert [r.would_block for r in rs] == [None] * 5
    s = await row()
    assert s.budget == 0 and s.lock_trigger == rs[4].id and s.level == 1

    # 시나리오 A: 트리거가 아닌 정답이 먼저 끝남(나머지 pending) → 자기 몫만 환불, 잠금·단계 유지
    await lt.finish_attempt(rs[1], True, now=T0 + timedelta(seconds=6))
    s = await row()
    assert (s.budget, s.level) == (1, 1) and s.locked_until is not None
    for r in (rs[0], rs[2], rs[3], rs[4]):
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=7))
    s = await row()
    assert s.budget == 1 and s.locked_until is not None  # 실패 4건의 소비가 지워지지 않았다


async def test_trigger_attempt_success_unlocks_but_keeps_pending_consumption():
    # 시나리오 B: 정답이 5번째(잠금 트리거). 나머지 4건이 pending 이면 전체 초기화 없이 잠금만 풀린다.
    rs = [await lt.reserve(SRC, now=T0 + timedelta(seconds=i)) for i in range(5)]
    await lt.finish_attempt(rs[4], True, now=T0 + timedelta(seconds=6))
    s = await row()
    assert (s.locked_until, s.level, s.budget) == (None, 0, 1)
    for r in rs[:4]:
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=7))
    s = await row()
    assert s.budget == 1


async def test_success_with_later_reserved_attempt_does_not_reset():
    # 시나리오 D: 정답이 먼저 예약되고 나중 예약(오답)이 pending 인 채 정답이 끝나면 초기화하지 않는다.
    good = await lt.reserve(SRC, now=T0)
    bad = await lt.reserve(SRC, now=T0 + timedelta(seconds=1))
    await lt.finish_attempt(good, True, now=T0 + timedelta(seconds=2))
    s = await row()
    assert s.budget == lt.BUDGET - 1  # bad 의 소비가 남음
    await lt.finish_attempt(bad, False, now=T0 + timedelta(seconds=3))
    assert (await row()).budget == lt.BUDGET - 1


# ------------------------------------------------------------ 병렬·원자성

async def test_parallel_20_same_source_exactly_budget_admitted():
    await refill_bucket(at=T0)
    rs = await asyncio.gather(*(lt.reserve(SRC, now=T0) for _ in range(20)))
    admitted = [r for r in rs if r.would_block is None]
    blocked = [r for r in rs if r.would_block is not None]
    assert len(admitted) == lt.BUDGET == 5 and len(blocked) == 15
    assert {r.would_block for r in blocked} == {"src_locked"}
    s = await row()
    assert s.budget == 0 and s.level == 1
    assert (await bucket()).tokens == pytest.approx(lt.BUCKET_CAP - 5)
    assert lt._blocked[SRC] == 15
    rows = await attempts()
    assert sum(1 for a in rows if a.charged_src) == 5 and len(rows) == 20


async def test_parallel_distinct_sources_bounded_by_account_bucket_and_no_row_without_token():
    await refill_bucket(at=T0)
    keys = [f"src:203.0.113.{i}" for i in range(1, 21)]
    rs = await asyncio.gather(*(lt.reserve(k, now=T0) for k in keys))
    assert sum(1 for r in rs if r.would_block is None) == int(lt.BUCKET_CAP) == 6
    assert {r.would_block for r in rs if r.would_block} == {"acct_bucket_empty"}
    async with sessionmaker()() as db:
        n_rows = await db.scalar(select(func.count()).select_from(LoginThrottle))
    assert n_rows == 6  # 토큰이 없을 때는 출처 행을 만들지 않는다


@pytest.mark.parametrize("seed", range(12))
async def test_parallel_one_correct_among_wrong_shuffled_keeps_invariants(seed):
    key = f"src:192.0.2.{seed + 1}"
    await refill_bucket(at=T0)
    rs = await asyncio.gather(*(lt.reserve(key, now=T0 + timedelta(milliseconds=i)) for i in range(20)))
    admitted = [r for r in rs if r.would_block is None]
    assert len(admitted) == 5
    rng = random.Random(seed)
    good = rng.choice(admitted)
    order = admitted[:]
    rng.shuffle(order)
    for r in order:
        await lt.finish_attempt(r, r is good, now=T0 + timedelta(seconds=1))
    s = await row(key)
    assert 0 <= s.budget <= lt.BUDGET
    outcomes = {a.id: a.outcome for a in await attempts() if a.id in {r.id for r in admitted}}
    assert sorted(outcomes.values()) == ["fail"] * 4 + ["ok"]
    # 정답은 자기 몫만 환불하므로, 전체 초기화가 없었다면 budget = 5 - (실패 수)
    if s.budget != lt.BUDGET:
        assert s.budget == lt.BUDGET - 4


async def test_restart_keeps_state_and_counters_after_flush(monkeypatch):
    await refill_bucket(at=T0)
    rs = [await lt.reserve(SRC, now=T0 + timedelta(seconds=i)) for i in range(5)]
    for r in rs:
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=9))
    blocked = [await lt.reserve(SRC, now=T0 + timedelta(seconds=20 + i)) for i in range(3)]
    assert all(r.would_block == "src_locked" for r in blocked)
    assert await lt.flush_counters() == 3
    # "재시작": 프로세스 메모리와 DB 연결을 모두 버린다
    lt.reset_state()
    await dispose_engine()
    s = await row()
    assert (s.budget, s.level, s.blocked_count) == (0, 1, 3)
    assert s.locked_until == T0 + timedelta(seconds=4 + 60)
    again = await lt.reserve(SRC, now=T0 + timedelta(seconds=30))
    assert again.would_block == "src_locked"


async def test_blocked_counter_loss_on_restart_is_bounded_by_flush_interval():
    await refill_bucket(at=T0)
    for i in range(5):
        r = await lt.reserve(SRC, now=T0 + timedelta(seconds=i))
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=i))
    for i in range(4):
        await lt.reserve(SRC, now=T0 + timedelta(seconds=10 + i))
    assert await lt.flush_counters() == 4  # flush 주기 도달
    for i in range(2):
        await lt.reserve(SRC, now=T0 + timedelta(seconds=20 + i))
    lt.reset_state()  # flush 전에 재시작 → 마지막 주기 몫(2)만 잃는다
    assert await lt.flush_counters() == 0
    assert (await row()).blocked_count == 4


async def test_acct_bucket_empty_counts_on_bucket_and_no_state_written():
    await refill_bucket(tokens=0.0, at=T0)
    r = await lt.reserve("src:203.0.113.50", now=T0 + timedelta(seconds=5))
    assert r.would_block == "acct_bucket_empty" and not r.charged_acct and not r.charged_src
    assert await row("src:203.0.113.50") is None
    assert await lt.flush_counters() == 1
    assert (await bucket()).blocked_count == 1
    # refill: 10분에 1개, 상한 6
    await lt.reserve("src:203.0.113.51", now=T0 + timedelta(seconds=605))
    assert (await bucket()).tokens == pytest.approx(605 / 600 - 1, abs=0.01)
    await refill_bucket(tokens=0.0, at=T0)
    r2 = await lt.reserve("src:203.0.113.52", now=T0 + timedelta(hours=5))
    assert r2.would_block is None
    assert (await bucket()).tokens == pytest.approx(lt.BUCKET_CAP - 1, abs=0.01)


# ------------------------------------------------------------ 출처 행 상한(src:overflow 폐지)

async def seed_locked_rows(n: int, now: datetime, prefix: str = "src:10.") -> None:
    async with sessionmaker()() as db:
        await db.execute(text(
            "INSERT INTO login_throttle (key, kind, budget, level, locked_until, stage_started_at, blocked_count, updated_at) "
            "SELECT :prefix || g, 'src', 0, 1, :locked, :now, 0, :now FROM generate_series(1, :n) g"),
            {"prefix": prefix, "locked": now + timedelta(hours=1), "now": now, "n": n})
        await db.commit()


async def test_row_cap_20000_all_locked_new_source_is_not_blocked_and_no_shared_key():
    await refill_bucket(at=T0)
    await seed_locked_rows(lt.SRC_ROW_CAP, T0)
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(LoginThrottle)) == 20_000
    r = await lt.reserve("src:203.0.113.77", now=T0 + timedelta(seconds=1))
    assert r.would_block is None and r.charged_acct and not r.charged_src  # 개별 키 없이 acct:u 만 적용
    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(LoginThrottle)) == 20_000
        assert await db.get(LoginThrottle, "src:overflow") is None
        assert await db.get(LoginThrottle, "src:203.0.113.77") is None
    # 이미 잠긴 출처는 그대로 잠금, 무관한 두 번째 새 출처도 막히지 않는다
    locked = await lt.reserve("src:10.5", now=T0 + timedelta(seconds=2))
    assert locked.would_block == "src_locked"
    other = await lt.reserve("src:203.0.113.78", now=T0 + timedelta(seconds=3))
    assert other.would_block is None
    await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=4))  # 개별 키 없는 시도의 확정도 안전


async def test_row_cap_evicts_oldest_unlocked_first_and_keeps_locked(monkeypatch):
    monkeypatch.setattr(lt, "SRC_ROW_CAP", 5)
    await refill_bucket(at=T0)
    await seed_locked_rows(4, T0)
    async with sessionmaker()() as db:  # 5번째는 잠기지 않은 오래된 행
        db.add(LoginThrottle(key="src:10.old", kind="src", budget=5, level=0, stage_started_at=T0 - timedelta(days=1),
                             updated_at=T0 - timedelta(days=1)))
        await db.commit()
    r = await lt.reserve("src:203.0.113.90", now=T0 + timedelta(seconds=1))
    assert r.would_block is None and r.charged_src
    async with sessionmaker()() as db:
        assert await db.get(LoginThrottle, "src:10.old") is None
        assert await db.get(LoginThrottle, "src:203.0.113.90") is not None
        assert await db.scalar(select(func.count()).select_from(LoginThrottle).where(LoginThrottle.level == 1)) == 4


async def test_sweep_abandons_pending_prunes_old_attempts_and_idle_rows():
    r = await lt.reserve(SRC, now=T0)
    async with sessionmaker()() as db:
        db.add(LoginThrottle(key="src:10.idle", kind="src", budget=5, level=0, stage_started_at=T0 - timedelta(days=9),
                             updated_at=T0 - timedelta(days=9)))
        db.add(LoginThrottle(key="src:10.hot", kind="src", budget=0, level=2, stage_started_at=T0 - timedelta(days=9),
                             updated_at=T0 - timedelta(days=9)))
        db.add(LoginAttempt(id=uuid.uuid4(), lane="U", src_key=SRC, reserved_at=T0 - timedelta(hours=30), outcome="ok"))
        await db.commit()
    res = await lt.sweep(now=T0 + timedelta(minutes=3))
    assert res == {"abandoned": 1, "attempts_deleted": 1, "rows_deleted": 1}
    assert {a.id: a.outcome for a in await attempts()}[r.id] == "abandoned"
    assert await row("src:10.idle") is None and await row("src:10.hot") is not None
    # 이미 확정된 예약을 다시 확정해도 무해(멱등)
    await lt.finish_attempt(r, True, now=T0 + timedelta(minutes=4))
    assert {a.id: a.outcome for a in await attempts()}[r.id] == "abandoned"


async def test_reset_decision_state_clears_locks_but_keeps_history_and_counters():
    await refill_bucket(at=T0)
    for i in range(5):
        r = await lt.reserve(SRC, now=T0 + timedelta(seconds=i))
        await lt.finish_attempt(r, False, now=T0 + timedelta(seconds=i))
    await lt.reserve(SRC, now=T0 + timedelta(seconds=10))
    await lt.flush_counters()
    n_attempts = len(await attempts())
    res = await lt.reset_decision_state()
    assert res["throttle_rows"] == 1 and res["buckets"] == 1
    s = await row()
    assert (s.budget, s.level, s.locked_until, s.lock_trigger) == (lt.BUDGET, 0, None, None)
    assert s.blocked_count == 1 and len(await attempts()) == n_attempts
    assert (await bucket()).tokens == lt.BUCKET_CAP


# ------------------------------------------------------------ HTTP: shadow 는 거절·지연·응답 변화가 없다

async def run_sequence(client) -> list[int]:
    limiter.reset()
    out = []
    for i in range(6):
        out.append((await post_login(client, f"wrong-password-{i}x")).status_code)
    return out


async def test_shadow_never_rejects_and_matches_legacy_behaviour(client):
    off = await run_sequence(client)
    await set_mode("shadow")
    shadow = await run_sequence(client)
    await lt.drain()
    assert off == shadow == [401, 401, 401, 401, 401, 429]  # 기존 메모리·IP 제한 그대로
    rows = await attempts()
    assert [a.outcome for a in rows] == ["fail"] * 5 and all(a.would_block is None for a in rows[:5])
    s = await row("src:127.0.0.1")
    assert s is not None and s.level == 1  # 새 설계의 가상 평가는 5번째에서 잠금 시작


async def test_shadow_login_success_refresh_agent_and_db_failure_are_unaffected(client, monkeypatch, new_session):
    from ttakkari.api.auth import COOKIE
    from ttakkari.orchestrator.runtime import issue_token
    await set_mode("shadow")
    _, session_info = await new_session()
    agent_headers = {"Authorization": f"Bearer {issue_token(session_info['id'])}"}

    ok = await post_login(client, PASSWORD)
    assert ok.status_code == 200 and ok.json()["access_token"]
    cookie = ok.cookies.get(COOKIE)
    assert cookie
    me = await client.get("/api/auth/me", headers={"Authorization": f"Bearer {ok.json()['access_token']}"})
    assert me.status_code == 200
    refreshed = await client.post("/api/auth/refresh", headers={"Cookie": f"{COOKIE}={cookie}"})
    assert refreshed.status_code == 200
    assert (await client.get("/api/agent/sessions", headers=agent_headers)).status_code == 200
    await lt.drain()
    rows = await attempts()
    assert [a.outcome for a in rows] == ["ok"]

    # shadow 의 DB 오류는 로그인에 영향을 주지 않는다
    async def boom(*a, **k):
        raise RuntimeError("shadow store down")
    monkeypatch.setattr(lt, "reserve", boom)
    limiter.reset()
    again = await post_login(client, PASSWORD)
    wrong = await post_login(client, "wrong-password-xxxx")
    assert (again.status_code, wrong.status_code) == (200, 401)
    await lt.drain()


async def test_cancelled_attempt_when_password_not_configured_refunds_own_charge(client):
    await set_mode("shadow")
    async with sessionmaker()() as db:
        await db.execute(text("DELETE FROM credentials"))
        await db.commit()
    r = await post_login(client, PASSWORD)
    assert r.status_code == 503
    await lt.drain()
    rows = await attempts()
    assert [a.outcome for a in rows] == ["abandoned"]
    assert (await bucket()).tokens == pytest.approx(lt.BUCKET_CAP, abs=0.01)
    assert (await row("src:127.0.0.1")).budget == lt.BUDGET
    async with sessionmaker()() as db:  # 다음 시험을 위해 복구
        db.add(Credential(id=1, password_hash="x"))
        await db.commit()


async def test_parallel_20_http_logins_in_shadow_keep_budget_atomic_and_reject_nothing_extra(client, monkeypatch):
    from ttakkari.config import get_settings
    monkeypatch.setattr(get_settings(), "trust_proxy_ip_header", "CF-Connecting-IP")
    await set_mode("shadow")
    limiter.reset()

    async def attempt(i: int):
        return await client.post("/api/auth/login", json={"password": f"wrong-parallel-{i}x"},
                                 headers={"CF-Connecting-IP": "198.51.100.200"})
    statuses = [r.status_code for r in await asyncio.gather(*(attempt(i) for i in range(20)))]
    await lt.drain()
    assert set(statuses) <= {401, 429}  # 기존 제한이 판정(동시 검증 상한 등), shadow 가 더한 거절 없음
    rows = await attempts()
    assert all(a.src_key == "src:198.51.100.200" for a in rows)
    assert sum(1 for a in rows if a.charged_src) <= lt.BUDGET
    s = await row("src:198.51.100.200")
    assert s.budget >= 0
    assert all(a.outcome in {"fail", "ok", "abandoned"} for a in rows)


async def test_shadow_survives_restart_between_attempts(client):
    await set_mode("shadow")
    limiter.reset()
    for i in range(3):
        assert (await post_login(client, f"wrong-password-{i}x")).status_code == 401
    await lt.drain()
    lt.reset_state()
    await dispose_engine()
    await set_mode("shadow")
    limiter.reset()
    for i in range(2):
        assert (await post_login(client, f"wrong-password-{i}y")).status_code == 401
    await lt.drain()
    s = await row("src:127.0.0.1")
    assert s.budget == lt.BUDGET - 5 + 0 and s.budget == 0 and s.level == 1  # 재시작 전후 5건이 한 예산에 누적
    assert len(await attempts()) == 5
