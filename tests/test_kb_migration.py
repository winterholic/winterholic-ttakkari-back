"""0053·0054·0055·0058·0072 마이그레이션이 ORM 모델과 같은 최종 head 스키마를 만들고 왕복하며, 데이터가 있는 downgrade 를 차단하는지 확인한다."""
import importlib.util
import os
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text

from ttakkari.db import sessionmaker
from ttakkari.models import Base

CORE_TABLES = ("kb_sources", "kb_documents", "kb_revisions", "kb_passages")
ALL_TABLES = CORE_TABLES + ("kb_evidence",)
# 0065 가 만든 접근 테이블. 0072 가 컬럼을 덧붙이므로 0071 상태로 되돌린 뒤 0072 를 적용해 비교한다.
ACCESS_TABLE = "kb_document_access"
ACCESS_TABLE_0065_DDL = (
    "CREATE TABLE kb_document_access (document_id UUID NOT NULL, tenant_id VARCHAR(64) NOT NULL, "
    "scope VARCHAR(32) NOT NULL, hidden BOOLEAN NOT NULL, PRIMARY KEY (document_id), "
    "CONSTRAINT ck_kb_document_access_scope CHECK (scope IN ('personal', 'work')), "
    "FOREIGN KEY(document_id) REFERENCES kb_documents (id) ON DELETE RESTRICT)"
)
VERSIONS_DIR = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _load(name):
    file_path = VERSIONS_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"kb_migration_{name}", str(file_path))
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module



async def test_kb_migrations_match_models_and_round_trip():
    core = _load("0053_kb_core")
    evidence = _load("0054_kb_evidence")
    hardening = _load("0055_kb_immutability_hardening")
    truncate_mod = _load("0058_kb_immutability_truncate")
    v3 = _load("0072_kb_v3_schema_and_scope")
    assert core.down_revision == "0052_artifact_area_byproducts"
    assert evidence.down_revision == "0053_kb_core"
    assert hardening.down_revision == "0054_kb_evidence"
    assert truncate_mod.down_revision == "0057_scheduled_actions"
    assert v3.down_revision == "0071_library_platform_v2_merge"
    head_tables = ALL_TABLES + (ACCESS_TABLE,) + v3._V3_TABLES
    async with sessionmaker()() as db:
        for table in v3._V3_TABLES + (ACCESS_TABLE,) + tuple(reversed(ALL_TABLES)):
            await db.execute(text(f"DROP TABLE IF EXISTS {table} CASCADE"))
        conn = await db.connection()

        def run(sync, fn):
            with Operations.context(MigrationContext.configure(sync)):
                fn()

        await conn.run_sync(run, core.upgrade)
        await conn.run_sync(run, evidence.upgrade)
        await conn.run_sync(run, hardening.upgrade)
        await conn.run_sync(run, truncate_mod.upgrade)
        await db.execute(text(ACCESS_TABLE_0065_DDL))
        await conn.run_sync(run, v3.upgrade)
        # 최종 head(0072 포함) 스키마가 모델과 일치해야 한다. 0072 의 추가 컬럼을 비교에서 빼지 않는다.
        for table in head_tables:
            cols = {r[0] for r in await db.execute(text(
                "select column_name from information_schema.columns where table_name=:t"), {"t": table})}
            expected = {c.name for c in Base.metadata.tables[table].columns}
            if table == ACCESS_TABLE:
                expected.discard("pool")  # 0074_collection_single_library 가 덧붙인다. 이 시험은 0065→0072 만 재생한다.
            assert cols == expected, table
            indexes = {r[0] for r in await db.execute(text("select indexname from pg_indexes where tablename=:t"), {"t": table})}
            for index in Base.metadata.tables[table].indexes:
                assert index.name in indexes, (table, index.name)
        triggers = {r[0] for r in await db.execute(text(
            "select tgname from pg_trigger where not tgisinternal"))}
        for table in ("kb_sources", "kb_revisions", "kb_passages", "kb_evidence"):
            assert f"{table}_immutable" in triggers, table
        # 0058 TRUNCATE 트리거 검증
        for table in ("kb_sources", "kb_documents", "kb_revisions", "kb_passages", "kb_evidence"):
            assert f"{table}_truncate" in triggers, table
        assert "kb_revisions_validate" in triggers
        for trigger in ("trg_kb_document_access_derive", "trg_kb_relations_active_guard", "trg_kb_relation_evidence_sync"):
            assert trigger in triggers, trigger
        assert "kb_passages_validate" in triggers
        assert "kb_evidence_validate" in triggers

        # 0055 FK 제약조건이 RESTRICT(b'r')인지 검증
        fk_actions = {
            r[0]: r[1]
            for r in await db.execute(
                text("SELECT conname, confdeltype FROM pg_constraint WHERE conname IN ("
                     "'kb_revisions_document_id_fkey', 'kb_passages_revision_id_fkey', 'kb_evidence_passage_id_fkey')")
            )
        }
        assert fk_actions["kb_revisions_document_id_fkey"] in (b"r", "r")
        assert fk_actions["kb_passages_revision_id_fkey"] in (b"r", "r")
        assert fk_actions["kb_evidence_passage_id_fkey"] in (b"r", "r")

        # 0072 왕복: 신규 테이블·접근 컬럼·트리거 함수가 사라졌다가 다시 생긴다
        await conn.run_sync(run, v3.downgrade)
        for table in v3._V3_TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is None, table
        assert await db.scalar(text("select count(*) from pg_proc where proname in "
                                    "('kb_relations_active_guard','kb_sync_relation_evidence_count',"
                                    "'kb_document_access_derive_visibility')")) == 0
        await conn.run_sync(run, v3.upgrade)

        # 0058 데이터가 있는 상태에서 downgrade 시 차단 검증
        await db.execute(
            text(
                "INSERT INTO kb_sources (id, source_key, kind, sha256, byte_size, store_path, collected_at, origin_meta, created_at) "
                "VALUES (gen_random_uuid(), 'test_key', 'file', '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', 10, 'path', now(), '{}'::jsonb, now())"
            )
        )
        with pytest.raises(RuntimeError) as exc_info:
            await conn.run_sync(run, truncate_mod.downgrade)
        assert "Refusing to downgrade populated KB ledger" in str(exc_info.value)

        # ALLOW_KB_DESTRUCTIVE_DOWNGRADE=1 플래그 시에는 데이터가 있어도 downgrade 허용
        old_env = os.environ.get("ALLOW_KB_DESTRUCTIVE_DOWNGRADE")
        try:
            os.environ["ALLOW_KB_DESTRUCTIVE_DOWNGRADE"] = "1"
            await conn.run_sync(run, truncate_mod.downgrade)
        finally:
            if old_env is None:
                os.environ.pop("ALLOW_KB_DESTRUCTIVE_DOWNGRADE", None)
            else:
                os.environ["ALLOW_KB_DESTRUCTIVE_DOWNGRADE"] = old_env

        # 0053~0055 빈 원장 다운그레이드 왕복 (트리거 우회를 위해 replica 모드로 삭제)
        await db.execute(text("SET session_replication_role = 'replica'"))
        await db.execute(text("DELETE FROM kb_sources"))
        await db.execute(text("SET session_replication_role = 'origin'"))
        await conn.run_sync(run, v3.downgrade)
        await db.execute(text(f"DROP TABLE {ACCESS_TABLE}"))  # 0065 소유 테이블이 kb_documents 를 참조한다
        await conn.run_sync(run, hardening.downgrade)

        await conn.run_sync(run, evidence.downgrade)
        await conn.run_sync(run, core.downgrade)
        for table in ALL_TABLES:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is None
        assert await db.scalar(text("select count(*) from pg_proc where proname in "
                                    "('kb_immutable_row','kb_validate_evidence_span','kb_truncate_deny')")) == 0

        # 재업그레이드
        await conn.run_sync(run, core.upgrade)
        await conn.run_sync(run, evidence.upgrade)
        await conn.run_sync(run, hardening.upgrade)
        await conn.run_sync(run, truncate_mod.upgrade)
        await db.execute(text(ACCESS_TABLE_0065_DDL))
        await conn.run_sync(run, v3.upgrade)
        for table in head_tables:
            assert await db.scalar(text("select to_regclass(:t)"), {"t": table}) is not None
        await db.rollback()
