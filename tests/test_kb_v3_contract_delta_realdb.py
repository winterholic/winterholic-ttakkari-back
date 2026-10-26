"""PostgreSQL/API regressions for currency and conflict contract follow-up."""

import uuid
from datetime import UTC, datetime

from test_kb_v3_api_and_mcp import (
    BASE,
    EN_DEF_OPEN,
    EN_DEF_VENDOR,
    _ingest_text_and_drain,
)
from test_kb_v3_integration_flow import _env  # noqa: F401

from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.db import sessionmaker
from ttakkari.kb_access_backfill import run_backfill
from ttakkari.kb_ontology.conflicts import detect_conflicts_for_entity
from ttakkari.kb_ontology.currency import current_revision_id
from ttakkari.kb_store import ingest_source


async def test_access_backfill_uses_observed_current_work_source_in_postgres():
    async with sessionmaker()() as db:
        key = f"legacy:scope-inversion:{uuid.uuid4()}"
        revisions = []
        for day, scope in ((10, "work"), (1, "personal")):
            result = await ingest_source(
                db, source_key=key, data=f"Scope snapshot {day}".encode(),
                kind="web", title="legacy scope inversion", media_type="text/plain",
                origin_meta={"via": "collector", "scope": scope},
                collected_at=datetime(2026, 10, day, tzinfo=UTC),
            )
            db.add(KbObservation(
                document_id=result.document_id, revision_id=result.revision_id,
                envelope_id=uuid.uuid4(), event_time=datetime(2026, 10, day, tzinfo=UTC),
                provenance={"test": "backfill_scope_inversion"},
            ))
            revisions.append(result)
        await db.commit()
        assert revisions[0].document_id == revisions[1].document_id
        document_id = revisions[0].document_id
        assert await current_revision_id(db, document_id) == revisions[0].revision_id

        dry = await run_backfill(db, apply=False)
        assert dry.by_scope == {"work": 1}
        assert await db.get(KbDocumentAccess, document_id) is None
        applied = await run_backfill(db, apply=True)
        assert applied.inserted == 1
        access = await db.get(KbDocumentAccess, document_id)
        assert (access.scope, access.origin_scope, access.visibility) == ("work", "work", "work_only")
        again = await run_backfill(db, apply=True)
        assert again.total == again.inserted == 0


async def test_conflict_filter_resolution_metadata_and_redetection_with_real_db(client, auth):
    await _ingest_text_and_drain(client, auth, EN_DEF_OPEN, "open")
    await _ingest_text_and_drain(client, auth, EN_DEF_VENDOR, "vendor")
    listed = (await client.get(f"{BASE}/conflicts", headers=auth)).json()
    assert listed["total"] == 1
    conflict = listed["items"][0]
    entity_id = conflict["entity_id"]

    matching = await client.get(f"{BASE}/conflicts", headers=auth, params={"entity_id": entity_id})
    assert matching.status_code == 200 and matching.json()["total"] == 1
    other = await client.get(f"{BASE}/conflicts", headers=auth, params={"entity_id": str(uuid.uuid4())})
    assert other.status_code == 200 and other.json()["total"] == 0
    invalid = await client.get(f"{BASE}/conflicts", headers=auth, params={"entity_id": "invalid"})
    assert invalid.status_code == 422

    resolved = await client.post(
        f"{BASE}/conflicts/{conflict['id']}/resolve", headers=auth,
        json={"resolution": "resolved_both_valid", "note": "independent PG regression"},
    )
    assert resolved.status_code == 200
    async with sessionmaker()() as db:
        assert await detect_conflicts_for_entity(db, uuid.UUID(entity_id)) == []
        await db.commit()
    assert (await client.get(f"{BASE}/conflicts", headers=auth)).json()["total"] == 0
    for claim in (conflict["claim_a"], conflict["claim_b"]):
        detail = (await client.get(f"{BASE}/documents/{claim['document_id']}", headers=auth)).json()
        item = next(item for item in detail["conflicts"] if item["id"] == conflict["id"])
        assert item["resolved_by"] == "user"
        assert item["note"] == "independent PG regression"
