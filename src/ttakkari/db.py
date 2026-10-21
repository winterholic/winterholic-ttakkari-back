from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session

from ttakkari.config import get_settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


@event.listens_for(Session, "before_flush")
def _fence_flush(session, _flush_context, _instances) -> None:
    """Implicit query autoflush must not precede an attempt's ownership check.

    SQLAlchemy runs this synchronous hook within AsyncSession's greenlet. The
    row lock lives in the same transaction as all pending ORM writes.
    """
    frame = session.info.get("runtime_lease")
    if frame is None:
        return
    from ttakkari.generation import OwnershipLost
    from ttakkari.models import RuntimeLease
    runtime, run_id, token = frame
    row = session.connection().execute(select(RuntimeLease.generation_id, RuntimeLease.token)
        .where(RuntimeLease.run_id == run_id).with_for_update()).first()
    if row is None or row.generation_id != runtime.id or row.token != token:
        raise OwnershipLost(str(run_id))


def init_engine(url: str | None = None) -> AsyncEngine:
    global _engine, _sessionmaker
    s = get_settings()
    _engine = create_async_engine(url or s.database_url, pool_pre_ping=True,
                                  pool_size=s.database_pool_size, max_overflow=s.database_pool_overflow)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        init_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_db() -> AsyncIterator[AsyncSession]:
    async with sessionmaker()() as session:
        yield session
