"""0069: 게임 빌드 실행부 인계 표식은 nullable 열 두 개만 더하고, 기존 행은 NULL(실행부가 집지 않음)로 남는다."""

import asyncio
import os
import subprocess
import uuid

import asyncpg
from sqlalchemy.engine import make_url
from test_retire_clovanote_migration import ROOT, migration_database  # noqa: F401


def database_url(database: str) -> str:
    """시험 클러스터의 계정·포트를 그대로 쓰고 DB 이름만 전용 이주 시험 DB 로 바꾼다."""
    return make_url(os.environ["TTAKKARI_TEST_DB"]).set(database=database).render_as_string(hide_password=False)


def alembic(database: str, *args: str) -> None:
    done = subprocess.run(
        [str(ROOT / ".venv/bin/alembic"), *args], cwd=ROOT, check=False, capture_output=True, text=True,
        env={**os.environ, "TTAKKARI_NO_ENV_FILE": "1",
             "TTAKKARI_DATABASE_URL": database_url(database)},
    )
    assert done.returncode == 0, done.stderr[-1500:]


async def columns(database: str) -> dict[str, tuple[str, str]]:
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        rows = await conn.fetch(
            "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name='game_builds' AND column_name IN ('queue_state', 'claimed_at')")
        return {r["column_name"]: (r["is_nullable"], r["data_type"]) for r in rows}
    finally:
        await conn.close()


async def test_0069_adds_only_nullable_columns_and_downgrades_cleanly(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(alembic, database, "upgrade", "0068_tts_payload_chunks")
    assert await columns(database) == {}

    await asyncio.to_thread(alembic, database, "upgrade", "0069_game_build_executor_handoff")
    assert await columns(database) == {"queue_state": ("YES", "character varying"), "claimed_at": ("YES", "timestamp with time zone")}

    await asyncio.to_thread(alembic, database, "downgrade", "0068_tts_payload_chunks")
    assert await columns(database) == {}
    uuid.UUID(database.removeprefix("ttk_retire_"))  # 전용 DB 이름 확인
