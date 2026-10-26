"""Mission migration matches ORM without rewriting acquisition or KB evidence."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from test_collector_stage_migration import _load

from ttakkari.db import sessionmaker
from ttakkari.models import Base

TABLES = {"collection_missions", "collection_mission_jobs", "collection_mission_runs"}


async def test_mission_schema_matches_models_and_roundtrips_in_isolated_database():
    base = _load("0072_collection_missions")
    runs = _load("0073_collection_mission_runs")
    assert base.down_revision == "0071_library_platform_v2_merge"
    assert runs.down_revision == "0072_collection_missions"

    def upgrade():
        base.upgrade()
        runs.upgrade()

    def downgrade():
        runs.downgrade()
        base.downgrade()

    async with sessionmaker()() as db:
        for table in ("collection_mission_runs", "collection_mission_jobs", "collection_missions"):
            await db.execute(text(f"DROP TABLE {table}"))
        connection = await db.connection()

        def migrate(sync, operation):
            with Operations.context(MigrationContext.configure(sync)):
                operation()

        def include(obj, name, kind, reflected, compare_to):
            if kind == "table":
                return name in TABLES
            table = getattr(obj, "table", None)
            return table is None or table.name in TABLES

        def drift(sync):
            return compare_metadata(MigrationContext.configure(sync, opts={"compare_type": True, "include_object": include}), Base.metadata)

        await connection.run_sync(migrate, upgrade)
        assert await connection.run_sync(drift) == []
        await connection.run_sync(migrate, downgrade)
        assert await db.scalar(text("select to_regclass('collection_missions')")) is None
        await connection.run_sync(migrate, upgrade)
        assert await connection.run_sync(drift) == []
        await db.commit()


async def test_run_migration_backfills_one_initial_run_per_existing_mission():
    runs = _load("0073_collection_mission_runs")
    async with sessionmaker()() as db:
        connection = await db.connection()

        def migrate(sync, operation):
            with Operations.context(MigrationContext.configure(sync)):
                operation()

        await connection.run_sync(migrate, runs.downgrade)
        await db.execute(text(
            "INSERT INTO collection_missions (id, tenant_id, request_key, input_hash, goal, plan, status, gaps, created_at, updated_at) "
            "VALUES ('00000000-0000-0000-0000-000000000072', 'owner', 'legacy', 'h', 'legacy', "
            "'{\"budget\": {\"pages\": 7}}'::jsonb, 'running', '[]'::jsonb, now(), now())"))
        await connection.run_sync(migrate, runs.upgrade)
        row = (await db.execute(text("select run_seq, trigger, page_budget, extra_pages from collection_mission_runs"))).one()
        assert tuple(row) == (1, "initial", 7, 0)
        assert await db.scalar(text("select current_run_seq from collection_missions")) == 1
        await db.commit()
