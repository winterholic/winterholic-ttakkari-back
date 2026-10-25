"""The integrated C1 and C2 migrations share one linear, online-safe schema path."""
import importlib.util
import json
import uuid
from pathlib import Path

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari import generation_deployment as deployment
from ttakkari.db import sessionmaker
from ttakkari.deployment import JobStore

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    path = ROOT / "alembic" / "versions" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"combined_migration_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_c1_and_c2_form_a_serial_migration_path_and_generation_contract():
    runs = _load("0073_collection_mission_runs")
    library = _load("0074_collection_single_library")
    contract = json.loads((ROOT / "deploy/generation-contract.json").read_text())

    assert runs.down_revision == "0072_collection_missions"
    assert library.down_revision == runs.revision
    assert contract["schema_revision"] == library.revision
    assert runs.revision in contract["online_migration_from"]
    assert "0072_collection_missions" in contract["online_migration_from"]


def test_previous_c1_generation_is_admitted_for_the_additive_c2_upgrade(monkeypatch, tmp_path):
    class Database:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

        async def scalar(self, _statement):
            return "0073_collection_mission_runs"

        async def scalars(self, _statement):
            class Rows:
                def all(self):
                    return ["0073_collection_mission_runs"]

                def __iter__(self):
                    return iter(self.all())

            return Rows()

    monkeypatch.setattr(deployment, "sessionmaker", lambda: Database)
    controller = deployment.GenerationController(JobStore(tmp_path))
    contract = deployment.Releases.contract(ROOT)
    assert awaitable_schema_plan(controller, contract) == ("online", "0073_collection_mission_runs")


async def test_0072_to_0074_migrations_roundtrip_in_isolated_database_and_match_orm():
    base = _load("0072_collection_missions")
    runs = _load("0073_collection_mission_runs")
    library = _load("0074_collection_single_library")
    schema = f"c2_migration_{uuid.uuid4().hex}"
    checked_tables = {
        "collection_missions", "collection_mission_jobs", "collection_mission_runs",
        "kb_document_aliases", "collection_scope_ledger", "collector_jobs", "collector_sources",
        "kb_document_access",
    }

    async with sessionmaker()() as db:
        await db.execute(text(f'CREATE SCHEMA "{schema}"'))
        copied_tables = (
            "collector_jobs", "collector_sources", "kb_document_access", "kb_documents",
            "collector_outbox", "collector_items", "collector_dedupe_bands",
        )
        for table in copied_tables:
            await db.execute(text(f'CREATE TABLE "{schema}"."{table}" (LIKE public."{table}" INCLUDING ALL)'))
        for table in ("collector_jobs", "collector_sources", "kb_document_access"):
            await db.execute(text(f'ALTER TABLE "{schema}"."{table}" DROP COLUMN pool'))
        await db.execute(text(f'SET LOCAL search_path TO "{schema}"'))
        connection = await db.connection()
        def migrate(sync, operation):
            with Operations.context(MigrationContext.configure(sync)):
                operation()

        await connection.run_sync(migrate, base.upgrade)
        await connection.run_sync(migrate, runs.upgrade)
        await connection.run_sync(migrate, library.upgrade)
        for table in checked_tables:
            assert await db.scalar(text(
                "SELECT to_regclass(:qualified)"), {"qualified": f'"{schema}"."{table}"'}) is not None, table
        pool_columns = await db.scalar(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
            "AND table_name IN ('collector_jobs', 'collector_sources', 'kb_document_access') AND column_name='pool'"),
            {"schema": schema})
        assert pool_columns == 3

        await connection.run_sync(migrate, library.downgrade)
        await connection.run_sync(migrate, runs.downgrade)
        await connection.run_sync(migrate, base.downgrade)
        assert await db.scalar(text("SELECT to_regclass(:qualified)"),
                                {"qualified": f'"{schema}"."collection_missions"'}) is None
        assert await db.scalar(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_schema=:schema "
            "AND table_name IN ('collector_jobs', 'collector_sources', 'kb_document_access') AND column_name='pool'"),
            {"schema": schema}) == 0

        await connection.run_sync(migrate, base.upgrade)
        await connection.run_sync(migrate, runs.upgrade)
        await connection.run_sync(migrate, library.upgrade)
        assert await db.scalar(text(
            "SELECT to_regclass(:qualified)"), {"qualified": f'"{schema}"."collection_mission_runs"'}) is not None
        await db.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await db.commit()


def awaitable_schema_plan(controller, contract):
    """Run the async admission planner without requiring pytest-asyncio in a sync test."""
    import asyncio

    return asyncio.run(controller.schema_plan(contract))
