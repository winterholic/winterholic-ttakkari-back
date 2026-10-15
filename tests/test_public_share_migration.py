"""Exercise real transactional DDL on the isolated pytest DB, then roll it back."""
import importlib.util

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker


async def test_public_share_migration_round_trip():
    spec = importlib.util.spec_from_file_location('share_migration', 'alembic/versions/0028_public_shares.py')
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with sessionmaker()() as db:
        # Existing test metadata has the ORM table. Restore it by rollback after
        # testing both directions, so subsequent tests see their original schema.
        await db.execute(text('DROP TABLE public_shares'))
        conn = await db.connection()
        def upgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.upgrade()
        def downgrade(sync):
            with Operations.context(MigrationContext.configure(sync)):
                migration.downgrade()
        await conn.run_sync(upgrade)
        assert await db.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'public_share_%'")) == 6
        assert await db.scalar(text("SELECT count(*) FROM pg_indexes WHERE indexname='uq_public_shares_active_root'")) == 1
        await conn.run_sync(downgrade)
        assert await db.scalar(text("SELECT to_regclass('public_shares')")) is None
        assert await db.scalar(text("SELECT count(*) FROM pg_trigger WHERE tgname LIKE 'public_share_%'")) == 0
        assert await db.scalar(text("SELECT count(*) FROM pg_proc WHERE proname='revoke_bound_public_shares'")) == 0
        await conn.run_sync(upgrade)
        assert await db.scalar(text("SELECT to_regclass('public_shares')")) == 'public_shares'
        await db.rollback()
