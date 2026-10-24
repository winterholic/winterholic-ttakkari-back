"""A suspended TTS tick must recheck claim admission after awaits."""
import asyncio
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ttakkari import generation
from ttakkari.tts import jobs


@pytest.mark.parametrize("stage", ["refresh", "queue", "budget"])
async def test_suspended_tts_tick_does_not_claim_after_stop(monkeypatch, stage):
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(at, result):
        if stage == at:
            entered.set()
            await release.wait()
        return result

    @asynccontextmanager
    async def budget(*args, **kwargs):
        yield await pause("budget", "ok")

    runtime = SimpleNamespace(id=uuid.uuid4(), refresh=lambda: pause("refresh", "active"))
    monkeypatch.setattr(generation, "current", runtime)
    monkeypatch.setattr(jobs, "_draining", False)
    monkeypatch.setattr(jobs, "_housekeeping", AsyncMock())
    monkeypatch.setattr(jobs, "_has_owned_queued", lambda *args, **kwargs: pause("queue", True))
    monkeypatch.setattr(jobs.service, "budget", budget)
    claim = AsyncMock(return_value=None)
    monkeypatch.setattr(jobs, "_claim", claim)
    tick = asyncio.create_task(jobs.tick())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        jobs.stop_claiming()
        release.set()
        assert await tick is False
        claim.assert_not_awaited()
    finally:
        release.set()
        await asyncio.gather(tick, return_exceptions=True)
