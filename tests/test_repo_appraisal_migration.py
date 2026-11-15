"""repo_appraisals 테이블 마이그레이션이 모델과 같은 컬럼을 만들고 되돌릴 수 있는지 확인한다."""
import importlib.util

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import RepoAppraisal

REVISION = "0078_repo_appraisals"


def _load():
    spec = importlib.util.spec_from_file_location("appraisal_migration", f"alembic/versions/{REVISION}.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _columns(db, table):
    rows = await db.execute(text(
        "select column_name from information_schema.columns where table_name=:t"), {"t": table})
    return {name for (name,) in rows}


async def test_repo_appraisals_migration_round_trip_matches_model():
    migration = _load()
    assert migration.down_revision == "0077_artcraft_account_pin"
    model_cols = set(RepoAppraisal.__table__.columns.keys())
    async with sessionmaker()() as db:
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, migration.downgrade)
        assert await _columns(db, "repo_appraisals") == set()
        await conn.run_sync(run, migration.upgrade)
        assert await _columns(db, "repo_appraisals") == model_cols
        idx = await db.execute(text(
            "select indexname from pg_indexes where tablename='repo_appraisals'"))
        assert "ix_repo_appraisals_owner_repo_created" in {name for (name,) in idx}
        await db.rollback()
