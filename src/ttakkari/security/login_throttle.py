"""로그인 제한 S1 shadow 관측(SEC2-LGN-01, docs/security/network-login-session-audit.md 5.5~5.8).

이 모듈은 **어떤 경우에도 로그인 요청을 거절하거나 늦추지 않는다**. 기존 메모리·IP 제한(`security/auth.py`)이 그대로 판정하고,
여기서는 같은 시도를 새 설계(레인 U: 출처 단계 잠금 + 계정 속도 제한)로 가상 평가해 DB에 남긴다.
모드는 `app_settings['login_throttle_mode']` 의 `off`(기본)와 `shadow` 만 받는다. 구현되지 않은 `enforce_source`·`enforce`·
알 수 없는 값은 `off` 로 취급하고 경고만 남긴다(설정 거부).

핵심 규칙(불변식)
- 해제는 시계로만 결정한다. 거절될 요청(would_block)은 어떤 결정 상태도 바꾸지 않는다.
- 예산은 검증 전에 행 잠금 아래에서 원자적으로 예약하고, 정답은 자기 예약분만 환불한다.
- 레인 U 의 행 잠금 순서는 항상 acct:u → src → attempt 이다(교착 방지). 토큰이 없으면 src 행을 만들지 않아
  출처 행 수가 acct:u refill 속도에 묶인다. 공용 overflow 키는 만들지 않는다.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, LoginAttempt, LoginBucket, LoginThrottle

log = logging.getLogger("ttakkari.login_throttle")

MODE_KEY = "login_throttle_mode"
MODE_TTL_SECONDS = 5.0
IMPLEMENTED_MODES = frozenset({"off", "shadow"})

ACCT_KEY = "acct:u"
BUDGET = 5
STAGES = (60, 300, 900, 3600)  # 5회 → 1분, 이후 단계당 1회 탐침으로 5/15/60분(상한)
MAX_LEVEL = len(STAGES)
QUIET_RESET = timedelta(hours=24)
BUCKET_CAP = 6.0
BUCKET_REFILL_SECONDS = 600.0
SRC_ROW_CAP = 20_000
SRC_EVICT_BATCH = 100
SRC_ROW_TTL = timedelta(days=7)
ABANDON_AFTER = timedelta(minutes=2)
ATTEMPT_RETENTION = timedelta(hours=24)
FLUSH_SECONDS = 30.0
SWEEP_SECONDS = 600.0
SHADOW_CONCURRENCY = 2
SHADOW_QUEUE_MAX = 256
# shadow 작업은 로그인과 같은 DB 엔진·풀을 쓴다. 풀이 차거나 잠금 대기가 길어져도 로그인이 늦어지지 않게
# (1) 동시 실행 2개, (2) 대기열 포함 전체 시간 상한, (3) 트랜잭션 안 잠금·문장 시간 상한을 둔다. 넘으면 기록만 버린다.
SHADOW_TIMEOUT_SECONDS = 1.5
SHADOW_LOCK_TIMEOUT_MS = 500
SHADOW_STATEMENT_TIMEOUT_MS = 1000


def _now() -> datetime:
    return datetime.now(UTC)


def source_key(ip: str | None) -> str:
    """IPv4 는 주소, IPv6 는 /64 를 한 키로 본다. 해석할 수 없으면 하나의 unknown 키."""
    if not ip or ip == "unknown":
        return "src:unknown"
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return "src:unknown"
    if addr.version == 6:
        if addr.ipv4_mapped is not None:
            return f"src:{addr.ipv4_mapped}"
        return f"src:{ipaddress.ip_network(f'{addr}/64', strict=False)}"
    return f"src:{addr}"


@dataclass(frozen=True)
class Reservation:
    id: uuid.UUID
    src_key: str
    would_block: str | None
    charged_src: bool
    charged_acct: bool


# ---------------------------------------------------------------- 모드

_mode_cache: tuple[float, str] | None = None
_mode_refreshing = False
_warned_modes: set[str] = set()


def _normalize_mode(raw: object) -> str:
    mode = raw if isinstance(raw, str) else "off"
    if mode in IMPLEMENTED_MODES:
        return mode
    if mode not in _warned_modes:
        _warned_modes.add(mode)
        log.warning("login_throttle_mode=%r 는 구현되지 않았거나 알 수 없는 값이라 off 로 처리합니다", mode)
    return "off"


async def refresh_mode() -> str:
    """DB 에서 모드를 읽어 캐시한다. 실패하면 off."""
    global _mode_cache
    try:
        async with sessionmaker()() as db:
            row = await db.get(AppSetting, MODE_KEY)
        mode = _normalize_mode((row.value or {}).get("mode") if row else "off")
    except Exception:  # noqa: BLE001 -- 관측 실패가 로그인에 영향을 주면 안 된다
        log.warning("login_throttle 모드를 읽지 못해 off 로 처리합니다")
        mode = "off"
    _mode_cache = (time.monotonic(), mode)
    return mode


async def _refresh_mode_task() -> None:
    global _mode_refreshing
    try:
        await refresh_mode()
    finally:
        _mode_refreshing = False


def current_mode() -> str:
    """요청 경로에서 부르는 동기 함수. I/O 를 하지 않는다(5초 캐시, 만료되면 백그라운드로 갱신)."""
    global _mode_refreshing
    now = time.monotonic()
    if _mode_cache is not None and now - _mode_cache[0] < MODE_TTL_SECONDS:
        return _mode_cache[1]
    if not _mode_refreshing:
        _mode_refreshing = True
        try:
            _spawn(_refresh_mode_task())
        except RuntimeError:
            _mode_refreshing = False
    return _mode_cache[1] if _mode_cache is not None else "off"


# ---------------------------------------------------------------- 상태 기계

async def _bound_transaction(db) -> None:
    """이 트랜잭션 안에서만 잠금·문장 대기 시간을 제한한다(SET LOCAL). 로그인 경로의 다른 연결에는 영향이 없다."""
    await db.execute(text(f"SET LOCAL lock_timeout = '{int(SHADOW_LOCK_TIMEOUT_MS)}ms'"))
    await db.execute(text(f"SET LOCAL statement_timeout = '{int(SHADOW_STATEMENT_TIMEOUT_MS)}ms'"))


async def _lock_bucket(db, key: str, now: datetime) -> LoginBucket:
    await db.execute(pg_insert(LoginBucket).values(key=key, tokens=BUCKET_CAP, refilled_at=now, blocked_count=0)
                     .on_conflict_do_nothing(index_elements=[LoginBucket.key]))
    bucket = (await db.execute(select(LoginBucket).where(LoginBucket.key == key).with_for_update())).scalar_one()
    if now > bucket.refilled_at:
        elapsed = (now - bucket.refilled_at).total_seconds()
        bucket.tokens = min(BUCKET_CAP, bucket.tokens + elapsed / BUCKET_REFILL_SECONDS)
        bucket.refilled_at = now
    return bucket


async def _evict_src_rows(db, now: datetime) -> int:
    """상한이 찼을 때 오래된 **비잠금** 행부터 지운다. 잠긴 행은 지키고, 지울 것이 없으면 0."""
    keys = list((await db.execute(
        select(LoginThrottle.key)
        .where(LoginThrottle.kind == "src", (LoginThrottle.locked_until.is_(None)) | (LoginThrottle.locked_until <= now))
        .order_by(LoginThrottle.updated_at).limit(SRC_EVICT_BATCH))).scalars())
    if keys:
        await db.execute(delete(LoginThrottle).where(LoginThrottle.key.in_(keys)))
    return len(keys)


async def _lock_src_row(db, key: str, now: datetime) -> LoginThrottle | None:
    """출처 행을 잠그고 돌려준다. 없으면 만든다. 상한이 가득 차 자리를 못 내면 None(개별 키 없이 처리)."""
    row = (await db.execute(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())).scalar_one_or_none()
    if row is not None:
        return row
    count = await db.scalar(select(func.count()).select_from(LoginThrottle).where(LoginThrottle.kind == "src"))
    if (count or 0) >= SRC_ROW_CAP and await _evict_src_rows(db, now) == 0:
        return None
    await db.execute(pg_insert(LoginThrottle).values(
        key=key, kind="src", budget=BUDGET, level=0, stage_started_at=now, blocked_count=0, updated_at=now)
        .on_conflict_do_nothing(index_elements=[LoginThrottle.key]))
    return (await db.execute(select(LoginThrottle).where(LoginThrottle.key == key).with_for_update())).scalar_one()


async def reserve(src_key: str, *, attempt_id: uuid.UUID | None = None, now: datetime | None = None) -> Reservation:
    """레인 U 예약(검증 전, 트랜잭션 하나). shadow 에서는 would_block 이어도 진행을 허용하고 사유만 기록한다."""
    now = now or _now()
    attempt_id = attempt_id or uuid.uuid4()
    would: str | None = None
    charged_src = charged_acct = False
    async with sessionmaker()() as db, db.begin():
        await _bound_transaction(db)
        bucket = await _lock_bucket(db, ACCT_KEY, now)
        if bucket.tokens < 1.0:
            would = "acct_bucket_empty"
        else:
            row = await _lock_src_row(db, src_key, now)
            if row is not None:
                if row.last_failure_at is not None and now - row.last_failure_at > QUIET_RESET:
                    row.level, row.budget, row.locked_until, row.lock_trigger = 0, BUDGET, None, None
                    row.stage_started_at = now
                if row.locked_until is not None and row.locked_until > now:
                    would = "src_locked"
                else:
                    if row.budget <= 0:
                        row.budget = 1  # 잠금이 막 끝남: 탐침 1회
                    row.budget -= 1
                    charged_src = True
                    if row.budget == 0:
                        row.level = min(row.level + 1, MAX_LEVEL)
                        row.locked_until = now + timedelta(seconds=STAGES[row.level - 1])
                        row.lock_trigger = attempt_id
                        row.summarized_at = None
                    row.updated_at = now
            if would is None:
                bucket.tokens -= 1.0
                charged_acct = True
        db.add(LoginAttempt(id=attempt_id, lane="U", src_key=src_key, reserved_at=now, would_block=would,
                            charged_src=charged_src, charged_acct=charged_acct, outcome="pending"))
    if would == "src_locked":
        _count_blocked(src_key)
    elif would == "acct_bucket_empty":
        _count_blocked(ACCT_KEY)
    return Reservation(attempt_id, src_key, would, charged_src, charged_acct)


async def finish_attempt(res: Reservation, ok: bool | None, *, now: datetime | None = None) -> None:
    """확정(검증 후). ok=True 성공, False 실패, None 검증 없이 취소(abandoned, 자기 소비 환불).

    환불은 **자기 예약이 소비한 것만** 한다. 전체 초기화는 다른 pending 이 없고 자기보다 늦게 예약된 시도가 없을 때만 한다.
    """
    now = now or _now()
    async with sessionmaker()() as db, db.begin():
        await _bound_transaction(db)
        if res.charged_acct or res.charged_src:
            bucket = await _lock_bucket(db, ACCT_KEY, now) if res.charged_acct else None
            row = (await db.execute(select(LoginThrottle).where(LoginThrottle.key == res.src_key).with_for_update())
                   ).scalar_one_or_none() if res.charged_src else None
        else:
            bucket = row = None
        attempt = (await db.execute(select(LoginAttempt).where(LoginAttempt.id == res.id).with_for_update())
                   ).scalar_one_or_none()
        if attempt is None or attempt.outcome != "pending":
            return
        attempt.outcome = "ok" if ok else ("fail" if ok is False else "abandoned")
        if ok is False:
            if row is not None:
                row.last_failure_at = now
                row.updated_at = now
            return
        # 성공 또는 취소: 자기 예약분만 환불한다.
        if bucket is not None:
            bucket.tokens = min(BUCKET_CAP, bucket.tokens + 1.0)
        if row is None:
            return
        row.budget = min(row.budget + 1, BUDGET)
        if row.lock_trigger == res.id:
            row.locked_until = None
            row.lock_trigger = None
            row.level = max(row.level - 1, 0)
        row.updated_at = now
        if ok is True:
            others = await db.scalar(
                select(func.count()).select_from(LoginAttempt).where(
                    LoginAttempt.src_key == res.src_key, LoginAttempt.charged_src.is_(True), LoginAttempt.id != res.id,
                    (LoginAttempt.outcome == "pending") | (LoginAttempt.reserved_at > attempt.reserved_at)))
            if not others:
                row.level, row.budget, row.locked_until, row.lock_trigger = 0, BUDGET, None, None
                row.stage_started_at = now


# ---------------------------------------------------------------- 차단 집계(재시작 유실 방지)

_blocked: dict[str, int] = {}


def _count_blocked(key: str) -> None:
    _blocked[key] = _blocked.get(key, 0) + 1


async def flush_counters() -> int:
    """메모리 차단 카운터를 DB 의 blocked_count 에 더한다. 실패하면 메모리에 되돌려 다음 주기에 다시 시도."""
    if not _blocked:
        return 0
    taken = dict(_blocked)
    _blocked.clear()
    try:
        async with sessionmaker()() as db, db.begin():
            for key, n in taken.items():
                if key == ACCT_KEY:
                    await db.execute(update(LoginBucket).where(LoginBucket.key == key)
                                     .values(blocked_count=LoginBucket.blocked_count + n))
                else:
                    await db.execute(update(LoginThrottle).where(LoginThrottle.key == key)
                                     .values(blocked_count=LoginThrottle.blocked_count + n))
    except Exception:  # noqa: BLE001
        for key, n in taken.items():
            _blocked[key] = _blocked.get(key, 0) + n
        log.warning("login_throttle 차단 집계 flush 실패, 다음 주기에 다시 시도합니다")
        return 0
    return sum(taken.values())


async def sweep(now: datetime | None = None) -> dict[str, int]:
    """고아 예약 정리, 오래된 시도 행·유휴 출처 행 삭제."""
    now = now or _now()
    async with sessionmaker()() as db, db.begin():
        abandoned = (await db.execute(
            update(LoginAttempt).where(LoginAttempt.outcome == "pending", LoginAttempt.reserved_at < now - ABANDON_AFTER)
            .values(outcome="abandoned"))).rowcount
        old_attempts = (await db.execute(
            delete(LoginAttempt).where(LoginAttempt.reserved_at < now - ATTEMPT_RETENTION))).rowcount
        idle_rows = (await db.execute(
            delete(LoginThrottle).where(LoginThrottle.kind == "src", LoginThrottle.level == 0,
                                        LoginThrottle.updated_at < now - SRC_ROW_TTL))).rowcount
    return {"abandoned": abandoned or 0, "attempts_deleted": old_attempts or 0, "rows_deleted": idle_rows or 0}


# ---------------------------------------------------------------- 요청 경로 연결(비차단)

_pending: set[asyncio.Task] = set()
_sem: asyncio.Semaphore | None = None
_maintenance: asyncio.Task | None = None
_dropped = 0  # 대기열이 가득 차 시작하지 못한 시도
_timeouts = 0  # 시간 상한을 넘겨 버려진 예약·확정
_errors = 0  # DB 오류로 버려진 예약·확정
_last_warn = 0.0


def stats() -> dict[str, int]:
    """S1 운영 지표: dropped·timeouts·errors 가 늘면 shadow 가 DB 를 못 따라가는 것이다(5.8.2)."""
    return {"dropped": _dropped, "timeouts": _timeouts, "errors": _errors, "pending": len(_pending)}


def _warn_limited(msg: str) -> None:
    global _last_warn
    now = time.monotonic()
    if now - _last_warn >= 60:
        _last_warn = now
        log.warning(msg)


def _spawn(coro) -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)
    return task


def _get_sem() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(SHADOW_CONCURRENCY)
    return _sem


@dataclass
class Handle:
    id: uuid.UUID
    src_key: str
    task: asyncio.Task | None = None
    result: Reservation | None = field(default=None)


async def _do_reserve(h: Handle) -> None:
    global _timeouts, _errors
    try:
        async with asyncio.timeout(SHADOW_TIMEOUT_SECONDS), _get_sem():  # 대기열 시간 포함
            h.result = await reserve(h.src_key, attempt_id=h.id)
    except TimeoutError:
        _timeouts += 1
        _warn_limited("login_throttle shadow 예약이 시간 상한을 넘어 기록만 버렸습니다(로그인에는 영향 없음)")
    except Exception:  # noqa: BLE001 -- shadow 는 로그인을 절대 막지 않는다
        _errors += 1
        _warn_limited("login_throttle shadow 예약 실패(로그인에는 영향 없음)")


async def _do_finish(h: Handle, ok: bool | None) -> None:
    global _timeouts, _errors
    try:
        async with asyncio.timeout(SHADOW_TIMEOUT_SECONDS):
            if h.task is not None:
                await asyncio.shield(h.task)
            if h.result is None:
                return
            async with _get_sem():
                await finish_attempt(h.result, ok)
    except TimeoutError:
        _timeouts += 1  # 확정이 버려지면 예약은 pending 으로 남고 sweep 이 2분 뒤 abandoned 로 바꾼다(보수적)
        _warn_limited("login_throttle shadow 확정이 시간 상한을 넘어 버렸습니다(로그인에는 영향 없음)")
    except Exception:  # noqa: BLE001
        _errors += 1
        _warn_limited("login_throttle shadow 확정 실패(로그인에는 영향 없음)")


async def _maintenance_loop() -> None:
    last_sweep = 0.0
    while True:
        await asyncio.sleep(FLUSH_SECONDS)
        with contextlib.suppress(Exception):
            await flush_counters()
        if time.monotonic() - last_sweep >= SWEEP_SECONDS:
            last_sweep = time.monotonic()
            with contextlib.suppress(Exception):
                await sweep()


def begin(ip: str | None) -> Handle | None:
    """로그인 시도 하나를 shadow 로 예약한다. 동기·비차단이며 어떤 예외도 밖으로 내지 않는다."""
    global _maintenance, _dropped
    try:
        if current_mode() != "shadow":
            return None
        if len(_pending) >= SHADOW_QUEUE_MAX:
            _dropped += 1
            return None
        h = Handle(id=uuid.uuid4(), src_key=source_key(ip))
        h.task = _spawn(_do_reserve(h))
        if _maintenance is None or _maintenance.done():
            _maintenance = asyncio.get_running_loop().create_task(_maintenance_loop())
        return h
    except Exception:  # noqa: BLE001
        return None


def finish(h: Handle | None, ok: bool | None) -> None:
    if h is None:
        return
    with contextlib.suppress(Exception):
        _spawn(_do_finish(h, ok))


async def drain(timeout: float = 5.0) -> None:
    """진행 중인 shadow 작업을 기다린다(종료·시험용)."""
    if _pending:
        with contextlib.suppress(Exception):
            await asyncio.wait(list(_pending), timeout=timeout)


async def shutdown() -> None:
    global _maintenance
    await drain()
    if _maintenance is not None:
        _maintenance.cancel()
        with contextlib.suppress(BaseException):
            await _maintenance
        _maintenance = None
    with contextlib.suppress(Exception):
        await flush_counters()


async def reset_decision_state() -> dict[str, int]:
    """S2 진입 절차용: 잠금·단계·토큰만 초기화하고 시도 이력과 blocked_count 집계는 보존한다(5.8.1)."""
    now = _now()
    async with sessionmaker()() as db, db.begin():
        rows = (await db.execute(
            update(LoginThrottle).values(budget=BUDGET, level=0, locked_until=None, lock_trigger=None,
                                         last_failure_at=None, stage_started_at=now, summarized_at=None,
                                         updated_at=now))).rowcount
        buckets = (await db.execute(
            update(LoginBucket).values(tokens=BUCKET_CAP, refilled_at=now))).rowcount
    return {"throttle_rows": rows or 0, "buckets": buckets or 0}


def reset_state() -> None:
    """프로세스 안 캐시·카운터만 비운다(시험용). DB 는 건드리지 않는다."""
    global _mode_cache, _mode_refreshing, _sem, _dropped, _timeouts, _errors, _last_warn
    _mode_cache = None
    _mode_refreshing = False
    _sem = None
    _dropped = _timeouts = _errors = 0
    _last_warn = 0.0
    _blocked.clear()
    _warned_modes.clear()
