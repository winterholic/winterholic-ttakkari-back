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
from ttakkari.runs import capabilities, profiles, usage_store, worker_routing
from ttakkari.runs import usage as usage_mod

KST = ZoneInfo("Asia/Seoul")

def _iso(epoch: float | None) -> str | None:
    return None if not epoch else datetime.fromtimestamp(epoch, KST).isoformat(timespec="seconds")


def _window(w: usage_mod.Window | None, now: float, points: list[usage_store.Point] | None = None) -> dict[str, Any] | None:
    if w is None:
        return None
    # 초기화 시각이 측정 뒤에 지났으면 그 사이 한도가 새로 시작됐다. 지난 퍼센트로 잔여나 사용량을 말하지 않는다.
    passed = bool(w.resets_at and w.resets_at <= now)
    remaining = None if passed else (w.remaining_percent if w.remaining_percent is not None else max(0, 100 - w.percent))
    used = None if passed else w.percent
    out: dict[str, Any] = {"used_percent": used, "remaining_percent": remaining,
                           "resets_at": _iso(w.resets_at), "reset_since_measured": passed,
                           "resets_in_seconds": None if not w.resets_at else max(0, round(w.resets_at - now))}
    burn = None if passed or not points else usage_store.burn_rate(points, w.key, w, now)
    out["burn_percent_per_hour"] = burn
    # 이 속도가 이어지면 한도가 차는 때까지(초)와, 초기화 전에 차는지. 속도를 모르면 null.
    eta = round((100 - w.percent) / burn * 3600) if burn and burn > 0 and not passed else None
    out["exhausts_in_seconds"] = eta
    out["exhausts_before_reset"] = None if eta is None or not w.resets_at else eta < (w.resets_at - now)
    return out


def _usage(account_id, now: float, history: dict[str, list[usage_store.Point]] | None = None,
           *, engine: str | None = None) -> dict[str, Any] | None:
    u = usage_mod.cached(account_id)
    if u is None:
        return None
    history = history or {}
    by_key = {w.key: w for w in u.windows}
    age = None if u.updated_at is None else max(0, round(now - u.updated_at))
    left = [100 - w.percent for w in u.windows if w.key in ("five_hour", "seven_day") and not (w.resets_at and w.resets_at <= now)]
    muse_current = not (engine == "muse" or u.source == 'muse_cli') or usage_mod.muse_quota_current(u, now)
    if not muse_current:
        left = []
    has_expired_window = any(w.resets_at and w.resets_at <= now for w in u.windows)
    # Antigravity 는 조회 실패 뒤 마지막 관측값을 유지한다. 오류가 붙은 값은 현재 측정이 아니다.
    agy_failed = (engine in {"antigravity", "opencode", "copilot"} or u.source in {"antigravity_cli", "copilot_sdk"}) and bool(u.error)
    return {
        "source": u.source, "plan": u.plan, "measured_at": _iso(u.updated_at), "age_seconds": age,
        "stale": age is None or age > get_settings().usage_stale_seconds or has_expired_window or not muse_current or agy_failed,
        # 5시간·주간 중 더 빡빡한 쪽의 잔여. 새로 시작된 창은 뺀다.
        "min_remaining_percent": min(left) if left else None,
        "five_hour": _window(by_key.get("five_hour"), now, history.get("five_hour")),
        "weekly": _window(by_key.get("seven_day"), now, history.get("seven_day")),
        "model_weekly": {w.key.removeprefix("seven_day_"): _window(w, now, history.get(w.key))
                         for w in u.windows if w.key.startswith("seven_day_")},
        # Antigravity reports shared group quotas, not per-account headroom. Preserve every
        # observed bucket and its exact source percentage without feeding routing caps.
        # state: 초기화가 지난 창은 unavailable, 조회 실패·지연 뒤의 값은 stale, 아니면 current.
        "model_quotas": [{"key": w.key, "label": w.label, **_window(w, now, history.get(w.key)),
                          "state": "unavailable" if w.resets_at and w.resets_at <= now
                          else "stale" if agy_failed or (age is None or age > get_settings().usage_stale_seconds) else "current"}
                         for w in u.windows if w.key.startswith(("agy_", "copilot_"))],
        "quota_unlimited": u.quota_unlimited,
        "credits_balance": u.credits_balance,
        "credits_error": u.credits_error,
        "error": u.error,
    }


def _cap_remaining(account_id, cap: int, usage: dict[str, Any] | None,
                   *, engine: str | None = None) -> float | None:
    if not usage or usage["stale"]:
        return None
    if engine in {"copilot", "opencode"}:
        room = profiles._usage_headroom(profiles.Profile(account_id, "", engine, max_used_percent=cap))
        return room[0] if room else None
    observed = usage_mod.cached(account_id)
    if observed and (engine == "muse" or observed.source == 'muse_cli') and not usage_mod.muse_quota_current(observed, time.time()):
        return None
    remaining = [max(0, cap - window["used_percent"]) for window in (usage["five_hour"], usage["weekly"])
                 if window and window["remaining_percent"] is not None and window["used_percent"] is not None]
    return min(remaining) if remaining else None


async def account_status(db: AsyncSession, pool: str) -> dict[str, Any]:
    """pool 은 부른 오케스트레이터 세션의 범위. work 면 회사에 들어온 계정만 회사 순서·켜기로, personal 이면 전체를 개인 순서·켜기로(scope 로 구분)."""
    s = get_settings()
    now = time.time()
    counts = await profiles.running_counts_async()
    override = await profiles.get_override(db, pool)
    out = []
    order = await profiles.effective_order(db, pool)
    hist = await usage_store.history(db, [a.id for a in order], now=now)
    for rank, a in enumerate(order, start=1):
        until = profiles.limited_until(a.id, a.max_used_percent)
        reason = None
        if until:
            reason = "cooldown" if profiles.cooldown_until(a.id) else "usage_window"
        orch = s.orchestrator_fallback_models.get(a.engine)
        usage = _usage(a.id, now, hist.get(a.id), engine=a.engine)
        cap_until = profiles.usage_limit_until(profiles.Profile(a.id, a.name, a.engine,
                                                              max_used_percent=a.max_used_percent), now)
        if cap_until:
            until = max(until or 0, cap_until)
            reason = "usage_cap"
        remaining = _cap_remaining(a.id, a.max_used_percent, usage, engine=a.engine)
        running = counts.get(a.id, 0)
        model_capacity = {model: worker_routing.capacity(
            profiles.Profile(a.id, a.name, a.engine, max_used_percent=a.max_used_percent, model=model), running)
            for model in s.engine_models.get(a.engine, [])}
        out.append({
            "name": a.name, "engine": a.engine, "scope": a.pool, "enabled": profiles.is_on(a, pool), "priority": rank,
            "available": profiles.is_on(a, pool) and not until and profiles.execution_ready(profiles._profile(a)),
            "limit": {"waiting": bool(until), "until": _iso(until), "reason": reason},
            "running_runs": running,
            "max_used_percent": a.max_used_percent,
            "remaining_to_limit_percent": remaining,
            "headroom_per_run_percent": None if remaining is None else remaining / (running + 1),
            "models": s.engine_models.get(a.engine, []),
            "model_capacity": model_capacity,
            "orchestrator_model": orch,
            "usage": usage,
        })
    from ttakkari.runs import qwen_slot
    return {
        "measured_at": _iso(now),
        "priority_override_until": override["until"] if override else None,
        "worker_routing": worker_routing.policy(),
        "muse_auto_routing": worker_routing.muse_auto_enabled(),
        "qwen_local": await qwen_slot.public_status(db, pool),
        # 엔진(실행 경로)별 프로브 실측 능력(ADR 0037). 기존 필드 의미는 그대로, 필드만 더한다.
        "execution_capabilities": capabilities.snapshot(now),
        "note": "usage is collected by the app every ~5 min (~1.5 min while the account runs a Run or is below 20% left) "
                f"and is not refreshed by this tool (null = never observed, stale = older than {s.usage_stale_seconds // 60} min). "
                "burn_percent_per_hour is the recent rise in used percent per hour (null = not enough history); "
                "exhausts_in_seconds/exhausts_before_reset extrapolate it. "
                "Accounts last measured at their max_used_percent in either the 5-hour or weekly window remain blocked "
                "even if stale, until reset, a newer lower measurement or a raised cap. "
                "Within each eligible task/model stage, compare accounts by headroom_per_run_percent "
                "(remaining_to_limit_percent / (running_runs + 1)); reset time breaks ties. "
                "This is a load-sharing score, not a consumption prediction; a Run excludes its own claim when choosing. "
                "Fixed-engine sessions compare within that engine; desk keeps its configured engine order. "
                "Fresh measured candidates rank before unknown candidates; unknowns keep model/idle/priority fallback. "
                "Auto workers use worker_routing and task_type; Antigravity is included. "
                "Use model_capacity for model-specific quota and cooldowns; account-level availability alone is insufficient. "
                "Task fit and known capacity precede stage priority, then headroom per Run. "
                "Desk and legacy auto sessions retain their native account policy; the orchestrator fails over "
                "to an enabled Muse account while every native account is usage-limited. "
                "Explicit worker/chat engines use that engine first and fall back to same-family then task-fit routes "
                "when all enabled accounts of the pinned engine hit usage limits (set strict_engine=True to wait instead); "
                "reroute an idle worker with message_worker(reroute=True). "
                "Flash (gemini-3.8-flash-*) is excluded from verification; when Flash completes a general/routine worker Run, "
                "independent cross-provider verification (task_type='verification') is required before final completion. "
                "Muse (muse-*) is a general/routine auto candidate only while muse_auto_routing is true (personal accounts only); "
                "its general/routine results require the same independent verification and Muse never verifies. "
                "An enabled Muse whose quota is unconfirmed (stale or /usage unavailable, e.g. after the 5-hour window reset while idle) "
                "is not treated as spare capacity, but model_capacity.exploration_eligible=true means routing may give it one real "
                f"task per {int(profiles.MUSE_PROBE_INTERVAL // 60)} min so a stream observation can end the unknown state. "
                "qwen_local shows the single local-Qwen slot (busy, current run, queue, wait estimate, recent "
                "verification/handoff counts). For routine workers in personal scope with parallel siblings, the backend "
                "defaults to Qwen-fit without declaration; general tasks still require declaring qwen_fit "
                "(needs_mcp/narrow_scope/verifiable). The backend routes one parallel-fit worker to Qwen while idle "
                "(receipt trigger parallel) and sends the rest to cloud. Orchestrators never take the parallel path; "
                "they reach Qwen only via the usage-limit emergency.",
        "accounts": out,
    }
