"""0059 collector 마이그레이션이 ORM 모델과 같은 스키마를 만들고 왕복하는지 일회용 테스트 DB 에서 확인한다.

`alembic check` 전체는 collector 와 무관한 기존 드리프트(ctx_*, weather_briefings 등) 때문에 실패하므로,
collector 테이블로 한정한 autogenerate diff 가 비어 있음을 여기서 고정한다.
"""
import importlib.util

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import Base

TABLES = ("collector_jobs", "collector_outbox")


def _load(name):
    spec = importlib.util.spec_from_file_location(f"collector_migration_{name}", f"alembic/versions/{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _only_collector(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name in TABLES
    table = getattr(obj, "table", None)
    return table is None or table.name in TABLES


def test_collector_tables_are_in_alembic_target_metadata():
    assert set(TABLES) <= set(Base.metadata.tables)


async def test_collector_migration_matches_models_and_round_trips():
    migration = _load("0059_collector_intake")
    assert migration.down_revision == "0058_kb_immutability_truncate"
    async with sessionmaker()() as db:
        for table in reversed(TABLES):
            await db.execute(text(f"DROP TABLE {table} CASCADE"))
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        def drift(sync):
            context = MigrationContext.configure(
                sync, opts={"compare_type": True, "include_object": _only_collector})
            return [d for d in compare_metadata(context, Base.metadata)]

        await conn.run_sync(run, migration.upgrade)
        assert await conn.run_sync(drift) == []

        await conn.run_sync(run, migration.downgrade)
        for table in TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is None

        await conn.run_sync(run, migration.upgrade)
        assert await conn.run_sync(drift) == []
        names = {r[0] for r in await db.execute(text(
            "select conname from pg_constraint where conrelid in ('collector_jobs'::regclass, 'collector_outbox'::regclass)"))}
        assert {"uq_collector_jobs_idempotency", "uq_collector_outbox_tenant_sink_envelope"} <= names
        await db.commit()
