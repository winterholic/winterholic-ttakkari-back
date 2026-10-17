"""Run-manager event path, using the real Muse parser and usage sink."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

from ttakkari.agents.base import ParseState
from ttakkari.agents.muse import MuseAdapter
from ttakkari.runs import usage, usage_store
from ttakkari.runs.manager import manager


async def test_account_wide_event_reaches_usage_history(client, auth, monkeypatch, tmp_path):
    created = await client.post('/api/accounts', headers=auth, json={'name': 'Pump Muse', 'engine': 'muse'})
    aid = UUID(created.json()['id'])
    stamp = int(time.time()*1000)
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps({'method': 'usage/changed', 'params': {
        'tier': 'power', 'observedAtMs': stamp,
        'window': {'usedPercent': 7, 'resetsAtMs': stamp+3600000},
        'weekly': {'usedPercent': 15, 'resetsAtMs': stamp+604800000},
    }}) + '\n').encode())
    reader.feed_eof()
    # Event storage is irrelevant to this path; the usage sink still writes
    # the disposable test DB and is read back through the public usage API.
    monkeypatch.setattr('ttakkari.runs.manager.events.append', lambda *_: None)
    monkeypatch.setattr(manager, '_commit', AsyncMock())
    usage_store.install()
    await manager._pump(None, None, SimpleNamespace(stdout=reader), MuseAdapter(), ParseState(), tmp_path, aid)
    await usage_store.flush()
    assert usage.cached(aid).windows[0].percent == 7
    assert usage.cached(aid).updated_at == stamp/1000
