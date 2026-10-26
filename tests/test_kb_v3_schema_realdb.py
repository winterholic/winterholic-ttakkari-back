"""KB v3 0072/0073 마이그레이션을 실제 PostgreSQL(Docker pgvector/pgvector:pg17)에서 검증한다.

시나리오(한 번의 Docker 컨테이너에서 순서대로 진행):
  1. 빈 DB 에 alembic upgrade 0071 -> 대표 기존 데이터 삽입(personal 2, work 2, revision/passage/evidence)
  2. upgrade head(0073) -> 백필(personal->global, work->work_only), L2 예산 원장 생성
  3. 반례: visibility 파생/모순 거부, 근거 없는 active 거부(INSERT·UPDATE), evidence_count 강제 동기화,
     activate_relation_with_evidence, 근거 삭제 시 강등
  4. downgrade: 신규 테이블 데이터가 있으면 거부, 비운 뒤 성공, 기존 데이터 보존, upgrade head 재실행
  5. 추가 컬럼 데이터 손실 가드와 ALLOW_KB_DESTRUCTIVE_DOWNGRADE=1 경고 경로
  6. 최종 head 스키마가 ORM 모델과 컬럼 단위로 일치

Docker 가 없거나 이미지를 띄울 수 없으면 skip 한다. 직접 실행: uv run pytest -q tests/test_kb_v3_schema_realdb.py -m pg
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_ontology_models import KbRelation, activate_relation_with_evidence
from ttakkari.models import Base

pytestmark = pytest.mark.pg

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "tests" / "fixtures" / "kb_v3_seed_0071.sql"
IMAGE = "pgvector/pgvector:pg17"
HEAD = "0074_kb_v3_research_merge"
V3_TABLES = (
    "kb_pipeline_jobs",
    "kb_pipeline_stages",
    "kb_entities",
    "kb_entity_aliases",
    "kb_document_entities",
    "kb_assertions",
    "kb_assertion_evidence",
    "kb_relations",
    "kb_relation_evidence",
    "kb_entity_canonicals",
    "kb_conflicts",
    "kb_l2_budget_usage",
    "kb_l2_budget_reservations",
)
KB_MODEL_TABLES = (
    "kb_sources",
    "kb_documents",
    "kb_revisions",
    "kb_passages",
    "kb_evidence",
    "kb_document_access",
) + V3_TABLES


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(["docker", "info"], capture_output=True, check=False).returncode
        == 0
    )


class Db:
    """컨테이너 한 개에 대한 접속·alembic 실행 도우미."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.dsn = f"postgresql://postgres:pw@127.0.0.1:{port}/kb3"
        self.url = f"postgresql+asyncpg://postgres:pw@127.0.0.1:{port}/kb3"

    # -- SQL --------------------------------------------------------------
    def _run(self, fn):
        async def go():
            conn = await asyncpg.connect(self.dsn)
            try:
                return await fn(conn)
            finally:
                await conn.close()

        return asyncio.run(go())

    def execute(self, sql: str, *args) -> None:
        self._run(lambda c: c.execute(sql, *args))

    def fetch(self, sql: str, *args) -> list[asyncpg.Record]:
        return self._run(lambda c: c.fetch(sql, *args))

    def scalar(self, sql: str, *args):
        return self._run(lambda c: c.fetchval(sql, *args))

    def rejected(self, sql: str, *args) -> str:
        """SQL 이 check_violation(23514)으로 거부되는지 확인하고 메시지를 돌려준다."""
        with pytest.raises(asyncpg.PostgresError) as err:
            self.execute(sql, *args)
        assert err.value.sqlstate == "23514", (err.value.sqlstate, str(err.value))
        return str(err.value)

    # -- ORM --------------------------------------------------------------
    def orm(self, fn):
        async def go():
            engine = create_async_engine(self.url)
            try:
                async with AsyncSession(engine, expire_on_commit=False) as session:
                    result = await fn(session)
                    await session.commit()
                    return result
            finally:
                await engine.dispose()

        return asyncio.run(go())

    # -- alembic ----------------------------------------------------------
    def alembic(self, *args: str, extra_env: dict[str, str] | None = None):
        env = {
            k: v for k, v in os.environ.items() if not k.startswith(("PG", "TTAKKARI_"))
        }
        env.update(
            TTAKKARI_NO_ENV_FILE="1",
            TTAKKARI_DATABASE_URL=self.url,
            **(extra_env or {}),
        )
        return subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )

    def alembic_ok(self, *args: str, extra_env: dict[str, str] | None = None) -> str:
        proc = self.alembic(*args, extra_env=extra_env)
        assert proc.returncode == 0, proc.stderr[-3000:]
        return proc.stdout + proc.stderr

    def version(self) -> str | None:
        return self.scalar("SELECT version_num FROM alembic_version")


@pytest.fixture(scope="module")
def db():
    if not _docker_ready():
        pytest.skip("docker 를 사용할 수 없다")
    name = f"kb3-realdb-{uuid.uuid4().hex[:8]}"
    started = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-e",
            "POSTGRES_PASSWORD=pw",
            "-e",
            "POSTGRES_DB=kb3",
            "-p",
            "127.0.0.1::5432",
            IMAGE,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if started.returncode != 0:
        pytest.skip(f"{IMAGE} 컨테이너를 띄울 수 없다: {started.stderr.strip()[:200]}")
    try:
        mapping = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()[0]
        handle = Db(int(mapping.rsplit(":", 1)[1]))
        deadline = time.time() + 90
        while True:
            try:
                if handle.scalar("SELECT 1") == 1:
                    break
            except Exception:
                if time.time() > deadline:
                    raise
                time.sleep(1)
        yield handle
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _new_doc(db: Db, key: str) -> uuid.UUID:
    doc = uuid.uuid4()
    db.execute(
        "INSERT INTO kb_documents (id, doc_key, title, lang, created_at, updated_at) "
        "VALUES ($1, $2, $2, 'ko', now(), now())",
        doc,
        key,
    )
    return doc


def _access(db: Db, doc: uuid.UUID) -> tuple[str, str, str]:
    row = db.fetch(
        "SELECT scope, origin_scope, visibility FROM kb_document_access WHERE document_id = $1",
        doc,
    )[0]
    return row["scope"], row["origin_scope"], row["visibility"]


def _new_entity(db: Db, name: str) -> uuid.UUID:
    ent = uuid.uuid4()
    db.execute(
        "INSERT INTO kb_entities (id, canonical_name, norm_name) VALUES ($1, $2, $2)",
        ent,
        name,
    )
    return ent


def _new_relation(db: Db, name: str) -> uuid.UUID:
    rel = uuid.uuid4()
    db.execute(
        "INSERT INTO kb_relations (id, source_entity_id, target_entity_id, relation_type) "
        "VALUES ($1, $2, $3, 'related_to')",
        rel,
        _new_entity(db, f"{name}-src"),
        _new_entity(db, f"{name}-dst"),
    )
    return rel


def _relation_state(db: Db, rel: uuid.UUID):
    row = db.fetch(
        "SELECT status, evidence_count, representative_evidence_id FROM kb_relations WHERE id = $1",
        rel,
    )[0]
    return row["status"], row["evidence_count"], row["representative_evidence_id"]


def _ledger_counts(db: Db) -> dict[str, int]:
    return {
        t: db.scalar(f"SELECT count(*) FROM {t}")
        for t in (
            "kb_sources",
            "kb_documents",
            "kb_revisions",
            "kb_passages",
            "kb_evidence",
            "kb_document_access",
        )
    }


def test_real_db_upgrade_guards_downgrade_roundtrip(db: Db):
    # 1. 0071 + 대표 기존 데이터
    db.alembic_ok("upgrade", "0071_library_platform_v2_merge")
    assert db.version() == "0071_library_platform_v2_merge"
    db.execute(SEED.read_text())
    before = _ledger_counts(db)
    assert before == {
        "kb_sources": 4,
        "kb_documents": 4,
        "kb_revisions": 4,
        "kb_passages": 4,
        "kb_evidence": 4,
        "kb_document_access": 4,
    }

    # 2. upgrade head + 백필
    db.alembic_ok("upgrade", "head")
    assert db.version() == HEAD
    backfill = {
        r["doc_key"]: (r["scope"], r["origin_scope"], r["visibility"])
        for r in db.fetch(
            "SELECT d.doc_key, a.scope, a.origin_scope, a.visibility FROM kb_document_access a "
            "JOIN kb_documents d ON d.id = a.document_id"
        )
    }
    assert backfill == {
        "personal-note-1": ("personal", "personal", "global"),
        "personal-note-2": ("personal", "personal", "global"),
        "work-spec-1": ("work", "work", "work_only"),
        "work-spec-2": ("work", "work", "work_only"),
    }
    assert _ledger_counts(db) == before
    # 서버 기본값이 없어야 트리거가 "명시하지 않음"을 구분한다
    assert (
        db.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'kb_document_access' "
            "AND column_name IN ('origin_scope', 'visibility') AND column_default IS NOT NULL"
        )
        == 0
    )

    # 3-a/b/c. visibility 파생과 모순 거부 (raw SQL: writer 가 scope 만 넘기는 경로와 동일)
    d_work, d_personal, d_bad = (
        _new_doc(db, "ex-work"),
        _new_doc(db, "ex-personal"),
        _new_doc(db, "ex-bad"),
    )
    db.execute(
        "INSERT INTO kb_document_access (document_id, tenant_id, scope, hidden) VALUES ($1, 'owner', 'work', false)",
        d_work,
    )
    assert _access(db, d_work) == ("work", "work", "work_only")  # (a)
    db.execute(
        "INSERT INTO kb_document_access (document_id, tenant_id, scope, hidden) VALUES ($1, 'owner', 'personal', false)",
        d_personal,
    )
    assert _access(db, d_personal) == ("personal", "personal", "global")  # (b)
    message = db.rejected(  # (c)
        "INSERT INTO kb_document_access (document_id, tenant_id, scope, visibility, hidden) "
        "VALUES ($1, 'owner', 'work', 'global', false)",
        d_bad,
    )
    assert "work scope cannot have global visibility" in message
    db.rejected(  # origin_scope 만 work 인 경우도 global 은 거부
        "INSERT INTO kb_document_access (document_id, tenant_id, scope, origin_scope, visibility, hidden) "
        "VALUES ($1, 'owner', 'personal', 'work', 'global', false)",
        d_bad,
    )
    # origin_scope 가 work 면 visibility 생략 시 work_only 로 파생
    d_origin = _new_doc(db, "ex-origin-work")
    db.execute(
        "INSERT INTO kb_document_access (document_id, tenant_id, scope, origin_scope, hidden) "
        "VALUES ($1, 'owner', 'personal', 'work', false)",
        d_origin,
    )
    assert _access(db, d_origin) == ("personal", "work", "work_only")
    # UPDATE 로 work 문서를 global 로 열 수 없다(check 제약)
    db.rejected(
        "UPDATE kb_document_access SET visibility = 'global' WHERE document_id = $1",
        d_work,
    )

    # 3-a/b ORM: kb_delivery/kb_library 가 쓰는 KbDocumentAccess(scope=...) 생성자 경로
    d_orm_p, d_orm_w = _new_doc(db, "orm-personal"), _new_doc(db, "orm-work")

    async def orm_insert(session: AsyncSession):
        session.add(
            KbDocumentAccess(document_id=d_orm_p, tenant_id="owner", scope="personal")
        )
        session.add(
            KbDocumentAccess(document_id=d_orm_w, tenant_id="owner", scope="work")
        )
        with pytest.raises(ValueError):
            KbDocumentAccess(
                document_id=uuid.uuid4(), scope="work", visibility="global"
            )

    db.orm(orm_insert)
    assert _access(db, d_orm_p) == ("personal", "personal", "global")
    assert _access(db, d_orm_w) == ("work", "work", "work_only")

    # 3-d. 근거 없는 active INSERT 거부 (evidence_count=1 위조 포함)
    ent_a, ent_b = _new_entity(db, "guard-a"), _new_entity(db, "guard-b")
    insert_rel = (
        "INSERT INTO kb_relations (id, source_entity_id, target_entity_id, relation_type, status, evidence_count) "
        "VALUES ($1, $2, $3, 'uses', $4, $5)"
    )
    assert "without supporting" in db.rejected(
        insert_rel, uuid.uuid4(), ent_a, ent_b, "active", 1
    )
    db.rejected(insert_rel, uuid.uuid4(), ent_a, ent_b, "active", 0)
    # candidate 로 evidence_count 를 위조해 넣어도 0 으로 강제된다
    forged = uuid.uuid4()
    db.execute(insert_rel, forged, ent_a, ent_b, "candidate", 7)
    assert _relation_state(db, forged) == ("candidate", 0, None)

    # 3-g. UPDATE 로 evidence_count 조작: active 승격 거부, 값 위조는 실제 행 수로 덮어씀
    assert "without supporting" in db.rejected(
        "UPDATE kb_relations SET status = 'active', evidence_count = 5 WHERE id = $1",
        forged,
    )
    db.execute("UPDATE kb_relations SET evidence_count = 99 WHERE id = $1", forged)
    assert _relation_state(db, forged) == ("candidate", 0, None)

    # 3-e. candidate -> 근거 연결 -> activate_relation_with_evidence -> active
    evidence_ids = [
        r["id"] for r in db.fetch("SELECT id FROM kb_evidence ORDER BY note")
    ]
    assert len(evidence_ids) == 4
    rel_ids = {}
    for key in ("one", "two"):
        rel_ids[key] = _new_relation(db, f"act-{key}")

    async def activate(session: AsyncSession):
        rel = await session.get(KbRelation, rel_ids["one"])
        await activate_relation_with_evidence(session, rel, evidence_ids[0])
        rel2 = await session.get(KbRelation, rel_ids["two"])
        await activate_relation_with_evidence(session, rel2, evidence_ids[0])
        await session.flush()
        await activate_relation_with_evidence(session, rel2, evidence_ids[1])

    db.orm(activate)
    assert _relation_state(db, rel_ids["one"]) == ("active", 1, evidence_ids[0])
    assert _relation_state(db, rel_ids["two"])[:2] == ("active", 2)
    assert (
        db.scalar(
            "SELECT count(*) FROM kb_relation_evidence WHERE relation_id = $1",
            rel_ids["two"],
        )
        == 2
    )

    # 3-f. 근거 연결 삭제: 하나 지우면 count 1 + active 유지, 마지막을 지우면 candidate 로 강등
    db.execute(
        "DELETE FROM kb_relation_evidence WHERE relation_id = $1 AND evidence_id = $2",
        rel_ids["two"],
        evidence_ids[0],
    )
    status, count, representative = _relation_state(db, rel_ids["two"])
    assert (status, count) == ("active", 1)
    assert (
        representative == evidence_ids[1]
    )  # 지워진 근거를 가리키지 않도록 남은 근거로 재지정
    db.execute(
        "DELETE FROM kb_relation_evidence WHERE relation_id = $1", rel_ids["two"]
    )
    assert _relation_state(db, rel_ids["two"]) == ("candidate", 0, None)
    # 강등된 관계는 근거 없이 다시 active 가 될 수 없다
    db.rejected(
        "UPDATE kb_relations SET status = 'active' WHERE id = $1", rel_ids["two"]
    )

    # 4. downgrade: 신규 테이블에 데이터가 있으면 거부
    proc = db.alembic("downgrade", "0071_library_platform_v2_merge")
    assert proc.returncode != 0
    assert "Refusing to downgrade populated KB v3 schema" in proc.stderr
    assert "kb_entities" in proc.stderr and "kb_relations" in proc.stderr
    assert db.version() == HEAD
    assert db.scalar("SELECT count(*) FROM kb_entities") > 0

    # 신규 테이블만 비우면(원장은 불변이라 그대로) 플래그 없이 성공
    db.execute("DELETE FROM kb_relation_evidence")
    for table in (
        "kb_conflicts",
        "kb_entity_canonicals",
        "kb_assertion_evidence",
        "kb_assertions",
        "kb_document_entities",
        "kb_entity_aliases",
        "kb_relations",
        "kb_entities",
        "kb_pipeline_stages",
        "kb_pipeline_jobs",
    ):
        db.execute(f"DELETE FROM {table}")
    # 3-a~d 에서 만든 임시 문서 접근 행도 비운다(원장 4건만 남겨 보존 검증을 단순화)
    db.execute(
        "DELETE FROM kb_document_access WHERE document_id IN ("
        "SELECT id FROM kb_documents WHERE doc_key NOT LIKE '%-note-%' AND doc_key NOT LIKE '%-spec-%')"
    )
    # 0073 은 예산 사용 원장이 남아 있으면 다운그레이드를 거부해야 한다.
    reservation_id = uuid.uuid4()
    db.execute(
        "INSERT INTO kb_l2_budget_reservations "
        "(id, budget_date, reservation_key, reserved_calls, status, lease_expires_at, "
        "next_call_no, created_at, updated_at) "
        "VALUES ($1, CURRENT_DATE, 'downgrade-safety-check', 1, 'completed', now(), 1, now(), now())",
        reservation_id,
    )
    db.execute(
        "INSERT INTO kb_l2_budget_usage "
        "(id, reservation_id, call_no, status, started_at, finished_at) "
        "VALUES ($1, $2, 1, 'consumed', now(), now())",
        uuid.uuid4(),
        reservation_id,
    )
    ledger_down = db.alembic("downgrade", "0072_kb_v3_schema_and_scope")
    assert ledger_down.returncode != 0
    assert "refusing to downgrade 0073 while L2 budget ledger data exists" in ledger_down.stderr
    assert db.version() == HEAD
    db.execute("DELETE FROM kb_l2_budget_usage WHERE reservation_id = $1", reservation_id)
    db.execute("DELETE FROM kb_l2_budget_reservations WHERE id = $1", reservation_id)

    db.alembic_ok("downgrade", "0071_library_platform_v2_merge")
    assert db.version() == "0071_library_platform_v2_merge"
    for table in V3_TABLES:
        assert db.scalar("SELECT to_regclass($1)", table) is None, table
    assert (
        db.scalar(
            "SELECT count(*) FROM pg_proc WHERE proname IN ('kb_relations_active_guard', "
            "'kb_sync_relation_evidence_count', 'kb_document_access_derive_visibility')"
        )
        == 0
    )
    assert (
        db.scalar(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'kb_document_access' "
            "AND column_name IN ('origin_scope', 'visibility')"
        )
        == 0
    )
    # 기존 원장·접근 데이터 보존 (임시 문서 4건 제외: 원장 4건 + 접근 4건)
    after = _ledger_counts(db)
    assert (
        after["kb_sources"] == 4
        and after["kb_evidence"] == 4
        and after["kb_passages"] == 4
    )
    assert after["kb_document_access"] == 4
    assert (
        db.scalar("SELECT count(*) FROM kb_document_access WHERE scope = 'personal'")
        == 2
    )

    # upgrade head 재실행 + 백필 재확인
    db.alembic_ok("upgrade", "head")
    assert db.version() == HEAD
    assert (
        db.scalar("SELECT count(*) FROM kb_document_access WHERE visibility = 'global'")
        == 2
    )
    assert (
        db.scalar(
            "SELECT count(*) FROM kb_document_access WHERE visibility = 'work_only'"
        )
        == 2
    )

    # 5. 추가 컬럼 데이터 손실 가드. 새 컬럼에 값이 있는 원장 행 + scope 로 복원 불가한 접근 행
    src = uuid.uuid4()
    body = "guard-body"
    db.execute(
        "INSERT INTO kb_sources (id, source_key, kind, sha256, byte_size, store_path, collected_at, "
        "origin_meta, created_at, blob_relpath, parser_name) "
        "VALUES ($1, 'src-col-guard', 'upload', $2, 10, 'p/x', now(), '{}'::jsonb, now(), 'blobs/x', 'pdf_v2')",
        src,
        _sha(body),
    )
    hidden_doc = _new_doc(db, "private-personal")
    db.execute(
        "INSERT INTO kb_document_access (document_id, tenant_id, scope, visibility, hidden) "
        "VALUES ($1, 'owner', 'personal', 'work_only', false)",
        hidden_doc,
    )
    proc = db.alembic("downgrade", "0071_library_platform_v2_merge")
    assert proc.returncode != 0
    assert "kb_sources.(blob_relpath" in proc.stderr
    assert "kb_document_access.(origin_scope, visibility)" in proc.stderr
    assert db.version() == HEAD
    flagged = db.alembic(
        "downgrade",
        "0071_library_platform_v2_merge",
        extra_env={"ALLOW_KB_DESTRUCTIVE_DOWNGRADE": "1"},
    )
    assert flagged.returncode == 0, flagged.stderr[-2000:]
    assert "discarding KB v3 data" in flagged.stderr
    assert db.version() == "0071_library_platform_v2_merge"
    db.alembic_ok("upgrade", "head")

    # 6. 최종 head 스키마 == ORM 모델 (컬럼 이름·NULL 허용)
    for table in KB_MODEL_TABLES:
        actual = {
            r["column_name"]: r["is_nullable"] == "YES"
            for r in db.fetch(
                "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = $1",
                table,
            )
        }
        expected = {c.name: c.nullable for c in Base.metadata.tables[table].columns}
        assert actual == expected, table
        indexes = {
            r["indexname"]
            for r in db.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = $1", table
            )
        }
        for index in Base.metadata.tables[table].indexes:
            assert index.name in indexes, (table, index.name)
