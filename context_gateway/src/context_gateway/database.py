from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from .config import Settings


def make_session_factory(settings: Settings) -> async_sessionmaker[AsyncSession]:
    if not settings.database_url.startswith("postgresql+asyncpg://"):
        raise ValueError("CONTEXT_GATEWAY_DATABASE_URL must use PostgreSQL via asyncpg")
    engine = create_async_engine(settings.database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)


async def get_session(factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[AsyncSession]:
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
