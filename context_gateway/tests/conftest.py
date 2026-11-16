from __future__ import annotations

import getpass
import os
import socket
import subprocess
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from context_gateway.models import Base


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    configured_url = os.environ.get("CONTEXT_GATEWAY_TEST_DATABASE_URL")
    if configured_url:
        database_name = urlsplit(configured_url).path.strip("/").lower()
        if not configured_url.startswith("postgresql+asyncpg://") or not (
            database_name.endswith("_test") or database_name.startswith("test_")
        ):
            pytest.fail(
                "CONTEXT_GATEWAY_TEST_DATABASE_URL must use asyncpg and a database named as a test DB"
            )
        yield configured_url
        return
    with tempfile.TemporaryDirectory(prefix="context-gateway-pg-") as root:
        data_dir = Path(root) / "data"
        socket_dir = Path(root) / "socket"
        socket_dir.mkdir()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        binary_dir = Path("/opt/homebrew/opt/postgresql@17/bin")
        initdb = binary_dir / "initdb"
        pg_ctl = binary_dir / "pg_ctl"
        if not initdb.exists() or not pg_ctl.exists():
            pytest.skip("isolated PostgreSQL 17 binaries are unavailable")
        try:
            subprocess.run(
                [
                    str(initdb),
                    "-D",
                    str(data_dir),
                    "-A",
                    "trust",
                    "--no-locale",
                    "--encoding=UTF8",
                    "-c",
                    "dynamic_shared_memory_type=mmap",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr or exc.stdout or str(exc)
            if "Operation not permitted" in detail and "shared memory segment" in detail:
                pytest.skip(
                    "isolated PostgreSQL unavailable in command sandbox: SysV shmget Operation not permitted"
                )
            raise
        subprocess.run(
            [
                str(pg_ctl),
                "-D",
                str(data_dir),
                "-o",
                f"-h 127.0.0.1 -p {port} -k {socket_dir} -F",
                "-w",
                "start",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        try:
            yield f"postgresql+asyncpg://{getpass.getuser()}@127.0.0.1:{port}/postgres"
        finally:
            subprocess.run(
                [str(pg_ctl), "-D", str(data_dir), "-m", "fast", "-w", "stop"],
                check=False,
                capture_output=True,
            )


@pytest_asyncio.fixture(autouse=True)
async def isolate_test_database(postgres_url: str) -> None:
    engine = create_async_engine(postgres_url)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_sessions(postgres_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()
