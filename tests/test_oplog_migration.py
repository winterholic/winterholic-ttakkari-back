"""0039 마이그레이션이 ORM 모델과 같은 스키마를 만들고 되돌릴 수 있는지 격리 DB 트랜잭션에서 확인한다."""
import importlib.util

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import Base

TABLES = ("ops_issues", "ops_events", "ops_issue_stats", "ops_ingest_cursors")


async def test_ops_log_migration_matches_models_and_round_trips():
    spec = importlib.util.spec_from_file_location("ops_migration", "alembic/versions/0039_ops_log_issues.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0038_qwen_flash_usage"
    async with sessionmaker()() as db:
        for table in reversed(TABLES):
            await db.execute(text(f"DROP TABLE {table} CASCADE"))
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, migration.upgrade)
        for table in TABLES:
            cols = {r[0] for r in await db.execute(text(
                "select column_name from information_schema.columns where table_name=:t"), {"t": table})}
            assert cols == {c.name for c in Base.metadata.tables[table].columns}, table
            indexes = {r[0] for r in await db.execute(text("select indexname from pg_indexes where tablename=:t"), {"t": table})}
            for index in Base.metadata.tables[table].indexes:
                assert index.name in indexes, (table, index.name)
        await conn.run_sync(run, migration.downgrade)
        for table in TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is None
        await conn.run_sync(run, migration.upgrade)
        await db.rollback()
