"""오케스트레이터가 작업자를 배정할 때 보는 계정별 상태(account_usage 도구). ADR 0006.

사용량은 수집기(runs/usage_collector.py)와 실행 스트림이 채운 마지막 값만 쓴다. 이 모듈은 새로 조회하지 않고 인증 파일·키체인도 읽지 않는다.
소모 속도는 DB 의 측정 이력(usage_store)에서 계산한다.
계정 폴더 경로·로그인 이메일·토큰은 내지 않는다.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.runs import profiles, usage_store
from ttakkari.runs import usage as usage_mod

KST = ZoneInfo("Asia/Seoul")

def _iso(epoch: float | None) -> str | None:
    return None if not epoch else datetime.fromtimestamp(epoch, KST).isoformat(timespec="seconds")


def _window(w: usage_mod.Window | None, now: float, points: list[usage_store.Point] | None = None) -> dict[str, Any] | None:
    if w is None:
        return None
    # 초기화 시각이 측정 뒤에 지났으면 그 사이 한도가 새로 시작됐다. 지난 퍼센트로 잔여를 말하지 않는다.
    passed = bool(w.resets_at and w.resets_at <= now)
    out: dict[str, Any] = {"used_percent": w.percent, "remaining_percent": None if passed else max(0, 100 - w.percent),
                           "resets_at": _iso(w.resets_at), "reset_since_measured": passed,
                           "resets_in_seconds": None if not w.resets_at else max(0, round(w.resets_at - now))}
    burn = None if passed or not points else usage_store.burn_rate(points, w.key, w, now)
    out["burn_percent_per_hour"] = burn
    # 이 속도가 이어지면 한도가 차는 때까지(초)와, 초기화 전에 차는지. 속도를 모르면 null.
    eta = round((100 - w.percent) / burn * 3600) if burn and burn > 0 and not passed else None
    out["exhausts_in_seconds"] = eta
    out["exhausts_before_reset"] = None if eta is None or not w.resets_at else eta < (w.resets_at - now)
    return out


def _usage(account_id, now: float, history: dict[str, list[usage_store.Point]] | None = None) -> dict[str, Any] | None:
    u = usage_mod.cached(account_id)
    if u is None:
        return None
    history = history or {}
    by_key = {w.key: w for w in u.windows}
    age = None if u.updated_at is None else max(0, round(now - u.updated_at))
    left = [100 - w.percent for w in u.windows if w.key in ("five_hour", "seven_day") and not (w.resets_at and w.resets_at <= now)]
    return {
        "source": u.source, "plan": u.plan, "measured_at": _iso(u.updated_at), "age_seconds": age,
        "stale": age is None or age > get_settings().usage_stale_seconds,
        # 5시간·주간 중 더 빡빡한 쪽의 잔여. 새로 시작된 창은 뺀다.
        "min_remaining_percent": min(left) if left else None,
        "five_hour": _window(by_key.get("five_hour"), now, history.get("five_hour")),
        "weekly": _window(by_key.get("seven_day"), now, history.get("seven_day")),
        "model_weekly": {w.key.removeprefix("seven_day_"): _window(w, now, history.get(w.key))
                         for w in u.windows if w.key.startswith("seven_day_")},
        "error": u.error,
    }


async def account_status(db: AsyncSession, pool: str) -> dict[str, Any]:
    """pool 은 부른 오케스트레이터 세션의 범위. work 면 회사 그룹 계정만, personal 이면 전체(scope 로 구분)."""
    s = get_settings()
    now = time.time()
    counts = profiles.running_counts()
    override = await profiles.get_override(db)
    out = []
    order = await profiles.effective_order(db, pool)
    hist = await usage_store.history(db, [a.id for a in order], now=now)
    for rank, a in enumerate(order, start=1):
        until = profiles.limited_until(a.id)
        reason = None
        if until:
            reason = "cooldown" if profiles.cooldown_until(a.id) else "usage_window"
        orch = s.orchestrator_fallback_models.get(a.engine)
        out.append({
            "name": a.name, "engine": a.engine, "scope": a.pool, "enabled": a.enabled, "priority": rank,
            "available": a.enabled and not until,
            "limit": {"waiting": bool(until), "until": _iso(until), "reason": reason},
            "running_runs": counts.get(a.id, 0),
            "models": s.engine_models.get(a.engine, []),
            "orchestrator_model": orch,
            "usage": _usage(a.id, now, hist.get(a.id)),
        })
    return {
        "measured_at": _iso(now),
        "priority_override_until": override["until"] if override else None,
        "note": "usage is collected by the app every ~5 min (~1.5 min while the account runs a Run or is below 20% left) "
                f"and is not refreshed by this tool (null = never observed, stale = older than {s.usage_stale_seconds // 60} min). "
                "burn_percent_per_hour is the recent rise in used percent per hour (null = not enough history); "
                "exhausts_in_seconds/exhausts_before_reset extrapolate it. "
                "New workers go to available accounts with no running Run first, then by priority.",
        "accounts": out,
    }
