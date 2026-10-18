"""Meeting data scope is independent from its company-first analysis account chain."""
from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.model_catalog import supports_effort
from ttakkari.runs import profiles

STAGES = (
    ("antigravity", "gemini-3.8-flash-high"),
    ("codex", "gpt-6-luna"),
    ("antigravity", "claude-sonnet-5-5-medium"),
    ("claude", "sonnet"),
)


async def availability(db: AsyncSession) -> tuple[list[profiles.Profile], list[tuple[profiles.Profile, float]]]:
    # Work mode has an independent toggle; personal mode normally includes work
    # accounts, so filter the fallback pass to avoid re-enabling a company account.
    personal_ids = {a.id for a in await profiles.accounts(db) if a.pool == "personal"}
    free, blocked = [], []
    for pool in ("work", "personal"):
        for engine, model in STAGES:
            ready, waiting = await profiles.availability(db, engine, pool, model=model)
            free.extend(replace(p, model=model) for p in ready if pool == "work" or p.id in personal_ids)
            blocked.extend((replace(p, model=model), until) for p, until in waiting
                           if pool == "work" or p.id in personal_ids)
    return free, blocked


def first_stage(candidates: list[profiles.Profile]) -> list[profiles.Profile]:
    """Balance the earliest scope/model stage without mixing company and personal accounts."""
    first = candidates[0]
    return [p for p in candidates if (p.pool, p.engine, p.model) == (first.pool, first.engine, first.model)]


def conversation_key(profile: profiles.Profile) -> str:
    # Flash and Sonnet share an account, but must retain their own provider conversation.
    return f"{profile.key}:{profile.model}"


def effort(profile: profiles.Profile) -> str | None:
    # Antigravity thinking is encoded in its model ID. Native fallbacks use medium.
    return "medium" if supports_effort(profile.engine, profile.model) else None
