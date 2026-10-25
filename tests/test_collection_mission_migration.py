"""Mission migration matches ORM without rewriting acquisition or KB evidence."""
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from test_collector_stage_migration import _load

from ttakkari.db import sessionmaker
from ttakkari.models import Base

TABLES = {"collection_missions", "collection_mission_jobs"}


async def test_mission_schema_matches_models_and_roundtrips_in_isolated_database():
    migration = _load("0072_collection_missions")
    assert migration.down_revision == "0071_library_platform_v2_merge"
    async with sessionmaker()() as db:
        for table in ("collection_mission_jobs", "collection_missions"):
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

        await connection.run_sync(migrate, migration.upgrade)
        assert await connection.run_sync(drift) == []
        await connection.run_sync(migrate, migration.downgrade)
        assert await db.scalar(text("select to_regclass('collection_missions')")) is None
        await connection.run_sync(migrate, migration.upgrade)
        assert await connection.run_sync(drift) == []
        await db.commit()
