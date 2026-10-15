"""Read-only schema check, with a deliberate mismatch proving detection works."""

import asyncio

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Column, Integer, MetaData, text
from sqlalchemy.ext.asyncio import create_async_engine

from ttakkari.models import Base


async def main():
    engine = create_async_engine(
        "postgresql+asyncpg://localhost/ttakkari_stt_migrate_20261015"
    )
    async with engine.connect() as conn:

        def check(sync):
            options = {
                "include_object": lambda obj, name, kind, reflected, compare_to: (
                    name.startswith("local_stt_") if kind == "table" else True
                )
            }
            ctx = MigrationContext.configure(sync, opts=options)
            differences = compare_metadata(ctx, Base.metadata)
            assert not differences, differences
            negative = MetaData()
            for table in Base.metadata.sorted_tables:
                table.to_metadata(negative)
            negative.tables["local_stt_jobs"].append_column(
                Column("negative_control", Integer)
            )
            broken = compare_metadata(ctx, negative)
            assert any(
                d[0] == "add_column"
                and d[2] == "local_stt_jobs"
                and d[3].name == "negative_control"
                for d in broken
            ), broken
            print("PASS: local STT migrated schema matches metadata")
            print("PASS: deliberate missing local_stt_jobs.negative_control detected")

        await conn.run_sync(check)
        print(
            "revision:",
            await conn.scalar(text("select version_num from alembic_version")),
        )
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
