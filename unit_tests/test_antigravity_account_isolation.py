"""Unsupported account paths must never execute or report the host's identity."""
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from ttakkari.runs import profiles, usage


def test_unsupported_account_path_never_silently_uses_default_auth():
    profile = profiles.Profile(uuid4(), 'other', 'antigravity', config_dir=Path('/tmp/other'))
    with pytest.raises(ValueError, match='Antigravity.*isolat'):
        profile.env()


def test_default_profile_keeps_existing_cli_auth():
    assert profiles.Profile(uuid4(), 'default', 'antigravity').env() == {}


@pytest.mark.asyncio
async def test_unsupported_account_path_never_queries_host_quota(monkeypatch):
    fetch = AsyncMock(return_value=usage.Usage(source='antigravity_cli'))
    monkeypatch.setattr(usage, '_fetch_antigravity', fetch)
    result = await usage.usage_for(uuid4(), 'antigravity', '/tmp/other', refresh=True)
    fetch.assert_not_awaited()
    assert result.error and 'isolation' in result.error
    assert result.source is None and result.windows == [] and result.updated_at is None


@pytest.mark.asyncio
async def test_invalid_path_cannot_reuse_default_identity_cache(monkeypatch):
    aid = uuid4()
    usage._cache[aid] = (usage.time.time(), usage.Usage(source='antigravity_cli', updated_at=usage.time.time()))
    fetch = AsyncMock()
    monkeypatch.setattr(usage, '_fetch_antigravity', fetch)
    try:
        result = await usage.usage_for(aid, 'antigravity', '/tmp/other')
        assert result.error and result.source is None
        fetch.assert_not_awaited()
    finally:
        usage._cache.pop(aid, None)
