from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from context_gateway.auth import AuthenticationError, exchange_key, issue_key
from context_gateway.models import APIKey, KeyACL, Project


async def _save_key(db_sessions, raw_key: str, key_hash: str, expires_at: datetime) -> None:
    async with db_sessions() as session:
        project = Project(
            slug=f"synthetic-key-exchange-{uuid4().hex}", name="Synthetic Key Exchange"
        )
        key = APIKey(label="synthetic test", credential_hash=key_hash, expires_at=expires_at)
        session.add_all([project, key])
        await session.flush()
        session.add(KeyACL(key_id=key.id, project_id=project.id, scopes=["read"]))
        await session.commit()


@pytest.mark.asyncio
async def test_only_one_concurrent_exchange_can_consume_a_key(db_sessions) -> None:
    now = datetime.now(UTC)
    raw_key, key_hash, expires_at = issue_key(now=now)
    await _save_key(db_sessions, raw_key, key_hash, expires_at)

    async def exchange() -> bool:
        async with db_sessions() as session:
            try:
                await exchange_key(session, raw_key, now=now)
                await session.commit()
                return True
            except AuthenticationError:
                await session.rollback()
                return False

    assert sum(await asyncio.gather(exchange(), exchange())) == 1


@pytest.mark.asyncio
async def test_expired_and_reused_keys_cannot_exchange(db_sessions) -> None:
    now = datetime.now(UTC)
    raw_key, key_hash, expires_at = issue_key(now=now - timedelta(hours=2))
    await _save_key(db_sessions, raw_key, key_hash, expires_at)

    async with db_sessions() as session:
        with pytest.raises(AuthenticationError):
            await exchange_key(session, raw_key, now=now)

    live_key, live_hash, live_expiry = issue_key(now=now)
    await _save_key(db_sessions, live_key, live_hash, live_expiry)
    async with db_sessions() as session:
        await exchange_key(session, live_key, now=now)
        await session.commit()
    async with db_sessions() as session:
        with pytest.raises(AuthenticationError):
            await exchange_key(session, live_key, now=now)


@pytest.mark.asyncio
async def test_session_expiry_is_capped_at_the_original_key_expiry(db_sessions) -> None:
    now = datetime.now(UTC)
    issued_at = now - timedelta(minutes=59, seconds=30)
    raw_key, key_hash, key_expiry = issue_key(now=issued_at)
    await _save_key(db_sessions, raw_key, key_hash, key_expiry)

    async with db_sessions() as session:
        _, session_expiry = await exchange_key(session, raw_key, now=now)
        await session.commit()

    assert session_expiry == key_expiry


@pytest.mark.asyncio
async def test_reusable_v2_key_can_exchange_repeatedly(db_sessions) -> None:
    now = datetime.now(UTC)
    raw_key, key_hash, _expires_at = issue_key(now=now)
    async with db_sessions() as session:
        project = Project(slug=f"reusable-{uuid4().hex}", name="Reusable")
        key = APIKey(
            label="reusable fixture",
            credential_hash=key_hash,
            expires_at=now + timedelta(days=3),
            credential_type="reusable_api_v2",
        )
        session.add_all([project, key])
        await session.flush()
        session.add(KeyACL(key_id=key.id, project_id=project.id, scopes=["read"]))
        await session.commit()

    async with db_sessions() as session:
        first, _ = await exchange_key(session, raw_key, now=now)
        await session.commit()
    async with db_sessions() as session:
        second, _ = await exchange_key(session, raw_key, now=now)
        await session.commit()

    assert first != second
