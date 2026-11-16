"""0079 마이그레이션이 모델과 같은 열을 만들고 되돌릴 수 있는지, 원문 키 열이 없는지 확인한다."""
import importlib.util

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.context_sharing import models
from ttakkari.db import sessionmaker

REVISION = "0079_context_sharing"
TABLES = {
    "ctx_share_projects": models.ShareProject,
    "ctx_share_drafts": models.ShareDraft,
    "ctx_share_draft_documents": models.ShareDraftDocument,
    "ctx_share_publications": models.SharePublication,
    "ctx_share_keys": models.ShareKey,
}


def _load():
    spec = importlib.util.spec_from_file_location("context_sharing_migration", f"alembic/versions/{REVISION}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _columns(db, table):
    rows = await db.execute(text("select column_name from information_schema.columns where table_name=:t"), {"t": table})
    return {name for (name,) in rows}


async def test_migration_round_trip_matches_models():
    migration = _load()
    assert migration.down_revision == "0078_repo_appraisals"
    async with sessionmaker()() as db:
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, migration.downgrade)
        for table in TABLES:
            assert await _columns(db, table) == set()
        await conn.run_sync(run, migration.upgrade)
        for table, model in TABLES.items():
            assert await _columns(db, table) == set(model.__table__.columns.keys()), table
        idx = {n for (n,) in await db.execute(text("select indexname from pg_indexes where tablename='ctx_share_publications'"))}
        assert "uq_ctx_share_publications_active" in idx
        await db.rollback()


def test_no_column_can_hold_a_raw_key_or_control_token():
    names = {c for model in TABLES.values() for c in model.__table__.columns.keys()}  # noqa: SIM118 - ColumnCollection.keys()
    assert not {n for n in names if n in {"key", "raw_key", "token", "secret", "control_token", "credential"}}
