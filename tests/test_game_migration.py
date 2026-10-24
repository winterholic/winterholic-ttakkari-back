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


async def _columns(database: str, table: str) -> set[str]:
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        rows = await conn.fetch("SELECT column_name FROM information_schema.columns WHERE table_name=$1", table)
        return {r["column_name"] for r in rows}
    finally:
        await conn.close()


async def test_0048_adds_only_nullable_columns_and_downgrades_cleanly(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(migrate, database, "0047_game_studio")
    projects_before, builds_before = await _columns(database, "game_projects"), await _columns(database, "game_builds")
    assert "active_build_id" not in projects_before and "restored_from_id" not in builds_before
    conn = await asyncpg.connect(database=database, host="localhost")
    try:  # 0047 상태에서 만든 행이 0048 뒤에도 그대로 읽힌다(기존 세대 코드와 호환되는 추가형 변경)
        ws = await conn.fetchval(
            "INSERT INTO workspaces(id,name,root_path,is_git,default_engine,codex_sandbox,extra_writable_roots,export_allowed,created_at) VALUES(gen_random_uuid(),'w','/tmp/g48',false,'claude','workspace-write','[]',true,now()) RETURNING id")
        game = await conn.fetchval(
            "INSERT INTO game_projects(id,slug,name,engine_id,workspace_id,created_at) VALUES(gen_random_uuid(),'mig-game','d','web-phaser',$1,now()) RETURNING id", ws)
        await conn.execute("INSERT INTO game_builds(id,game_id,seq,created_at) VALUES(gen_random_uuid(),$1,1,now())", game)
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, database, "0048_game_active_build")
    assert await _columns(database, "game_projects") == projects_before | {"active_build_id"}
    assert await _columns(database, "game_builds") == builds_before | {"restored_from_id"}
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        assert await conn.fetchval("SELECT active_build_id FROM game_projects WHERE id=$1", game) is None
        build = await conn.fetchval("SELECT id FROM game_builds WHERE game_id=$1", game)
        child = await conn.fetchval("INSERT INTO game_builds(id,game_id,seq,restored_from_id,created_at) VALUES(gen_random_uuid(),$1,2,$2,now()) RETURNING id", game, build)
        await conn.execute("DELETE FROM game_builds WHERE id=$1", build)  # 복구 출처 빌드가 지워져도 복구 빌드는 남는다
        assert await conn.fetchval("SELECT restored_from_id FROM game_builds WHERE id=$1", child) is None
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, database, "0047_game_studio", "downgrade")
    assert await _columns(database, "game_projects") == projects_before and await _columns(database, "game_builds") == builds_before
