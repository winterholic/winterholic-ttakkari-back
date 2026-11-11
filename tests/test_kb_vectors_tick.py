import os

import pytest

from ttakkari import generation_service as gs
from ttakkari import kb_retrieval


@pytest.mark.asyncio
async def test_tick_drains_while_load_is_low(monkeypatch):
    calls = []

    async def fake_index(*, limit):
        calls.append(limit)
        return limit if len(calls) < 4 else 0

    monkeypatch.setattr(kb_retrieval, "index_pending", fake_index)
    monkeypatch.setattr(os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    monkeypatch.setattr(gs.asyncio, "sleep", lambda _s: _noop())
    await gs._kb_vectors()
    assert calls == [gs.KB_VECTOR_BATCH] * 4


@pytest.mark.asyncio
async def test_tick_stops_when_load_is_high(monkeypatch):
    calls = []

    async def fake_index(*, limit):
        calls.append(limit)
        return limit

    monkeypatch.setattr(kb_retrieval, "index_pending", fake_index)
    monkeypatch.setattr(os, "getloadavg", lambda: (gs.KB_VECTOR_LOAD_CEILING + 1, 0, 0))
    await gs._kb_vectors()
    assert len(calls) == 1


async def _noop():
    return None
