"""Real additive migration, database guardrails and downgrade preservation."""

import asyncio

import asyncpg
import pytest
from test_meeting_lineage_migration import migrate
from test_retire_clovanote_migration import migration_database  # noqa: F401


async def test_knowledge_migration_is_additive_and_guards_evidence(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(migrate, database, "0033")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        tables_before = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
        )
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, database, "head")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        assert await conn.fetchval("SELECT to_regclass('knowledge_units')") is not None
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute("""INSERT INTO knowledge_revisions(id,source_key,sha256,parser_version,origin,scope,sensitivity,active,created_at)
                VALUES(gen_random_uuid(),'negative','x','v1','text','invalid','normal',true,now())""")
    finally:
        await conn.close()
    await asyncio.to_thread(migrate, database, "0033", "downgrade")
    conn = await asyncpg.connect(database=database, host="localhost")
    try:
        assert (
            await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
            == tables_before
        )
    finally:
        await conn.close()


async def test_populated_ledger_cannot_be_destroyed_by_application_rollback(
    migration_database,  # noqa: F811
):
    import subprocess

    await asyncio.to_thread(migrate, migration_database, "head")
    conn = await asyncpg.connect(database=migration_database, host="localhost")
    try:
        await conn.execute("""INSERT INTO knowledge_revisions(id,source_key,sha256,parser_version,origin,scope,sensitivity,active,created_at)
            VALUES(gen_random_uuid(),'preserve','abc','v1','text','personal','normal',true,now())""")
        with pytest.raises(subprocess.CalledProcessError) as failure:
            await asyncio.to_thread(migrate, migration_database, "0033", "downgrade")
        assert "knowledge_ledger_contains_data" in failure.value.stderr
        assert await conn.fetchval("SELECT count(*) FROM knowledge_revisions") == 1
    finally:
        await conn.close()
