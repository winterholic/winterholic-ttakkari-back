"""API response contracts with DB I/O doubles; no PG or live-server claim."""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine

from ttakkari import kb_ontology, kb_v3_views
from ttakkari.api import kb_library as api
from ttakkari.db import get_db
from ttakkari.kb_access import create_web_context
from ttakkari.kb_models import KbDocument, KbRevision
from ttakkari.kb_ontology_models import KbAssertion, KbConflict, KbEntity
from ttakkari.security.auth import require_user


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "resolved_by", "note"),
    [("kept_separate", "system:revision_supersession", "both_parties_superseded_by_newer_revision"),
     ("kept_separate", "user", "different concepts"), ("open", None, None)],
)
async def test_document_detail_retains_conflict_resolution_metadata(monkeypatch, status, resolved_by, note):
    doc_id, rev_id, entity_id, assertion_id = (uuid.uuid4() for _ in range(4))
    conflict = KbConflict(id=uuid.uuid4(), entity_id=entity_id, assertion_a_id=assertion_id,
                          status=status, conflict_kind="value_contradiction", resolved_by=resolved_by, resolution_note=note)
    entity = KbEntity(id=entity_id, canonical_name="Database")
    revision = KbRevision(id=rev_id, source_id=uuid.uuid4())
    doc = KbDocument(id=doc_id)
    assertion = KbAssertion(id=assertion_id)

    class DetailStore:
        async def get(self, model, key):
            return {KbRevision: revision, KbDocument: doc, KbEntity: entity}[model]

        async def execute(self, statement):
            return SimpleNamespace(all=list)

        async def scalars(self, statement):
            return [assertion] if statement.column_descriptions[0]["entity"] is KbAssertion else []

        async def scalar(self, statement):
            return None

    monkeypatch.setattr(kb_v3_views.lib, "get_document", AsyncMock(return_value={
        "selected_revision_id": str(rev_id), "title": "Database",
        "revisions": [{"id": str(rev_id), "rev_no": 1, "source": {"locator": "https://example.test"}}],
    }))
    monkeypatch.setattr(kb_v3_views.lib, "_document_row", AsyncMock(return_value={"id": str(doc_id)}))
    monkeypatch.setattr(kb_v3_views, "assertion_views", AsyncMock(return_value=[{"id": str(assertion_id)}]))
    monkeypatch.setattr(kb_ontology, "list_conflicts_with_total", AsyncMock(return_value=([conflict], 1)))
    result = await kb_v3_views.document_detail_v3(
        DetailStore(), create_web_context(), doc_id, revision_id=None, passage_limit=100, passage_offset=0
    )
    assert result["conflicts"] == [{"id": str(conflict.id), "kind": "value_contradiction", "status": status,
                                    "entity_name": "Database", "resolved_by": resolved_by, "note": note}]
    assert result["assertions"] == [{"id": str(assertion_id)}]
    assert result["source"] == {"locator": "https://example.test"}


class ConflictReadStore:
    """Run the real list/filter SQL in SQLite, hydrate model objects at the I/O edge.

    The real conflict accessibility check and response renderer both run. These
    ambiguous-entity fixtures need no assertion/provenance I/O and include a
    forbidden candidate, another namespace, and work-only entities.
    """

    def __init__(self):
        self.engine = create_engine("sqlite://")
        self.connection = self.engine.connect()
        self.connection.exec_driver_sql("CREATE TABLE kb_entities (id TEXT PRIMARY KEY, tenant_id TEXT)")
        self.connection.exec_driver_sql("CREATE TABLE kb_conflicts (id TEXT PRIMARY KEY, entity_id TEXT, visibility TEXT, "
                                        "status TEXT, conflict_kind TEXT, created_at TIMESTAMP)")
        self.objects = {}
        for number, visibility, tenant in ((1, "global", "personal"), (2, "global", "personal"),
                                            (3, "work_only", "personal"), (4, "global", "other")):
            entity = KbEntity(id=uuid.UUID(int=number), canonical_name=f"Entity {number}", tenant_id=tenant,
                              visibility=visibility, entity_kind="concept", document_count=1, domain="general")
            self.objects[KbEntity, entity.id] = entity
            self.connection.exec_driver_sql("INSERT INTO kb_entities VALUES (?, ?)", (entity.id.hex, tenant))
        for number, entity, candidate, visibility in ((1, 1, 2, "global"), (2, 2, 1, "global"),
                                                      (3, 3, 3, "work_only"), (4, 1, 3, "global"), (5, 4, 4, "global")):
            conflict = KbConflict(id=uuid.UUID(int=100 + number), entity_id=uuid.UUID(int=entity),
                                  candidate_entity_id=uuid.UUID(int=candidate), visibility=visibility,
                                  status="open", conflict_kind="ambiguous_entity", created_at=datetime(2026, 10, 26, tzinfo=UTC))
            self.objects[KbConflict, conflict.id] = conflict
            self.connection.exec_driver_sql("INSERT INTO kb_conflicts VALUES (?, ?, ?, 'open', 'ambiguous_entity', ?)",
                                            (conflict.id.hex, conflict.entity_id.hex, visibility, conflict.created_at.isoformat()))

    async def scalars(self, statement):
        model = statement.column_descriptions[0]["entity"]
        ids = self.connection.execute(statement.with_only_columns(model.id)).scalars()
        return [self.objects[model, key] for key in ids]

    async def get(self, model, key):
        return self.objects.get((model, key))

    def close(self):
        self.connection.close()
        self.engine.dispose()


@pytest.fixture
def conflict_app():
    db = ConflictReadStore()
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_user] = lambda: "owner"
    app.dependency_overrides[get_db] = lambda: db
    try:
        yield app
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_ids", "total"),
    [("", [101, 102], 2), (f"entity_id={uuid.UUID(int=1)}", [101], 1),
     (f"entity_id={uuid.UUID(int=2)}", [102], 1), (f"entity_id={uuid.UUID(int=99)}", [], 0),
     (f"entity_id={uuid.UUID(int=1)}&offset=1&limit=1", [], 1),
     (f"entity_id={uuid.UUID(int=3)}", [], 0), ("scope=work", [103], 1),
     (f"entity_id={uuid.UUID(int=3)}&scope=work", [103], 1),
     (f"entity_id={uuid.UUID(int=1)}&kind=value_contradiction", [], 0)],
)
async def test_conflicts_http_entity_filter_preserves_access_totals_and_pagination(conflict_app, query, expected_ids, total):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=conflict_app), base_url="http://unit.test") as client:
        response = await client.get("/api/assistant/kb/conflicts?" + query)
    assert response.status_code == 200, response.text
    data = response.json()
    assert [uuid.UUID(item["id"]).int for item in data["items"]] == expected_ids
    assert data["total"] == total
    assert all(item["entity_name"] and item["entity_a"] and item["entity_b"] for item in data["items"])


@pytest.mark.asyncio
async def test_conflicts_http_rejects_invalid_entity_uuid(conflict_app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=conflict_app), base_url="http://unit.test") as client:
        response = await client.get("/api/assistant/kb/conflicts?entity_id=bad-uuid")
    assert response.status_code == 422
