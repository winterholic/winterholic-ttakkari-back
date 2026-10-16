"""One task policy for auto workers; native main/desk and meetings keep their own contracts.

Task fit precedes account load. Unknown capacity stays unknown; stale exhausted pools
are still blocked by profiles. Explicit engines remain locks for tool requirements.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.config import get_settings
from ttakkari.model_catalog import quota_group, thinking_level
from ttakkari.runs import profiles, usage

TaskType = Literal["general", "routine", "design", "verification", "reasoning"]
POLICY_VERSION = "worker-routing-v1"
SONNET = ("antigravity", "claude-sonnet-5-5-high")
FLASH = ("antigravity", "gemini-3.8-flash-high")
LUNA = ("codex", "gpt-6-luna")
NATIVE_SONNET = ("claude", "sonnet")
# Each stage has equally suitable models; profiles.choose balances accounts within it.
ROUTES = {
    "general": ((SONNET,), (FLASH,), (NATIVE_SONNET, LUNA)),
    "routine": ((FLASH,), (LUNA,), (SONNET,), (NATIVE_SONNET,)),
    "design": ((SONNET,), (NATIVE_SONNET,)),
    "verification": ((LUNA,), (SONNET,), (FLASH,), (NATIVE_SONNET,)),
    "reasoning": ((("antigravity", "gemini-4-argon-high"),),
                  (("antigravity", "claude-opus-5-5-medium"),),
                  (("codex", "gpt-6.1-sol"), ("claude", "opus"))),
}


def infer_task_type(model: str | None) -> TaskType:
    if model in get_settings().peer_models:
        return "reasoning"
    return "general"


def policy() -> dict:
    return {task: [[{"engine": engine, "model": model} for engine, model in stage] for stage in stages]
            for task, stages in ROUTES.items()}


def accepts_model(task_type: str, model: str) -> bool:
    engine = profiles.model_engine(model)
    family = model.rsplit("-", 1)[0] if engine == "antigravity" else model
    return any((engine, family) == (e, m.rsplit("-", 1)[0] if e == "antigravity" else m)
               for stage in ROUTES[task_type] for e, m in stage)


async def availability(db: AsyncSession, task_type: str, pool: str, *, model: str | None = None):
    """Expand model-aware candidates while preserving scope, enable flags, configured IDs and cooldowns."""
    settings = get_settings()
    free, blocked = [], []
    for stage in ROUTES[task_type]:
        for engine, default_model in stage:
            # A requested model tunes the same family, never turns a routine task into a peer-model task.
            selected = default_model
            if model and profiles.model_engine(model) == engine:
                family = default_model.rsplit("-", 1)[0] if engine == "antigravity" else default_model
                if model == default_model or (engine == "antigravity" and model.rsplit("-", 1)[0] == family):
                    selected = model
            if selected not in settings.engine_models.get(engine, []):
                continue
            ready, waiting = await profiles.availability(db, engine, pool, model=selected)
            free.extend(replace(p, model=selected) for p in ready)
            blocked.extend((replace(p, model=selected), until) for p, until in waiting)
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


def conversation_key(profile: profiles.Profile) -> str:
    return f"{profile.key}:{profile.model}"


def effort(profile: profiles.Profile, requested: str | None) -> str:
    if profile.model in {"opus", "gpt-6.1-sol"}:
        return "medium" if profile.model == "opus" else "high"
    return thinking_level(profile.model) if profile.engine == "antigravity" else (requested or "high")


def capacity(profile: profiles.Profile, running: int) -> dict:
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
                               "remaining_percent": w.remaining_percent if group else max(0, 100-w.percent),
                               "reset_at": w.resets_at} for w in snapshot.windows if w.key in keys] if snapshot else []}


def receipt(task_type, selected, candidates, blocked, counts, *, trigger):
    return {"policy": POLICY_VERSION, "task_type": task_type, "trigger": trigger,
            "selected": {"account": selected.name, **capacity(selected, counts.get(selected.id, 0))},
            "candidates": [{"account": p.name, **capacity(p, counts.get(p.id, 0))} for p in candidates],
            "blocked": [{"account": p.name, **capacity(p, counts.get(p.id, 0)), "limited_until": until}
                        for p, until in blocked],
            "reason": "task_fit_then_known_quota_then_stage_then_headroom_per_run"}
