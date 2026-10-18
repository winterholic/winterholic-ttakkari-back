"""Owner opt-in when operator policy is unspecified; explicit env policy wins.

Only the owner API writes this setting. Agent tokens/MCP cannot activate it.
Readiness, model allowlists, scope and sandbox enforcement remain independent.
"""
from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.agents.qwen_local import QwenLocalAdapter
from ttakkari.config import get_settings
from ttakkari.models import AppSetting

KEY = "qwen_worker_activation"


def config_locked() -> bool:
    return "qwen_local_enabled" in get_settings().model_fields_set


async def adapter_for(db: AsyncSession, base: QwenLocalAdapter | None = None) -> QwenLocalAdapter:
    settings = get_settings()
    base = base if base is not None else QwenLocalAdapter(
        worker_path=settings.qwen_local_worker_path, enabled=settings.qwen_local_enabled,
        turns=settings.qwen_local_turns)
    if config_locked():
        return base
    row = await db.get(AppSetting, KEY, populate_existing=True)
    if row is None or row.value.get("enabled") is not True:
        return base
    # Per-attempt adapter: changing settings never mutates a running attempt.
    return QwenLocalAdapter(worker_path=base.worker_path, enabled=True, turns=base.turns)


async def status(db: AsyncSession) -> dict:
    adapter = await adapter_for(db)
    ready, reason = await asyncio.to_thread(adapter.readiness)
    return {"enabled": adapter.enabled, "config_locked": config_locked(), "ready": ready, "reason": reason}
