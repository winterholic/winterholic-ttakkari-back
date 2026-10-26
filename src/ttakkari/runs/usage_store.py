"""계정 사용량 측정값의 DB 저장(최신값 + 추세용 이력). 토큰은 다루지 않는다: 퍼센트·초기화 시각·출처만 둔다.

저장은 usage 모듈의 sink 로 이어진다. 수집기·사용량 화면·실행 스트림 어느 쪽이 값을 봐도 같은 표에 쌓인다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ttakkari.db import sessionmaker
from ttakkari.models import AccountUsageSample
from ttakkari.runs import usage as usage_mod

log = logging.getLogger(__name__)

# 같은 값이면 이 시간 안에는 다시 쓰지 않는다. 값이 바뀌었어도 MIN_GAP 안에는 한 번만 쓴다(실행 중 스트림이 잦다).
SAME_VALUE_GAP = 600.0
MIN_GAP = 60.0
# 추세를 읽는 되돌아볼 시간(초). 5시간 창은 짧게, 주간 창은 하루.
LOOKBACK = {"five_hour": 2 * 3600, "default": 24 * 3600}
MIN_SPAN = 300.0  # 이보다 짧은 구간으로는 소모 속도를 말하지 않는다.

_last: dict[uuid.UUID, tuple[float, tuple]] = {}
_tasks: set[asyncio.Task] = set()


def _sig(u: usage_mod.Usage) -> tuple:
    return (tuple((w.key, w.percent, w.resets_at, w.remaining_percent) for w in u.windows),
            u.credits_balance, u.credits_error, tuple(sorted(u.quota_unlimited.items())))


def _window_payload(u: usage_mod.Usage) -> list[dict]:
    return [{"key": w.key, "label": w.label, "percent": w.percent, "resets_at": w.resets_at,
             "remaining_percent": w.remaining_percent} for w in u.windows] + [
        {"key": key, "label": label, "unlimited": True} for key, label in u.quota_unlimited.items()]


def _decode_windows(payload: list) -> tuple[list[usage_mod.Window], dict[str, str]]:
    windows = [usage_mod.Window(w["key"], w.get("label") or usage_mod.label_of(w["key"]), int(w["percent"]),
                                w.get("resets_at"), w.get("remaining_percent"))
               for w in payload if isinstance(w, dict) and "key" in w and "percent" in w and w.get("unlimited") is not True]
    unlimited = {w["key"]: w.get("label") or usage_mod.label_of(w["key"]) for w in payload
                 if isinstance(w, dict) and isinstance(w.get("key"), str) and w.get("unlimited") is True}
    return windows, unlimited


async def record(account_id: uuid.UUID, u: usage_mod.Usage) -> bool:
    """측정값 한 개를 남긴다. 거의 같은 값이 막 남았으면 건너뛴다(True 면 남김)."""
    if not (u.windows or u.quota_unlimited):
        return False
    from ttakkari import generation
    now = time.time()
    sig = _sig(u)
    prev = _last.get(account_id)
    if prev:
        age, same = now - prev[0], prev[1] == sig
        if (same and age < SAME_VALUE_GAP) or (age < MIN_GAP and not generation.current):
            return False
    _last[account_id] = (now, sig)
    async with sessionmaker()() as db:
        db.add(AccountUsageSample(
            account_id=account_id, measured_at=datetime.fromtimestamp(u.updated_at or now, UTC), source=u.source or "api",
            plan=u.plan, windows=_window_payload(u),
            credits_balance=u.credits_balance, credits_error=u.credits_error))
        if generation.current:
            await generation.wake(db)
        await db.commit()
    return True


def schedule_record(account_id: uuid.UUID, u: usage_mod.Usage) -> None:
    """usage 모듈의 sink. 동기 코드에서 불리므로 돌고 있는 이벤트 루프가 있을 때만 남긴다."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(_safe_record(account_id, u))
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


async def _safe_record(account_id: uuid.UUID, u: usage_mod.Usage) -> None:
    try:
        await record(account_id, u)
    except Exception as e:  # noqa: BLE001
        # 저장이 실패해도 수집·실행은 계속한다. 다음 값이 오면 다시 시도한다.
        _last.pop(account_id, None)
        log.warning("usage sample not saved: %s", type(e).__name__)


async def flush() -> None:
    """남기는 중인 저장을 기다린다(테스트·종료용).

    끝났는데 완료 콜백(_tasks.discard)이 돌지 못해 남은 작업(이미 닫힌 루프의 작업 등)은 목록에서 직접 뺀다.
    빼지 않으면 끝난 작업만 gather 하는 이 루프가 영영 끝나지 않는다(2026-10-19 배포 pytest 의 120초 타임아웃)."""
    while True:
        for task in [t for t in _tasks if t.done()]:
            _tasks.discard(task)
        if not _tasks:
            return
        await asyncio.gather(*list(_tasks), return_exceptions=True)


def reset() -> None:
    _last.clear()


def install() -> None:
    usage_mod.set_sink(schedule_record)


async def load_latest(db: AsyncSession, *, refresh: bool = False) -> int:
    """계정별 마지막 측정값으로 메모리 캐시를 채운다(재시작 직후 account_usage 가 비지 않게)."""
    newest = select(AccountUsageSample.account_id, func.max(AccountUsageSample.measured_at).label("m")).group_by(
        AccountUsageSample.account_id).subquery()
    rows = list(await db.scalars(select(AccountUsageSample).join(
        newest, (AccountUsageSample.account_id == newest.c.account_id) & (AccountUsageSample.measured_at == newest.c.m))))
    for r in rows:
        windows, unlimited = _decode_windows(r.windows or [])
        usage_mod.seed(r.account_id, usage_mod.Usage(windows=windows, source=r.source, plan=r.plan,
                                                      credits_balance=r.credits_balance,
                                                      credits_error=r.credits_error,
                                                      quota_unlimited=unlimited, updated_at=r.measured_at.timestamp()), refresh=refresh)
        _last[r.account_id] = (r.measured_at.timestamp(), _sig(usage_mod.Usage(
            windows=windows, quota_unlimited=unlimited, credits_balance=r.credits_balance, credits_error=r.credits_error)))
    return len(rows)


async def prune(db: AsyncSession, days: int) -> int:
    """보존 기간이 지난 이력을 지운다. 계정별 가장 최근 행은 남긴다."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    newer = aliased(AccountUsageSample)
    has_newer = select(newer.id).where(newer.account_id == AccountUsageSample.account_id,
                                       newer.measured_at > AccountUsageSample.measured_at).exists()
    res = await db.execute(delete(AccountUsageSample).where(AccountUsageSample.measured_at < cutoff, has_newer))
    await db.commit()
    return res.rowcount or 0


# ---- 추세

@dataclass
class Point:
    at: float
    percent: int
    resets_at: int | None


async def history(db: AsyncSession, account_ids: list[uuid.UUID], *, now: float | None = None) -> dict[uuid.UUID, dict[str, list[Point]]]:
    """계정 → 창 키 → 시간순 점(최근 24시간)."""
    now = now or time.time()
    since = datetime.fromtimestamp(now - LOOKBACK["default"], UTC)
    out: dict[uuid.UUID, dict[str, list[Point]]] = {}
    if not account_ids:
        return out
    rows = await db.scalars(select(AccountUsageSample).where(
        AccountUsageSample.account_id.in_(account_ids), AccountUsageSample.measured_at >= since)
        .order_by(AccountUsageSample.measured_at))
    for r in rows:
        for w in r.windows or []:
            with contextlib.suppress(KeyError, TypeError, ValueError):
                out.setdefault(r.account_id, {}).setdefault(w["key"], []).append(
                    Point(r.measured_at.timestamp(), int(w["percent"]), w.get("resets_at")))
    return out


def burn_rate(points: list[Point], key: str, current: usage_mod.Window, now: float) -> float | None:
    """같은 초기화 주기 안에서 최근 구간의 퍼센트 증가 속도(퍼센트포인트/시간). 구간이 짧거나 줄어든 경우 None.

    percent 는 정수라 구간이 길수록 믿을 만하다. 초기화가 끼면 resets_at 이 달라지므로 그 앞 점은 버린다."""
    lookback = LOOKBACK.get(key, LOOKBACK["default"])
    same = [p for p in points if p.at >= now - lookback
            and (current.resets_at is None or p.resets_at is None or abs(p.resets_at - current.resets_at) < 120)]
    if len(same) < 2:
        return None
    first, last = same[0], same[-1]
    span = last.at - first.at
    if span < MIN_SPAN or last.percent < first.percent:
        return None
    return round((last.percent - first.percent) / (span / 3600), 1)
