"""One task policy for auto workers and limit-fallback routing for pinned sessions.

Task fit precedes account load. Unknown capacity stays unknown; stale exhausted pools
are still blocked by profiles. Strict-engine sessions keep hard engine locks; default
pinned sessions switch to same-family then task-fit alternatives only when every
enabled account of the pinned engine is usage-limited.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import replace
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.model_catalog import quota_group, supports_effort
from ttakkari.runs import profiles, usage

TaskType = Literal["general", "routine", "design", "verification", "reasoning"]
POLICY_VERSION = "worker-routing-v2"
SONNET = ("antigravity", "claude-sonnet-5-5-high")
FLASH = ("antigravity", "gemini-3.8-flash-high")
LUNA = ("codex", "gpt-6-luna")
NATIVE_SONNET = ("claude", "sonnet")
ARGON = ("antigravity", "gemini-4-argon-high")
AGY_OPUS = ("antigravity", "claude-opus-5-5-medium")
SOL = ("codex", "gpt-6.1-sol")
NATIVE_OPUS = ("claude", "opus")


def _qwen_local_model() -> str:
    from ttakkari.config import get_settings
    return get_settings().worker_default_models.get("qwen-local", {}).get("model") or "Qwen3.8-Flash-Next-Q2_K"


QWEN_LOCAL = ("qwen-local", "Qwen3.8-Flash-Next-Q2_K")
# Muse has one account-wide 5h/weekly pool (keys five_hour/seven_day), so headroom compares like native engines.
MUSE_FULL = ("muse", "muse-wonder-full-1.0")
MUSE_SPARK = ("muse", "muse-spark-1.3")
# Each stage has equally suitable models; profiles.choose balances accounts within it.
# Flash is excluded from verification so cross-provider review uses GPT first, then Claude.
# Muse shares the first general/routine stage; it is only a candidate while muse_auto_routing is on (see
# muse_auto_enabled), joins no design/verification/reasoning stage, and its output needs independent verification.
ROUTES = {
    "general": ((SONNET, MUSE_FULL), (FLASH,), (NATIVE_SONNET, LUNA), (QWEN_LOCAL,)),
    "routine": ((FLASH, MUSE_SPARK), (LUNA,), (SONNET,), (NATIVE_SONNET,), (QWEN_LOCAL,)),
    "design": ((SONNET,), (NATIVE_SONNET,)),
    "verification": ((LUNA,), (SONNET,), (NATIVE_SONNET,)),
    "reasoning": ((ARGON,), (AGY_OPUS,), (SOL, NATIVE_OPUS)),
}


def muse_auto_enabled() -> bool:
    """Muse joins auto routing only after execution is enabled and auto routing is switched on explicitly."""
    settings = get_settings()
    return settings.muse_enabled and settings.muse_auto_routing


def _routable(engine: str) -> bool:
    return engine != "muse" or muse_auto_enabled()


def is_flash_model(model: str | None) -> bool:
    return bool(model and (model.startswith("gemini-3.8-flash") or "qwen" in model.lower()))


def is_muse_model(model: str | None) -> bool:
    return bool(model and model.startswith("muse-"))


def needs_independent_verification(task_type: str | None, model: str | None) -> bool:
    """Flash, local Qwen and Muse implementation/routine runs require independent cross-provider verification before final completion.

    Muse has no measured quality baseline against the other workers yet, so its results are treated like Flash's.
    """
    return (is_flash_model(model) or is_muse_model(model)) and (task_type or "general") in {"general", "routine"}


def verifier_recommendation(model: str | None = None) -> dict:
    return {
        "task_type": "verification",
        "preferred_routes": [
            {"engine": engine, "model": model_id}
            for stage in ROUTES["verification"]
            for engine, model_id in stage
        ],
        "excluded_family": "muse" if is_muse_model(model) else "gemini-3.8-flash",
    }


def infer_task_type(model: str | None) -> TaskType:
    if model and (model in get_settings().peer_models or model.startswith(("claude-opus-5-5", "gemini-4-argon"))):
        return "reasoning"
    return "general"


def policy() -> dict:
    stages_by_task = {
        task: [[{"engine": engine, "model": model} for engine, model in stage if _routable(engine)] for stage in stages]
        for task, stages in ROUTES.items()
    }
    return {task: [stage for stage in stages if stage] for task, stages in stages_by_task.items()}


def accepts_model(task_type: str, model: str) -> bool:
    engine = profiles.model_engine(model)
    family = model.rsplit("-", 1)[0] if engine == "antigravity" else model
    return any((engine, family) == (e, m.rsplit("-", 1)[0] if e == "antigravity" else m)
               for stage in ROUTES[task_type] for e, m in stage if _routable(e))


def primary_target(session_engine: str, requested_model: str | None = None) -> tuple[str, str]:
    settings = get_settings()
    default_model = settings.worker_default_models[session_engine]["model"]
    if (
        requested_model
        and profiles.model_engine(requested_model) == session_engine
        and requested_model in settings.engine_models.get(session_engine, [])
    ):
        return session_engine, requested_model
    return session_engine, default_model


def same_family_counterparts(engine: str, model: str) -> tuple[tuple[str, str], ...]:
    if engine == "claude":
        return (AGY_OPUS,) if model == "opus" else (SONNET,)
    if engine == "antigravity":
        if model.startswith("claude-opus-5-5"):
            return (NATIVE_OPUS,)
        if model.startswith("claude-sonnet-5-5"):
            return (NATIVE_SONNET,)
        if model.startswith("gpt-oss-120b"):
            return (LUNA,)
    return ()


def pinned_stages(session_engine: str, requested_model: str | None = None,
                  task_type: str | None = None) -> tuple[tuple[tuple[str, str], ...], ...]:
    eng, primary_model = primary_target(session_engine, requested_model)
    effective_task = task_type if task_type in ROUTES else infer_task_type(primary_model)
    seen = {(eng, primary_model.rsplit("-", 1)[0] if eng == "antigravity" else primary_model)}
    stages: list[tuple[tuple[str, str], ...]] = [((eng, primary_model),)]
    counterparts = tuple(
        (ce, cm) for ce, cm in same_family_counterparts(eng, primary_model)
        if (ce, cm.rsplit("-", 1)[0] if ce == "antigravity" else cm) not in seen
    )
    if counterparts:
        stages.append(counterparts)
        for ce, cm in counterparts:
            seen.add((ce, cm.rsplit("-", 1)[0] if ce == "antigravity" else cm))
    for stage in ROUTES[effective_task]:
        filtered = tuple(
            (se, sm) for se, sm in stage
            if (se, sm.rsplit("-", 1)[0] if se == "antigravity" else sm) not in seen
            and _routable(se) and not (se == "muse" and eng == "muse")
        )
        if filtered:
            stages.append(filtered)
            for se, sm in filtered:
                seen.add((se, sm.rsplit("-", 1)[0] if se == "antigravity" else sm))
    return tuple(stages)


async def availability(db: AsyncSession, task_type: str, pool: str, *, model: str | None = None):
    """Expand model-aware candidates while preserving scope, enable flags, configured IDs and cooldowns."""
    settings = get_settings()
    free, blocked = [], []
    for stage in ROUTES[task_type]:
        for engine, default_model in stage:
            if not _routable(engine):
                continue
            # A requested model tunes the same family, never turns a routine task into a peer-model task.
            selected = default_model
            if model and profiles.model_engine(model) == engine:
                family = default_model.rsplit("-", 1)[0] if engine == "antigravity" else default_model
                if model == default_model or (engine == "antigravity" and model.rsplit("-", 1)[0] == family):
                    selected = model
            if selected not in settings.engine_models.get(engine, []):
                continue
            if engine == "qwen-local":
                import asyncio

                from ttakkari.runs.manager import QWEN_LOCAL_ADAPTER
                ready, _reason = await asyncio.to_thread(QWEN_LOCAL_ADAPTER.readiness)
                if ready:
                    profile_id = uuid.uuid5(uuid.NAMESPACE_URL, f"ttakkari:qwen-local:{pool}")
                    free.append(profiles.Profile(id=profile_id, name=f"Qwen local ({pool})", engine="qwen-local", model=selected))
                continue
            ready, waiting = await profiles.availability(db, engine, pool, model=selected)
            free.extend(replace(p, model=selected) for p in ready)
            blocked.extend((replace(p, model=selected), until) for p, until in waiting)
    return free, blocked


async def pinned_availability(db: AsyncSession, session_engine: str, pool: str, *,
                              model: str | None = None, task_type: str | None = None,
                              strict_engine: bool = False):
    """Availability for a pinned-engine session with optional limit fallback across routes."""
    settings = get_settings()
    stages = pinned_stages(session_engine, model, task_type)
    _, primary_model = stages[0][0]
    primary_ready, primary_waiting = await profiles.availability(db, session_engine, pool, model=primary_model)
    primary_free = [replace(p, model=primary_model) for p in primary_ready]
    primary_blocked = [(replace(p, model=primary_model), until) for p, until in primary_waiting]
    # If the pinned engine has no enabled accounts at all in this pool, or strict_engine is set,
    # do not silently substitute another engine.
    if strict_engine or (not primary_free and not primary_blocked):
        return primary_free, primary_blocked
    free = list(primary_free)
    blocked = list(primary_blocked)
    for stage in stages[1:]:
        for engine, fallback_model in stage:
            if fallback_model not in settings.engine_models.get(engine, []):
                continue
            if engine == "qwen-local":
                import asyncio

                from ttakkari.runs.manager import QWEN_LOCAL_ADAPTER
                ready, _reason = await asyncio.to_thread(QWEN_LOCAL_ADAPTER.readiness)
                if ready:
                    profile_id = uuid.uuid5(uuid.NAMESPACE_URL, f"ttakkari:qwen-local:{pool}")
                    free.append(profiles.Profile(id=profile_id, name=f"Qwen local ({pool})", engine="qwen-local", model=fallback_model))
                continue
            ready, waiting = await profiles.availability(db, engine, pool, model=fallback_model)
            free.extend(replace(p, model=fallback_model) for p in ready)
            blocked.extend((replace(p, model=fallback_model), until) for p, until in waiting)
    return free, blocked


def first_stage(candidates: list[profiles.Profile], task_type: str) -> list[profiles.Profile]:
    # Do not treat unknown capacity as confirmed spare capacity ahead of measured alternatives.
    measured = [p for p in candidates if profiles._usage_headroom(p) is not None]
    usable = measured or candidates
    for stage in ROUTES[task_type]:
        engines_and_families = {(engine, model.rsplit("-", 1)[0] if engine == "antigravity" else model)
                                for engine, model in stage}
        match = [p for p in usable if (p.engine, p.model.rsplit("-", 1)[0] if p.engine == "antigravity" else p.model)
                 in engines_and_families]
        if match:
            return match
    raise ValueError("No candidate belongs to the task policy")


def pinned_first_stage(candidates: list[profiles.Profile], session_engine: str,
                       requested_model: str | None = None,
                       task_type: str | None = None) -> list[profiles.Profile]:
    """Select the highest-priority stage for a pinned session (own engine -> same family -> task fit)."""
    stages = pinned_stages(session_engine, requested_model, task_type)
    for idx, stage in enumerate(stages):
        engines_and_families = {(engine, model.rsplit("-", 1)[0] if engine == "antigravity" else model)
                                for engine, model in stage}
        match = [p for p in candidates if (p.engine, p.model.rsplit("-", 1)[0] if p.engine == "antigravity" else p.model)
                 in engines_and_families]
        if not match:
            continue
        if idx == 0:
            return match
        measured = [p for p in match if profiles._usage_headroom(p) is not None]
        return measured or match
    raise ValueError("No candidate belongs to the pinned fallback policy")


def conversation_key(profile: profiles.Profile) -> str:
    return f"{profile.key}:{profile.model}"


def effort(profile: profiles.Profile, requested: str | None) -> str | None:
    if profile.model in {"opus", "gpt-6.1-sol"}:
        return "medium" if profile.model == "opus" else "high"
    if not supports_effort(profile.engine, profile.model):
        return None
    return requested or "high"


def capacity(profile: profiles.Profile, running: int) -> dict:
    if profile.engine == "qwen-local":
        return {
            "engine": profile.engine, "model": profile.model, "quota_group": None,
            "remaining_to_limit_percent": 100.0,
            "headroom_per_run_percent": 100.0 / (running + 1),
            "quota_known": True, "reset_at": None,
            "limited_until": None, "usage_measured_at": None,
            "usage_age_seconds": None, "quota_windows": [],
        }
    room = profiles._usage_headroom(profile)
    until = profiles.profile_limited_until(profile)
    snapshot = usage.cached(profile.id)
    group = quota_group(profile.model)
    keys = {f"{group}_5h", f"{group}_weekly"} if group else {"five_hour", "seven_day"}
    return {"engine": profile.engine, "model": profile.model, "quota_group": quota_group(profile.model),
            "remaining_to_limit_percent": room[0] if room else None,
            "headroom_per_run_percent": room[0] / (running + 1) if room else None,
            "quota_known": room is not None, "reset_at": room[1] if room and room[1] != float("inf") else None,
            "limited_until": until, "usage_measured_at": snapshot.updated_at if snapshot else None,
            "usage_age_seconds": max(0, round(time.time() - snapshot.updated_at)) if snapshot and snapshot.updated_at else None,
            "quota_windows": [{"key": w.key, "used_percent": w.percent,
                               "remaining_percent": w.remaining_percent if group else max(0, 100 - w.percent),
                               "reset_at": w.resets_at} for w in snapshot.windows if w.key in keys] if snapshot else []}


def receipt(task_type, selected, candidates, blocked, counts, *, trigger, fallback_from: dict | None = None):
    verify = needs_independent_verification(task_type, selected.model)
    out = {"policy": POLICY_VERSION, "task_type": task_type, "trigger": trigger,
           "selected": {"account": selected.name, **capacity(selected, counts.get(selected.id, 0))},
           "candidates": [{"account": p.name, **capacity(p, counts.get(p.id, 0))} for p in candidates],
           "blocked": [{"account": p.name, **capacity(p, counts.get(p.id, 0)), "limited_until": until}
                       for p, until in blocked],
           "verification_required": verify,
           "recommended_verifier": verifier_recommendation(selected.model) if verify else None,
           "reason": "task_fit_then_known_quota_then_stage_then_headroom_per_run"}
    if fallback_from is not None:
        out["fallback_from"] = fallback_from
    return out
