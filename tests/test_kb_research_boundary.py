"""Research evidence is retained without automatically becoming general knowledge."""

import uuid

import pytest

from ttakkari import kb_library, kb_retrieval, kb_store
from ttakkari.config import get_settings
from ttakkari.db import sessionmaker


@pytest.mark.parametrize("locator", ["/repo/docs/research/report.md", "session:old-request", "repo:commit:report.md"])
async def test_existing_research_sources_stay_out_of_general_library(locator):
    # Seed through the old ingestion contract: this also covers already deployed rows.
    async with sessionmaker()() as db:
        research = await kb_store.ingest_source(
            db, source_key="research:work:handoff", kind="research_text",
            title="Final acceptance and unfinished work", locator=locator,
            data=b"boundarytoken acceptance receipt and unfinished tasks",
            origin_meta={"research_scope": "work"},
        )
        [passage] = await kb_store.segment_revision(db, research.revision_id)
        reference = await kb_library.register_text(
            db, title="Reference manual", text="boundarytoken reference documentation",
            source_key="manual:reference", locator="https://example.org/manual", lang=None,
        )

        listing = await kb_library.list_documents(db, q=None, limit=100, offset=0)
        assert [row["id"] for row in listing["items"]] == [str(reference.document_id)]
        assert listing["total"] == 1
        assert (await kb_library.summary(db))["counts"]["documents"] == 1
        hits = await kb_library.search_passages(
            db, query="boundarytoken", limit=30, offset=0,
            document_id=None, current_only=True,
        )
        assert {row["document"]["id"] for row in hits["items"]} == {str(reference.document_id)}
        # Citation IDs remain usable; this is classification, never revocation/deletion.
        document = await kb_library.get_document(
            db, research.document_id, revision_id=None, passage_limit=100, passage_offset=0,
        )
        assert document["selected_revision_id"] == str(research.revision_id)
        evidence = await kb_library.get_evidence(db, passage.id, start=None, end=None)
        assert evidence["quote"] == passage.body
        await db.rollback()


@pytest.mark.parametrize("locator", ["/repo/report.md", "https://example.org/external-report"])
async def test_new_research_source_survives_resume_without_library_pollution(client, auth, locator):
    created = await client.post("/api/research/studies", headers=auth, json={
        "title": "Context retention", "goal": "Preserve receipts outside general knowledge",
    })
    assert created.status_code == 201, created.text
    study_id = created.json()["id"]
    source = await client.post(f"/api/research/studies/{study_id}/sources", headers=auth, json={
        "source_key": "final-receipt", "title": "Final acceptance receipt",
        "locator": locator, "body": "boundaryreceipt tasks remain unfinished.",
    })
    assert source.status_code == 201, source.text
    imported = source.json()
    assert imported["library_visibility"] == "research_only"
    unit = imported["units"][0]
    node = await client.post(f"/api/research/studies/{study_id}/nodes", headers=auth, json={
        "kind": "claim", "title": "External status receipt", "unit_id": unit["id"],
        "start": 0, "quote": unit["body"],
    })
    assert node.status_code == 201, node.text
    resumed = await client.get(f"/api/research/studies/{study_id}/resume", headers=auth)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["evidence"][0]["quote"] == unit["body"]
    async with sessionmaker()() as db:
        assert (await kb_library.list_documents(db, q=None, limit=100, offset=0))["total"] == 0
        doc = await kb_library.get_document(
            db, uuid.UUID(imported["revision"]["document_id"]),
            revision_id=None, passage_limit=100, passage_offset=0,
        )
        assert doc["passages"]["items"][0]["body"] == unit["body"]


async def test_legacy_external_references_remain_in_library():
    async with sessionmaker()() as db:
        ref = await kb_store.ingest_source(
            db, source_key="research:work:official-paper", kind="research_text",
            title="Official reference", locator="https://example.org/paper ; accessed 2026-10-27",
            data=b"Reference excerpt with exact source citation.",
        )
        await kb_store.segment_revision(db, ref.revision_id)
        assert (await kb_library.list_documents(db, q=None, limit=100, offset=0))["total"] == 1
        await db.rollback()


@pytest.mark.parametrize("locator", ["https://example.org/manual", "/repo/curated-guide.md"])
async def test_reference_registration_requires_explicit_purpose(client, auth, locator):
    created = await client.post("/api/research/studies", headers=auth, json={
        "title": "Reference import", "goal": "Explicitly retain reusable references",
    })
    sid = created.json()["id"]
    payload = {"source_key": "official-ref", "title": "Official reference",
               "locator": locator, "body": "Reference content.",
               "purpose": "knowledge_reference"}
    r = await client.post(f"/api/research/studies/{sid}/sources", headers=auth, json=payload)
    assert r.status_code == 201, r.text
    assert r.json()["library_visibility"] == "knowledge_reference"
    again = await client.post(f"/api/research/studies/{sid}/sources", headers=auth, json=payload)
    assert again.status_code == 201 and again.json()["revision"]["created"] is False
    async with sessionmaker()() as db:
        assert (await kb_library.list_documents(db, q=None, limit=100, offset=0))["total"] == 1
    payload.update(source_key="invalid-purpose", purpose="handoff")
    rejected = await client.post(f"/api/research/studies/{sid}/sources", headers=auth, json=payload)
    assert rejected.status_code == 422, rejected.text


async def test_idempotent_replay_cannot_silently_reclassify_context(client, auth):
    created = await client.post("/api/research/studies", headers=auth, json={
        "title": "Placement conflict", "goal": "Keep immutable placement honest",
    })
    sid = created.json()["id"]
    payload = {"source_key": "receipt", "title": "External receipt",
               "locator": "https://example.org/receipt", "body": "Task receipt."}
    first = await client.post(f"/api/research/studies/{sid}/sources", headers=auth, json=payload)
    assert first.status_code == 201, first.text
    assert first.json()["library_visibility"] == "research_only"
    payload["purpose"] = "knowledge_reference"
    conflict = await client.post(f"/api/research/studies/{sid}/sources", headers=auth, json=payload)
    assert conflict.status_code == 409, conflict.text
    assert "purpose_conflict" in conflict.text
    async with sessionmaker()() as db:
        assert (await kb_library.list_documents(db, q=None, limit=100, offset=0))["total"] == 0


async def test_hybrid_search_excludes_already_indexed_research_context(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_semantic_enabled", True)
    async with sessionmaker()() as db:
        research = await kb_store.ingest_source(
            db, source_key="research:personal:old-receipt", kind="research_text",
            title="Old receipt", data=b"boundarytoken acceptance receipt",
        )
        await kb_store.segment_revision(db, research.revision_id)
        reference = await kb_library.register_text(
            db, title="Reference", text="boundarytoken reference manual",
            source_key=None, locator=None, lang=None,
        )
        await db.commit()
    # Simulate a semantic projection created before the boundary was introduced.
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ttakkari.kb_models import KbPassage
    from ttakkari.kb_vector_models import KbPassageVector

    async with sessionmaker()() as db:
        passages = list(await db.scalars(select(KbPassage)))
        vectors = await kb_retrieval.embed_complete([p.body for p in passages])
        for passage, vector in zip(passages, vectors, strict=True):
            await db.execute(insert(KbPassageVector).values(
                passage_id=passage.id, model_key=kb_retrieval.model_key(),
                text_sha256=passage.sha256, vector=vector,
            ))
        await db.commit()
        hits = await kb_retrieval.search(db, query="boundarytoken")
        assert {row["document"]["id"] for row in hits["items"]} == {str(reference.document_id)}
        assert hits["retrieval"]["total"] == 1
