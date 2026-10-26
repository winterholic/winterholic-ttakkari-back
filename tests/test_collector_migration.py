"""0059·0060 collector 마이그레이션이 ORM 모델과 같은 스키마를 만들고 왕복하는지 일회용 테스트 DB 에서 확인한다.

`alembic check` 전체는 collector 와 무관한 기존 드리프트(ctx_*, weather_briefings 등) 때문에 실패하므로,
collector 테이블로 한정한 autogenerate diff 가 비어 있음을 여기서 고정한다.
"""
import importlib.util
import re
from pathlib import Path

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


async def test_collector_migrations_match_models_and_round_trip():
    intake = _load("0059_collector_intake")
    runtime = _load("0060_collector_job_runtime")
    retry = _load("0067_collector_outbox_retry")
    single_lib = _load("0074_collection_single_library")
    assert intake.down_revision == "0058_kb_immutability_truncate"
    assert runtime.down_revision == "0059_collector_intake"
    assert single_lib.down_revision == "0073_collection_mission_runs"

    def upgrade_0074_pool():
        import sqlalchemy as sa

        from alembic import op

        op.add_column("collector_jobs", sa.Column("pool", sa.String(16), nullable=False, server_default="personal"))

    def downgrade_0074_pool():
        from alembic import op
        op.drop_column("collector_jobs", "pool")

    async with sessionmaker()() as db:
        for table in reversed(TABLES):
            await db.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        def drift(sync):
            context = MigrationContext.configure(
                sync, opts={"compare_type": True, "include_object": _only_collector})
            return [d for d in compare_metadata(context, Base.metadata)]

        async def columns() -> set[str]:
            return {r[0] for r in await db.execute(text(
                "select column_name from information_schema.columns where table_name = 'collector_jobs'"))}

        runtime_columns = {"cursor", "cancel_requested", "last_error", "heartbeat_at"}
        await conn.run_sync(run, intake.upgrade)
        assert not runtime_columns & await columns()
        await conn.run_sync(run, runtime.upgrade)
        await conn.run_sync(run, retry.upgrade)  # outbox retry columns that the ORM model carries
        await conn.run_sync(run, upgrade_0074_pool)  # C2 single library pool column on collector_jobs
        assert await conn.run_sync(drift) == []
        assert runtime_columns <= await columns()

        await conn.run_sync(run, downgrade_0074_pool)
        await conn.run_sync(run, retry.downgrade)
        await conn.run_sync(run, runtime.downgrade)
        assert not runtime_columns & await columns()
        await conn.run_sync(run, intake.downgrade)
        for table in TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is None

        await conn.run_sync(run, intake.upgrade)
        await conn.run_sync(run, runtime.upgrade)
        await conn.run_sync(run, retry.upgrade)
        await conn.run_sync(run, upgrade_0074_pool)
        assert await conn.run_sync(drift) == []
        names = {r[0] for r in await db.execute(text(
            "select conname from pg_constraint where conrelid in ('collector_jobs'::regclass, 'collector_outbox'::regclass)"))}
        assert {"uq_collector_jobs_idempotency", "uq_collector_outbox_tenant_sink_envelope"} <= names
        await db.commit()


def test_runtime_migration_only_adds_columns_to_the_new_table():
    """0060 은 기존 테이블 재작성 없이 collector_jobs 에 nullable/기본값 컬럼만 더한다(online-safe)."""
    source = Path("alembic/versions/0060_collector_job_runtime.py").read_text(encoding="utf-8")
    upgrade = source.split("def upgrade")[1].split("def downgrade")[0]
    assert upgrade.count("op.add_column") == 4
    assert len(re.findall(r'op\.add_column\(\s*"collector_jobs"', upgrade)) == 4
    for forbidden in ("drop_", "alter_column", "create_table", "op.execute", "NOT NULL"):
        assert forbidden not in upgrade
