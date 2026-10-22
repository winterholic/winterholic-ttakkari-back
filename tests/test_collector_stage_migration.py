"""0063 collector stage tables: the migration builds the same schema as the ORM models and round-trips.

Scoped to the five new tables because `alembic check` as a whole fails on drift that predates the collector.
"""
import importlib.util

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import Base

TABLES = (
    "collector_source_policies", "collector_host_state", "collector_fetch_cache", "collector_items",
    "collector_dedupe_bands",
)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"collector_migration_{name}", f"alembic/versions/{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _only_new(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in TABLES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLES


def test_models_are_registered_in_target_metadata():
    assert set(TABLES) <= set(Base.metadata.tables)


async def test_stage_migration_matches_models_adds_only_new_tables_and_round_trips():
    mig = _load("0063_collector_stage_items")
    assert mig.down_revision == "0062_delivery_change_sets"
    async with sessionmaker()() as db:
        before = {r[0] for r in await db.execute(text(
            "select column_name from information_schema.columns where table_name = 'collector_jobs'"))}
        for table in reversed(TABLES):
            await db.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        def drift(sync):
            ctx = MigrationContext.configure(sync, opts={"compare_type": True, "include_object": _only_new})
            return list(compare_metadata(ctx, Base.metadata))

        await conn.run_sync(run, mig.upgrade)
        assert await conn.run_sync(drift) == []
        after = {r[0] for r in await db.execute(text(
            "select column_name from information_schema.columns where table_name = 'collector_jobs'"))}
        assert after == before, "online-safe: collector_jobs is untouched"
        fks = {r[0] for r in await db.execute(text(
            "select confrelid::regclass::text from pg_constraint where contype = 'f' and conrelid in "
            "('collector_items'::regclass, 'collector_dedupe_bands'::regclass)"))}
        assert fks == {"collector_jobs", "collector_items"}

        await conn.run_sync(run, mig.downgrade)
        for table in TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is None
        await conn.run_sync(run, mig.upgrade)
        assert await conn.run_sync(drift) == []
        uniques = {r[0] for r in await db.execute(text(
            "select conname from pg_constraint where contype = 'u' and conrelid = 'collector_items'::regclass"))}
        assert uniques == {"uq_collector_items_job_url"}
        await db.commit()
