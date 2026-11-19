"""0079 마이그레이션이 모델과 같은 열을 만들고 되돌릴 수 있는지, 원문 키 열이 없는지 확인한다."""
import importlib.util
from pathlib import Path

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
    v2_spec = importlib.util.spec_from_file_location("context_sharing_v2_migration",
        "alembic/versions/0080_ctx_share_v2_keys.py")
    assert v2_spec and v2_spec.loader
    v2 = importlib.util.module_from_spec(v2_spec)
    v2_spec.loader.exec_module(v2)
    async with sessionmaker()() as db:
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, migration.downgrade)
        for table in TABLES:
            assert await _columns(db, table) == set()
        await conn.run_sync(run, migration.upgrade)
        await conn.run_sync(run, v2.upgrade)
        for table, model in TABLES.items():
            assert await _columns(db, table) == set(model.__table__.columns.keys()), table
        idx = {n for (n,) in await db.execute(text("select indexname from pg_indexes where tablename='ctx_share_publications'"))}
        assert "uq_ctx_share_publications_active" in idx
        await db.rollback()


def test_no_column_can_hold_a_raw_key_or_control_token():
    names = {c for model in TABLES.values() for c in model.__table__.columns.keys()}  # noqa: SIM118 - ColumnCollection.keys()
    assert not {n for n in names if n in {"key", "raw_key", "token", "secret", "control_token", "credential"}}


def test_share_key_model_has_v2_metadata_and_durable_revocation_fields():
    names = set(models.ShareKey.__table__.columns.keys())
    assert {"credential_type", "snapshot_policy", "pinned_snapshot_id", "scopes", "last_used_at",
            "revoke_error"} <= names


def test_v2_migration_is_additive_and_backfills_legacy_keys_as_v1():
    migration = importlib.util.spec_from_file_location(
        "context_sharing_v2_migration", "alembic/versions/0080_ctx_share_v2_keys.py")
    assert migration and migration.loader
    mod = importlib.util.module_from_spec(migration)
    migration.loader.exec_module(mod)
    assert mod.down_revision == REVISION
    assert len(mod.revision) <= 32
    source = Path("alembic/versions/0080_ctx_share_v2_keys.py").read_text()
    assert 'server_default="single_use_v1"' in source
    assert "ADD COLUMN" not in source.upper()  # Alembic uses additive op.add_column calls.
    assert "DROP TABLE" not in source.upper()
