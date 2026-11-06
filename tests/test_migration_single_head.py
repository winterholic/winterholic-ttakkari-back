"""Alembic 마이그레이션 그래프가 head 하나인지 검사한다.

두 세션이 같은 down_revision 에서 마이그레이션을 따로 만들면 head 가 둘이 되어 배포 게이트의 세대 계약 검사
(`get_current_head`)가 MultipleHeads 로 깨진다. contract 마크로 묶여 push 전 사전 검증에 항상 포함된다.
"""

import asyncio
import os
import subprocess
import sys
import uuid
from pathlib import Path

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from test_retire_clovanote_migration import migration_database  # noqa: F401

pytestmark = pytest.mark.contract

ROOT = Path(__file__).resolve().parents[1]


def test_alembic_executable_comes_from_the_active_python_environment(monkeypatch, tmp_path):
    python = tmp_path / ".venv" / "bin" / "python"
    monkeypatch.setattr(sys, "executable", str(python))

    assert _alembic_executable() == str(python.with_name("alembic"))


def test_alembic_has_exactly_one_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    assert len(heads) == 1, (
        f"마이그레이션 head 가 {len(heads)}개다: {sorted(heads)}. 한쪽 down_revision 을 다른 head 뒤로 옮겨 합친다."
    )


def test_delivery_models_are_registered_in_alembic_metadata():
    from ttakkari.delivery_models import ChangeSet
    from ttakkari.models import Base

    env_source = (ROOT / "alembic/env.py").read_text(encoding="utf-8")
    assert "import ttakkari.delivery_models" in env_source
    assert Base.metadata.tables[ChangeSet.__tablename__] is ChangeSet.__table__


def _alembic_executable() -> str:
    return str(Path(sys.executable).with_name("alembic"))


def _alembic(database: str, *args: str) -> None:
    import pg_sandbox

    result = subprocess.run(
        [_alembic_executable(), *args], cwd=ROOT,
        env={**os.environ, "TTAKKARI_NO_ENV_FILE": "1",
             "TTAKKARI_DATABASE_URL": pg_sandbox.localhost_database_url(database)},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]


async def test_0077_upgrade_preserves_existing_sessions_and_adds_nullable_pin(migration_database):  # noqa: F811
    database = migration_database
    await asyncio.to_thread(_alembic, database, "upgrade", "0076_kb_v3_collection_merge")
    connection = await asyncpg.connect(database=database, host="localhost")
    session_id = uuid.uuid4()
    try:
        await connection.execute("SET session_replication_role = 'replica'")
        await connection.execute(
            "INSERT INTO sessions(id, workspace_id, title, engine, created_at, updated_at) "
            "VALUES($1, $2, 'pre-0077 session', 'codex', now(), now())",
            session_id, uuid.uuid4(),
        )
        await connection.execute("SET session_replication_role = 'origin'")
        before = await connection.fetchrow(
            "SELECT id, title, engine FROM sessions WHERE id=$1", session_id)
    finally:
        await connection.close()

    await asyncio.to_thread(_alembic, database, "upgrade", "0077_artcraft_account_pin")
    connection = await asyncpg.connect(database=database, host="localhost")
    try:
        after = await connection.fetchrow(
            "SELECT id, title, engine, account_id FROM sessions WHERE id=$1", session_id)
        column = await connection.fetchrow(
            "SELECT is_nullable, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='sessions' AND column_name='account_id'")
        index = await connection.fetchval(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='public' AND tablename='sessions' "
            "AND indexname='ix_sessions_account_id'")
        fk_count = await connection.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conrelid='sessions'::regclass "
            "AND confrelid='engine_accounts'::regclass")
        assert tuple(before) == (session_id, "pre-0077 session", "codex")
        assert tuple(after) == (session_id, "pre-0077 session", "codex", None)
        assert tuple(column) == ("YES", "uuid")
        assert index and "USING btree (account_id)" in index
        assert fk_count == 0
    finally:
        await connection.close()
