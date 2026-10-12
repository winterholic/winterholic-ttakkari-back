"""오케스트레이터가 작업자를 배정할 때 보는 계정별 상태(account_usage 도구). ADR 0006.

사용량은 앱이 마지막으로 본 값(캐시·실행 스트림)만 쓴다. 이 모듈은 새로 조회하지 않고 인증 파일·키체인도 읽지 않는다.
계정 폴더 경로·로그인 이메일·토큰은 내지 않는다.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.runs import profiles
from ttakkari.runs import usage as usage_mod

KST = ZoneInfo("Asia/Seoul")
# 사용량 화면을 열어 두면 2분마다 갱신된다. 이보다 오래된 값은 낡았다고 알린다.
STALE_SECONDS = 15 * 60


def _iso(epoch: float | None) -> str | None:
    return None if not epoch else datetime.fromtimestamp(epoch, KST).isoformat(timespec="seconds")


def _window(w: usage_mod.Window | None, now: float) -> dict[str, Any] | None:
    if w is None:
        return None
    # 초기화 시각이 측정 뒤에 지났으면 그 사이 한도가 새로 시작됐다. 지난 퍼센트로 잔여를 말하지 않는다.
    passed = bool(w.resets_at and w.resets_at <= now)
    return {"used_percent": w.percent, "remaining_percent": None if passed else max(0, 100 - w.percent),
            "resets_at": _iso(w.resets_at), "reset_since_measured": passed}


def _usage(account_id, now: float) -> dict[str, Any] | None:
    u = usage_mod.cached(account_id)
    if u is None:
        return None
    by_key = {w.key: w for w in u.windows}
    age = None if u.updated_at is None else max(0, round(now - u.updated_at))
    return {
        "source": u.source, "plan": u.plan, "measured_at": _iso(u.updated_at), "age_seconds": age,
        "stale": age is None or age > STALE_SECONDS,
        "five_hour": _window(by_key.get("five_hour"), now),
        "weekly": _window(by_key.get("seven_day"), now),
        "model_weekly": {w.key.removeprefix("seven_day_"): _window(w, now) for w in u.windows if w.key.startswith("seven_day_")},
        "error": u.error,
    }


async def account_status(db: AsyncSession, pool: str) -> dict[str, Any]:
    """pool 은 부른 오케스트레이터 세션의 범위. work 면 회사 그룹 계정만, personal 이면 전체(scope 로 구분)."""
    s = get_settings()
    now = time.time()
    counts = profiles.running_counts()
    override = await profiles.get_override(db)
    out = []
    for rank, a in enumerate(await profiles.effective_order(db, pool), start=1):
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
            "usage": _usage(a.id, now),
        })
    return {
        "measured_at": _iso(now),
        "priority_override_until": override["until"] if override else None,
        "note": "usage is the app's last observed value (null = never observed, stale = older than 15 min); "
                "it is not refreshed by this tool. New workers go to available accounts with no running Run first, "
                "then by priority.",
        "accounts": out,
    }
