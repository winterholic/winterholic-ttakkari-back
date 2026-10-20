"""Disposable test environment; imports the project's isolated test configuration."""

import asyncio
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import conftest as fixture
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.config import get_settings
from ttakkari.models import Base, Credential
from ttakkari.security.passwords import hash_password


async def prepare():
    await fixture._use_process_database()
    engine = create_async_engine(get_settings().database_url)
    async with engine.begin() as connection:
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await connection.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            Credential.__table__.insert().values(
                id=1, password_hash=hash_password("research-lab-test-only-2026")
            )
        )
    await engine.dispose()


asyncio.run(prepare())
import uvicorn

from ttakkari.main import create_app

get_settings().cors_origins = ["http://127.0.0.1:18792"]
app = create_app()
try:
    uvicorn.run(app, host="127.0.0.1", port=18791, log_level="warning")
finally:
    asyncio.run(fixture._drop_process_database())
