"""Account metadata must never wait on provider authentication or quota calls."""
import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest

from ttakkari.api import accounts
from ttakkari.models import EngineAccount


def row(engine="muse"):
    return EngineAccount(id=uuid.uuid4(), name=engine, engine=engine, pool="personal", config_dir=None,
                         enabled=True, priority=0, work_enabled=True, work_priority=0, max_used_percent=100)


async def test_summary_list_never_probes_identity_or_provider(monkeypatch):
    a = row()
    monkeypatch.setattr(accounts.profiles, "accounts", AsyncMock(return_value=[a]))
    def forbidden(*args):
        pytest.fail("summary must use DB metadata only")
    monkeypatch.setattr(accounts.profiles, "identity", forbidden)
    monkeypatch.setattr(accounts.profiles, "execution_ready", forbidden)
    monkeypatch.setattr(accounts.usage_mod, "usage_for", forbidden)
    db = AsyncMock()
    result = await accounts.list_accounts(None, db, details=False)
    assert result[0].name == "muse"
    assert result[0].status_pending and result[0].login_command is None
    assert not result[0].execution_enabled
    db.commit.assert_awaited_once()


async def test_legacy_list_does_not_start_usage_collection(monkeypatch):
    a = row()
    monkeypatch.setattr(accounts.profiles, "accounts", AsyncMock(return_value=[a]))
    monkeypatch.setattr(accounts, "_out", lambda a: a)
    collect = AsyncMock(side_effect=AssertionError("list must not start provider collection"))
    monkeypatch.setattr(accounts.usage_mod, "usage_for", collect)
    assert await accounts.list_accounts(None, AsyncMock()) == [a]
    collect.assert_not_awaited()


async def test_slow_account_status_does_not_block_peer_or_keep_db_transaction(monkeypatch):
    slow, fast = row(), row("codex")
    db = AsyncMock()
    monkeypatch.setattr(accounts, "_get", AsyncMock(side_effect=lambda db, aid: slow if aid == slow.id else fast))
    started, release = asyncio.Event(), asyncio.Event()
    async def usage(*args):
        db.commit.assert_awaited()
        started.set()
        await release.wait()
    monkeypatch.setattr(accounts.usage_mod, "usage_for", usage)
    monkeypatch.setattr(accounts, "_out", lambda a: a)
    pending = asyncio.create_task(accounts.account_status(slow.id, None, db))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert await asyncio.wait_for(accounts.account_status(fast.id, None, db), 1) is fast
        assert not pending.done()
    finally:
        release.set()
        await pending


async def test_status_timeout_is_reported_as_retryable_gateway_timeout(monkeypatch):
    from fastapi import HTTPException
    a = row()
    monkeypatch.setattr(accounts, "_get", AsyncMock(return_value=a))
    monkeypatch.setattr(accounts, "USAGE_REQUEST_TIMEOUT", 0.001)
    async def stuck(*args):
        await asyncio.Event().wait()
    monkeypatch.setattr(accounts.usage_mod, "usage_for", stuck)
    with pytest.raises(HTTPException) as caught:
        await accounts.account_status(a.id, None, AsyncMock())
    assert caught.value.status_code == 504
