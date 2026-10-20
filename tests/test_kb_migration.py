"""0053·0054 마이그레이션이 ORM 모델과 같은 스키마를 만들고 왕복하는지 격리 트랜잭션에서 확인한다."""
import importlib.util

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import Base

CORE_TABLES = ("kb_sources", "kb_documents", "kb_revisions", "kb_passages")
ALL_TABLES = CORE_TABLES + ("kb_evidence",)


def _load(name):
    spec = importlib.util.spec_from_file_location(f"kb_migration_{name}", f"alembic/versions/{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


async def test_kb_migrations_match_models_and_round_trip():
    core = _load("0053_kb_core")
    evidence = _load("0054_kb_evidence")
    assert core.down_revision == "0052_artifact_area_byproducts"
    assert evidence.down_revision == "0053_kb_core"
    async with sessionmaker()() as db:
        for table in reversed(ALL_TABLES):
            await db.execute(text(f"DROP TABLE {table} CASCADE"))
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, core.upgrade)
        await conn.run_sync(run, evidence.upgrade)
        for table in ALL_TABLES:
            cols = {r[0] for r in await db.execute(text(
                "select column_name from information_schema.columns where table_name=:t"), {"t": table})}
            assert cols == {c.name for c in Base.metadata.tables[table].columns}, table
            indexes = {r[0] for r in await db.execute(text("select indexname from pg_indexes where tablename=:t"), {"t": table})}
            for index in Base.metadata.tables[table].indexes:
                assert index.name in indexes, (table, index.name)
        triggers = {r[0] for r in await db.execute(text(
            "select tgname from pg_trigger where not tgisinternal"))}
        for table in ("kb_sources", "kb_revisions", "kb_passages", "kb_evidence"):
            assert f"{table}_immutable" in triggers, table
        assert "kb_documents_immutable" not in triggers
        assert "kb_evidence_validate" in triggers
        await conn.run_sync(run, evidence.downgrade)
        await conn.run_sync(run, core.downgrade)
        for table in ALL_TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is None
        assert await db.scalar(text("select count(*) from pg_proc where proname in "
                                    "('kb_immutable_row','kb_validate_evidence_span')")) == 0
        await conn.run_sync(run, core.upgrade)
        await conn.run_sync(run, evidence.upgrade)
        for table in ALL_TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is not None
        await db.rollback()
