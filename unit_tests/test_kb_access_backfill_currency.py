"""Backfill currency regression with a SQLite-backed DB I/O double, not PostgreSQL.

The old PostgreSQL LATERAL read is represented by its equivalent SQLite scalar
subquery. All shared currency SELECTs and new batched source SELECTs run as-is.
Only structure-backfill writes outside access classification are replaced.
"""

import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.sql.elements import TextClause

from ttakkari import kb_access_backfill as access_backfill
from ttakkari import kb_backfill
from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_models import KbDocument, KbRevision, KbSource


class BackfillStore:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        self.connection = self.engine.connect()
        for ddl in (
            "CREATE TABLE kb_documents (id TEXT PRIMARY KEY, doc_key TEXT)",
            "CREATE TABLE kb_sources (id TEXT PRIMARY KEY, origin_meta JSON)",
            "CREATE TABLE kb_revisions (id TEXT PRIMARY KEY, document_id TEXT, source_id TEXT, rev_no INTEGER)",
            "CREATE TABLE kb_observations (id TEXT, document_id TEXT, revision_id TEXT, event_time TEXT, recorded_at TEXT)",
            ("CREATE TABLE kb_document_access (document_id TEXT PRIMARY KEY, tenant_id TEXT, scope TEXT, "
             "origin_scope TEXT, visibility TEXT, hidden BOOLEAN, pool TEXT DEFAULT 'personal')"),
        ):
            self.connection.exec_driver_sql(ddl)
        self.objects = {}
        self.added = []
        self.reads = []
        for number, scope in ((1, "work"), (2, "personal"), (3, None), (4, "personal")):
            doc = KbDocument(id=uuid.UUID(int=number), doc_key=f"collector:legacy-{number}", title=str(number))
            self.objects[KbDocument, doc.id] = doc
            self.connection.exec_driver_sql("INSERT INTO kb_documents VALUES (?, ?)", (doc.id.hex, doc.doc_key))
            if scope:
                self.add_revision(doc.id, number * 10, 1, scope, day=10)
        # Higher rev_no arrived later but describes an older event; current remains work.
        self.add_revision(uuid.UUID(int=1), 11, 2, "personal", day=1)
        existing = KbDocumentAccess(document_id=uuid.UUID(int=4), tenant_id="owner", scope="work",
                                    origin_scope="work", visibility="work_only", hidden=True)
        self.objects[KbDocumentAccess, existing.document_id] = existing
        self.connection.exec_driver_sql(
            "INSERT INTO kb_document_access (document_id, tenant_id, scope, origin_scope, visibility, hidden) VALUES (?, 'owner', 'work', 'work', 'work_only', 1)",
            (existing.document_id.hex,),
        )
        self.connection.commit()

    def add_revision(self, doc_id, number, rev_no, scope, *, day):
        revision_id, source_id = uuid.UUID(int=number), uuid.UUID(int=number + 100)
        meta = {"via": "collector", "scope": scope}
        self.objects[KbSource, source_id] = KbSource(id=source_id, origin_meta=meta)
        self.objects[KbRevision, revision_id] = KbRevision(
            id=revision_id, document_id=doc_id, source_id=source_id, rev_no=rev_no, body="unchanged"
        )
        self.connection.exec_driver_sql("INSERT INTO kb_sources VALUES (?, ?)", (source_id.hex, json.dumps(meta)))
        self.connection.exec_driver_sql("INSERT INTO kb_revisions VALUES (?, ?, ?, ?)",
                                        (revision_id.hex, doc_id.hex, source_id.hex, rev_no))
        self.connection.exec_driver_sql("INSERT INTO kb_observations VALUES (?, ?, ?, ?, ?)",
                                        (revision_id.hex, doc_id.hex, revision_id.hex, f"2026-10-{day:02d}", "2026-10-20"))

    async def execute(self, statement, parameters=None):
        self.reads.append(str(statement))
        if isinstance(statement, TextClause):
            query = str(statement)
            if "LEFT JOIN LATERAL" in query:
                # PostgreSQL's old LATERAL origin read, same ordering and fallback.
                query = """SELECT d.id, d.doc_key, (
                    SELECT s.origin_meta FROM kb_revisions r JOIN kb_sources s ON s.id = r.source_id
                    WHERE r.document_id = d.id ORDER BY r.rev_no DESC LIMIT 1)
                    FROM kb_documents d
                    WHERE NOT EXISTS (SELECT 1 FROM kb_document_access a WHERE a.document_id = d.id)
                    AND d.id > :after ORDER BY d.id LIMIT :n"""
                rows = self.connection.execute(text(query), {**parameters, "after": parameters["after"].hex}).all()
                return SimpleNamespace(all=lambda: [(uuid.UUID(d), k, json.loads(m) if m else None) for d, k, m in rows])
            statement = statement.columns(KbDocument.id, KbDocument.doc_key)
            parameters = {**parameters, "after": parameters["after"].hex}
        return self.connection.execute(statement, parameters or {})

    async def get(self, model, key):
        return self.objects.get((model, key))

    async def scalar(self, _statement):
        return 0

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass

    async def commit(self):
        self.connection.commit()

    def snapshot(self):
        return {table: self.connection.exec_driver_sql(f"SELECT * FROM {table}").all()
                for table in ("kb_documents", "kb_sources", "kb_revisions", "kb_observations")}

    def close(self):
        self.connection.close()
        self.engine.dispose()


@pytest.fixture
def store():
    db = BackfillStore()
    try:
        yield db
    finally:
        db.close()


@pytest.mark.asyncio
async def test_access_and_structure_backfills_classify_the_same_current_source(store, monkeypatch):
    before = store.snapshot()
    planned = await access_backfill.plan_backfill(store)
    current = await kb_backfill._latest_revision(store, uuid.UUID(int=1))
    assert current.rev_no == 1  # Actual shared currency query, not a fixed mock result.
    monkeypatch.setattr(kb_backfill, "segment_revision", AsyncMock())
    monkeypatch.setattr(kb_backfill, "enqueue_pipeline_job", AsyncMock())
    await kb_backfill.apply_plan(store, kb_backfill.Plan(current.document_id, "1", kb_backfill.ENQUEUE_ONLY,
                                                       "test", revision_id=current.id), "test", classify_legacy=True)
    assert store.added[0].scope == "work"
    assert store.added[0].visibility == "work_only"
    assert [(item.document_id.int, item.scope) for item in planned] == [(1, "work"), (2, "personal"), (3, "work")]
    assert store.snapshot() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 1, 2, 3, 4, None])
async def test_keyset_batches_and_limit_keep_existing_access_untouched(store, monkeypatch, limit):
    monkeypatch.setattr(access_backfill, "BATCH", 2)
    before = store.snapshot()
    items = await access_backfill.plan_backfill(store, limit=limit)
    assert [item.document_id.int for item in items] == [1, 2, 3][:limit]
    assert len([query for query in store.reads if "FROM kb_documents d" in query]) <= 3
    existing = await store.get(KbDocumentAccess, uuid.UUID(int=4))
    assert (existing.scope, existing.visibility, existing.hidden) == ("work", "work_only", True)
    assert store.snapshot() == before


@pytest.mark.asyncio
async def test_no_observation_uses_shared_revision_fallback(store):
    store.connection.exec_driver_sql("DELETE FROM kb_observations")
    items = await access_backfill.plan_backfill(store)
    assert [(item.document_id.int, item.scope) for item in items] == [(1, "personal"), (2, "personal"), (3, "work")]


@pytest.mark.asyncio
async def test_access_apply_is_idempotent_and_preserves_ledger_and_existing_access(store, monkeypatch):
    monkeypatch.setattr(access_backfill, "BATCH", 2)
    before = store.snapshot()
    report = await access_backfill.run_backfill(store, apply=True)
    assert (report.total, report.inserted, report.by_scope) == (3, 3, {"work": 2, "personal": 1})
    rows = store.connection.exec_driver_sql("SELECT scope, visibility, hidden FROM kb_document_access ORDER BY document_id").all()
    assert rows == [("work", "work_only", 0), ("personal", "global", 0), ("work", "work_only", 0), ("work", "work_only", 1)]
    again = await access_backfill.run_backfill(store, apply=True)
    assert (again.total, again.inserted) == (0, 0)
    assert store.snapshot() == before
