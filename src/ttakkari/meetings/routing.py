"""Meeting data scope is independent from its personal analysis account chain."""
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.runs import profiles

STAGES = (
    ("codex", "gpt-6-luna"),
    ("antigravity", "gemini-3.8-flash-high"),
    ("antigravity", "claude-sonnet-5-5-high"),
    ("claude", "sonnet"),
)


async def availability(db: AsyncSession) -> tuple[list[profiles.Profile], list[tuple[profiles.Profile, float]]]:
    # Personal mode normally includes work accounts. Meetings explicitly use personal accounts only.
    personal_ids = {a.id for a in await profiles.accounts(db) if a.pool == "personal"}
    free, blocked = [], []
    for engine, model in STAGES:
        ready, waiting = await profiles.availability(db, engine, "personal", model=model)
        free.extend(replace(p, model=model) for p in ready if p.id in personal_ids)
        blocked.extend((replace(p, model=model), until) for p, until in waiting if p.id in personal_ids)
    return free, blocked


def first_stage(candidates: list[profiles.Profile]) -> list[profiles.Profile]:
    """Balance accounts within the earliest available model, preserving the requested chain."""
    first = candidates[0]
    return [p for p in candidates if (p.engine, p.model) == (first.engine, first.model)]


def conversation_key(profile: profiles.Profile) -> str:
    # Flash and Sonnet share an account, but must retain their own provider conversation.
    return f"{profile.key}:{profile.model}"
