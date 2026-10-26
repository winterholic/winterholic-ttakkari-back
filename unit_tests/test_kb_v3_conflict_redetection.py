"""Detector behavior with DB I/O doubles; no PostgreSQL integration is claimed.

The real duplicate-pair SQL is executed against a tiny in-memory SQLite table.
Assertion/evidence/currency reads are fixed I/O doubles for two current assertions.
"""

import sqlite3
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import sqlite

from ttakkari.kb_models import KbRevision
from ttakkari.kb_ontology import conflicts
from ttakkari.kb_ontology_models import KbAssertion, KbConflict, KbEntity


class ConflictStore:
    def __init__(self, status, *, resolved_by="user", replace_assertion=False):
        self.entity = KbEntity(id=uuid.uuid4(), status="active")
        self.rows = []
        for value in ("5432", "5433"):
            doc_id, revision_id = uuid.uuid4(), uuid.uuid4()
            assertion = KbAssertion(
                id=uuid.uuid4(), subject_entity_id=self.entity.id,
                document_id=doc_id, revision_id=revision_id, predicate="port",
                norm_object_value=value, polarity="positive", modality="fact",
                applies_condition={}, visibility="global", status="active",
            )
            self.rows.append((assertion, KbRevision(id=revision_id, document_id=doc_id)))
        self.previous = KbConflict(
            id=uuid.uuid4(), entity_id=self.entity.id,
            assertion_a_id=self.rows[0][0].id,
            assertion_b_id=uuid.uuid4() if replace_assertion else self.rows[1][0].id,
            status=status, resolved_by=resolved_by,
        )
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute(
            "CREATE TABLE kb_conflicts (id TEXT, entity_id TEXT, status TEXT, resolved_by TEXT)"
        )
        self.connection.execute(
            "INSERT INTO kb_conflicts VALUES (?, ?, ?, ?)",
            (self.previous.id.hex, self.entity.id.hex, status, resolved_by),
        )
        self.added = []

    async def get(self, model, key):
        assert model is KbEntity and key == self.entity.id
        return self.entity

    async def execute(self, statement):
        return SimpleNamespace(all=lambda: self.rows)

    async def scalars(self, statement):
        query = statement.with_only_columns(KbConflict.id).compile(
            dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}
        )
        ids = {row[0] for row in self.connection.execute(str(query))}
        return [self.previous] if self.previous.id.hex in ids else []

    def add(self, conflict):
        self.added.append(conflict)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "resolved_by", "replace_assertion", "expected_count"),
    [
        ("resolved_both_valid", "user", False, 0),
        ("resolved_both_valid", None, False, 0),
        ("open", None, False, 0),
        ("resolved_both_valid", "user", True, 1),
        ("kept_separate", "system:revision_supersession", False, 1),
        ("resolved_both_valid", "system:revision_supersession", False, 1),
    ],
)
async def test_redetection_preserves_same_pair_decisions_without_hiding_new_conflicts(
    monkeypatch, status, resolved_by, replace_assertion, expected_count
):
    db = ConflictStore(status, resolved_by=resolved_by, replace_assertion=replace_assertion)
    current = {assertion.document_id: revision.id for assertion, revision in db.rows}
    monkeypatch.setattr(
        conflicts, "_current_revision_id", AsyncMock(side_effect=lambda _db, doc_id: current[doc_id])
    )
    try:
        found = await conflicts.detect_conflicts_for_entity(db, db.entity.id)
        assert len(found) == expected_count
        assert db.added == found
        if not expected_count and status == "resolved_both_valid":
            assert all(assertion.status == "active" for assertion, _ in db.rows)
        if expected_count:
            assert found[0].status == "open"
            assert found[0].assertion_b_id == db.rows[1][0].id
    finally:
        db.connection.close()
