"""0047_game_studio: 신규 표만 추가하는 online-safe migration, 제약, downgrade 복원 (TASK-G05)."""

import asyncio

import asyncpg
import pytest
from test_meeting_lineage_migration import migrate
from test_retire_clovanote_migration import migration_database  # noqa: F401

GAME_TABLES = {"game_projects", "game_builds", "game_feedbacks"}


async def _tables(database: str) -> list[str]:
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        rows = await conn.fetch("SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename")
        return [r["tablename"] for r in rows]
    finally:
        await conn.close()


async def test_game_migration_is_additive_and_downgrades_cleanly(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(migrate, database, "0046_notebooklm")
    before = await _tables(database)
    assert not GAME_TABLES & set(before)
    await asyncio.to_thread(migrate, database, "head")
    after = await _tables(database)
    assert GAME_TABLES <= set(after)
    assert set(before) <= set(after)  # 기존 표는 하나도 사라지지 않는다
    await asyncio.to_thread(migrate, database, "0046_notebooklm", "downgrade")
    assert await _tables(database) == before


async def test_game_tables_enforce_uniqueness_and_cascade(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(migrate, database, "head")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        ws = await conn.fetchval(
            "INSERT INTO workspaces(id,name,root_path,is_git,default_engine,codex_sandbox,extra_writable_roots,export_allowed,created_at) VALUES(gen_random_uuid(),'w','/tmp/g1',false,'claude','workspace-write','[]',true,now()) RETURNING id"
        )
        game = await conn.fetchval(
            "INSERT INTO game_projects(id,slug,name,engine_id,workspace_id,created_at) "
            "VALUES(gen_random_uuid(),'demo-game','d','web-phaser',$1,now()) RETURNING id",
            ws,
        )
        with pytest.raises(asyncpg.UniqueViolationError):  # 한 workspace 는 게임 하나에만 연결
            await conn.execute(
                "INSERT INTO game_projects(id,slug,name,engine_id,workspace_id,created_at) "
                "VALUES(gen_random_uuid(),'other-game','o','web-phaser',$1,now())",
                ws,
            )
        await conn.execute("INSERT INTO game_builds(id,game_id,seq,created_at) VALUES(gen_random_uuid(),$1,1,now())", game)
        with pytest.raises(asyncpg.UniqueViolationError):  # 게임별 seq 는 유일
            await conn.execute("INSERT INTO game_builds(id,game_id,seq,created_at) VALUES(gen_random_uuid(),$1,1,now())", game)
        with pytest.raises(asyncpg.ForeignKeyViolationError):  # workspace 삭제는 게임이 있는 동안 거부
            await conn.execute("DELETE FROM workspaces WHERE id=$1", ws)
        await conn.execute("DELETE FROM game_projects WHERE id=$1", game)
        assert await conn.fetchval("SELECT count(*) FROM game_builds") == 0
    finally:
        await conn.close()
