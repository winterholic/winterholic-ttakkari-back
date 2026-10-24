"""0070 가 모델과 같은 컬럼을 만들고 되돌릴 수 있는지 테스트 DB 트랜잭션에서 확인한다(expand 전용, nullable)."""
import importlib.util

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import CtxSource, NotebookLMJob

ADDED = {"notebooklm_jobs": ("spec",), "ctx_sources": ("index_requested_at", "index_started_at")}


async def _columns(db, table):
    rows = await db.execute(text(
        "select column_name, is_nullable from information_schema.columns where table_name=:t"), {"t": table})
    return {name: nullable for name, nullable in rows}


async def test_background_consumer_columns_are_nullable_additions_that_round_trip():
    spec = importlib.util.spec_from_file_location("bg_migration", "alembic/versions/0070_background_consumer_queue.py")
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "0069_game_build_executor_handoff"
    model_cols = {"notebooklm_jobs": set(NotebookLMJob.__table__.columns.keys()),
                  "ctx_sources": set(CtxSource.__table__.columns.keys())}
    async with sessionmaker()() as db:
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, migration.downgrade)
        for table, added in ADDED.items():
            assert not set(added) & set(await _columns(db, table)), table
        await conn.run_sync(run, migration.upgrade)
        for table, added in ADDED.items():
            cols = await _columns(db, table)
            assert set(cols) == model_cols[table], table
            assert all(cols[name] == "YES" for name in added), table  # 이전 세대 API 가 모르는 컬럼이라 nullable 이어야 한다
        await db.rollback()
