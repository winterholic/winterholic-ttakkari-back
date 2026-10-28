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


async def test_mixed_history_context_then_reference_appears_in_library():
    """P2 defect regression: context -> reference mixed history must show document and reference passages, but hide context passages."""
    async with sessionmaker()() as db:
        # Rev 1: Legacy local research task receipt
        rev1 = await kb_store.ingest_source(
            db, source_key="doc:mixed-evolution", kind="research_text",
            title="Local task receipt", locator="/repo/work/receipt.md",
            data=b"firstmixedcontext unique_receipt_token task completed",
            origin_meta={},
        )
        [p1] = await kb_store.segment_revision(db, rev1.revision_id)

        # Rev 2: Subsequent reference manual on the same document lineage
        rev2 = await kb_store.ingest_source(
            db, source_key="doc:mixed-evolution", kind="research_text",
            title="Official reference guide", locator="https://example.org/guide",
            data=b"secondmixedreference unique_guide_token official documentation",
            origin_meta={"library_purpose": "knowledge_reference"},
        )
        [p2] = await kb_store.segment_revision(db, rev2.revision_id)
        assert rev1.document_id == rev2.document_id

        # 1. Document listing: current revision is reference, so document MUST appear
        listing = await kb_library.list_documents(db, q=None, limit=100, offset=0)
        assert listing["total"] == 1
        assert listing["items"][0]["id"] == str(rev2.document_id)
        assert listing["items"][0]["current_revision"]["id"] == str(rev2.revision_id)

        # 2. Search current: reference guide is discoverable
        hits_cur = await kb_library.search_passages(
            db, query="unique_guide_token", limit=10, offset=0,
            document_id=None, current_only=True,
        )
        assert len(hits_cur["items"]) == 1
        assert hits_cur["items"][0]["passage_id"] == str(p2.id)

        # 3. Search historical (current_only=False): context task receipt must NOT leak
        hits_hist = await kb_library.search_passages(
            db, query="unique_receipt_token", limit=10, offset=0,
            document_id=None, current_only=False,
        )
        assert len(hits_hist["items"]) == 0

        # 4. Summary counts: only the valid reference revision/passage/source is counted in knowledge library
        summary = await kb_library.summary(db)
        assert summary["counts"]["documents"] == 1
        assert summary["counts"]["revisions"] == 1
        assert summary["counts"]["passages"] == 1
        assert summary["counts"]["sources"] == 1

        # 5. Direct citation / byte inspection: rev1 and rev2 remain fully intact
        doc = await kb_library.get_document(
            db, rev1.document_id, revision_id=rev1.revision_id, passage_limit=10, passage_offset=0,
        )
        assert doc["selected_revision_id"] == str(rev1.revision_id)
        ev1 = await kb_library.get_evidence(db, p1.id, start=None, end=None)
        assert ev1["quote"] == p1.body
        assert ev1["span"]["hash_verified"] is True
        ev2 = await kb_library.get_evidence(db, p2.id, start=None, end=None)
        assert ev2["quote"] == p2.body
        assert ev2["span"]["hash_verified"] is True

        await db.rollback()


async def test_mixed_history_reference_then_context_hidden_from_library():
    """Reference -> context mixed history: when latest revision is context, document is excluded and context text does not leak."""
    async with sessionmaker()() as db:
        # Rev 1: Initial reference
        rev1 = await kb_store.ingest_source(
            db, source_key="doc:mixed-demoted", kind="research_text",
            title="Initial reference", locator="https://example.org/paper",
            data=b"initialreference unique_initial_ref_token",
            origin_meta={"library_purpose": "knowledge_reference"},
        )
        await kb_store.segment_revision(db, rev1.revision_id)

        # Rev 2: Replaced by work context
        rev2 = await kb_store.ingest_source(
            db, source_key="doc:mixed-demoted", kind="research_text",
            title="Overriding work notes", locator="/local/scratch/notes.md",
            data=b"overridingcontext unique_work_notes_token task handoff",
            origin_meta={"library_purpose": "research_context"},
        )
        [p2] = await kb_store.segment_revision(db, rev2.revision_id)
        assert rev1.document_id == rev2.document_id

        # Document listing: current revision is context, so document MUST be hidden
        listing = await kb_library.list_documents(db, q=None, limit=100, offset=0)
        assert listing["total"] == 0

        # General search must not leak overriding context
        hits = await kb_library.search_passages(
            db, query="unique_work_notes_token", limit=10, offset=0,
            document_id=None, current_only=False,
        )
        assert len(hits["items"]) == 0

        # Direct access remains intact
        doc = await kb_library.get_document(
            db, rev2.document_id, revision_id=None, passage_limit=10, passage_offset=0,
        )
        assert doc["selected_revision_id"] == str(rev2.revision_id)
        ev = await kb_library.get_evidence(db, p2.id, start=None, end=None)
        assert ev["quote"] == p2.body
        await db.rollback()


async def test_mixed_history_http_search_and_aggregate_boundaries(client, auth):
    """HTTP list/search/summary apply placement at the current-document and every-revision levels."""
    async with sessionmaker()() as db:
        context_then_reference = await kb_store.ingest_source(
            db, source_key="doc:http-context-to-reference", kind="research_text",
            title="HTTP current reference", locator="https://example.org/http-current-ref",
            data=b"olddocumentcontextleakmarker", origin_meta={"library_purpose": "research_context"},
        )
        await kb_store.segment_revision(db, context_then_reference.revision_id)
        current_reference = await kb_store.ingest_source(
            db, source_key="doc:http-context-to-reference", kind="research_text",
            title="HTTP current reference", locator="https://example.org/http-current-ref",
            data=b"currentreferenceonlymarker", origin_meta={"library_purpose": "knowledge_reference"},
        )
        await kb_store.segment_revision(db, current_reference.revision_id)

        reference_then_context = await kb_store.ingest_source(
            db, source_key="doc:http-reference-to-context", kind="research_text",
            title="HTTP current context", locator="https://example.org/http-current-context",
            data=b"priorreferencedemotedmarker", origin_meta={"library_purpose": "knowledge_reference"},
        )
        await kb_store.segment_revision(db, reference_then_context.revision_id)
        current_context = await kb_store.ingest_source(
            db, source_key="doc:http-reference-to-context", kind="research_text",
            title="HTTP current context", locator="/repo/http-current-context.md",
            data=b"newcontextnotdiscoverablemarker", origin_meta={"library_purpose": "research_context"},
        )
        await kb_store.segment_revision(db, current_context.revision_id)
        await db.commit()

    listing = await client.get("/api/assistant/kb/documents", headers=auth)
    assert listing.status_code == 200, listing.text
    assert [row["id"] for row in listing.json()["items"]] == [str(current_reference.document_id)]

    historical_context = await client.get(
        "/api/assistant/kb/search", params={"q": "olddocumentcontextleakmarker", "current_only": "false"}, headers=auth,
    )
    assert historical_context.status_code == 200, historical_context.text
    assert historical_context.json()["items"] == []
    current_ref = await client.get(
        "/api/assistant/kb/search", params={"q": "currentreferenceonlymarker", "current_only": "true"}, headers=auth,
    )
    assert current_ref.status_code == 200, current_ref.text
    assert {row["document"]["id"] for row in current_ref.json()["items"]} == {str(current_reference.document_id)}
    historical_reference = await client.get(
        "/api/assistant/kb/search", params={"q": "priorreferencedemotedmarker", "current_only": "false"}, headers=auth,
    )
    assert historical_reference.status_code == 200, historical_reference.text
    assert historical_reference.json()["items"] == []
    summary = await client.get("/api/assistant/kb/summary", headers=auth)
    assert summary.status_code == 200, summary.text
    assert summary.json()["counts"]["documents"] == 1
    assert summary.json()["counts"]["revisions"] == 1
    assert summary.json()["counts"]["passages"] == 1


@pytest.mark.parametrize("locator", ["/local/shared/handbook.md", "session:architecture-review-2026"])
async def test_explicit_knowledge_reference_with_local_and_session_locators(locator):
    """Explicit knowledge_reference purpose is preserved even with local or session locators."""
    async with sessionmaker()() as db:
        ref = await kb_store.ingest_source(
            db, source_key=f"doc:explicit:{locator}", kind="research_text",
            title="Explicit local knowledge", locator=locator,
            data=b"explicitlocal unique_explicit_local_token architecture guide",
            origin_meta={"library_purpose": "knowledge_reference"},
        )
        [p] = await kb_store.segment_revision(db, ref.revision_id)

        listing = await kb_library.list_documents(db, q=None, limit=100, offset=0)
        assert listing["total"] == 1
        assert listing["items"][0]["id"] == str(ref.document_id)

        hits = await kb_library.search_passages(
            db, query="unique_explicit_local_token", limit=10, offset=0,
            document_id=None, current_only=True,
        )
        assert len(hits["items"]) == 1
        assert hits["items"][0]["passage_id"] == str(p.id)
        await db.rollback()


async def test_canonical_and_covered_alias_with_research_boundary():
    """Covered duplicate is hidden from library filter, while canonical document remains."""
    from ttakkari.collector.control_models import KbDocumentAlias

    async with sessionmaker()() as db:
        # Canonical reference document
        canonical = await kb_store.ingest_source(
            db, source_key="doc:canonical-ref", kind="research_text",
            title="Canonical reference", locator="https://example.org/canonical",
            data=b"canonical content unique_alias_token",
            origin_meta={"library_purpose": "knowledge_reference"},
        )
        await kb_store.segment_revision(db, canonical.revision_id)

        # Duplicate reference document marked as covered
        duplicate = await kb_store.ingest_source(
            db, source_key="doc:duplicate-ref", kind="research_text",
            title="Duplicate reference", locator="https://example.org/canonical-mirror",
            data=b"canonical content unique_alias_token",
            origin_meta={"library_purpose": "knowledge_reference"},
        )
        await kb_store.segment_revision(db, duplicate.revision_id)

        db.add(KbDocumentAlias(
            document_id=duplicate.document_id,
            canonical_document_id=canonical.document_id,
            relation="covered",
            reason="duplicate_content",
            run_id="test-kb-research-boundary",
        ))
        await db.flush()

        listing = await kb_library.list_documents(db, q=None, limit=100, offset=0)
        assert listing["total"] == 1
        assert listing["items"][0]["id"] == str(canonical.document_id)
        await db.rollback()


async def test_covered_alias_direct_id_citation_and_research_resume(client, auth, monkeypatch):
    """A covered alias stays hidden from general retrieval while direct citations and study resume remain usable."""
    from hashlib import sha256

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert

    from ttakkari.collector.control_models import KbDocumentAlias
    from ttakkari.kb_models import KbPassage, KbRevision
    from ttakkari.kb_store import object_relpath, originals_root
    from ttakkari.kb_vector_models import KbPassageVector

    reference_bytes = b"canonical source exact bytes; unique_alias_direct_id_token"
    async with sessionmaker()() as db:
        canonical = await kb_store.ingest_source(
            db, source_key="doc:direct-id-canonical", kind="research_text",
            title="Canonical reference", locator="https://example.org/canonical-direct-id",
            data=reference_bytes, origin_meta={"library_purpose": "knowledge_reference"},
        )
        await kb_store.segment_revision(db, canonical.revision_id)
        alias = await kb_store.ingest_source(
            db, source_key="doc:direct-id-covered-alias", kind="research_text",
            title="Covered alias reference", locator="https://example.org/covered-alias",
            data=reference_bytes, origin_meta={"library_purpose": "knowledge_reference"},
        )
        [alias_passage] = await kb_store.segment_revision(db, alias.revision_id)
        alias_passage_id = alias_passage.id
        db.add(KbDocumentAlias(
            document_id=alias.document_id, canonical_document_id=canonical.document_id,
            relation="covered", reason="duplicate_content", run_id="test-kb-research-boundary",
        ))
        await db.commit()

        # The original object and DB revision remain byte-for-byte linked by the stored digest.
        source_path = originals_root() / object_relpath(alias.sha256)
        assert source_path.read_bytes() == reference_bytes
        assert sha256(source_path.read_bytes()).hexdigest() == alias.sha256
        alias_revision = await db.get(KbRevision, alias.revision_id)
        assert alias_revision.body.encode("utf-8") == reference_bytes

        listing = await kb_library.list_documents(db, q=None, limit=100, offset=0)
        assert [row["id"] for row in listing["items"]] == [str(canonical.document_id)]
        current = await kb_library.search_passages(
            db, query="unique_alias_direct_id_token", limit=20, offset=0,
            document_id=None, current_only=True,
        )
        assert {row["document"]["id"] for row in current["items"]} == {str(canonical.document_id)}
        historical = await kb_library.search_passages(
            db, query="unique_alias_direct_id_token", limit=20, offset=0,
            document_id=None, current_only=False,
        )
        assert historical["items"]
        assert {row["document"]["id"] for row in historical["items"]} == {str(canonical.document_id)}
        summary = await kb_library.summary(db)
        assert summary["counts"]["documents"] == 1
        assert summary["counts"]["revisions"] == 1
        assert summary["counts"]["passages"] == 1
        assert summary["counts"]["sources"] == 1
        await db.rollback()

    # Direct ID read and citation intentionally bypass library visibility filtering.
    detail = await client.get(
        f"/api/assistant/kb/documents/{alias.document_id}?revision_id={alias.revision_id}", headers=auth,
    )
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == str(alias.document_id)
    assert detail.json()["passages"]["items"][0]["body"].encode("utf-8") == reference_bytes
    evidence = await client.get(
        f"/api/assistant/kb/passages/{alias_passage_id}/evidence", headers=auth,
    )
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["quote"].encode("utf-8") == reference_bytes
    assert evidence.json()["span"]["hash_verified"] is True
    assert evidence.json()["span"]["sha256"] == sha256(reference_bytes).hexdigest()

    # Existing vectors on a covered alias must not bypass the same visibility boundary.
    monkeypatch.setattr(get_settings(), "kb_semantic_enabled", True)
    async with sessionmaker()() as db:
        passages = list(await db.scalars(select(KbPassage)))
        vectors = await kb_retrieval.embed_complete([p.body for p in passages])
        for passage, vector in zip(passages, vectors, strict=True):
            await db.execute(insert(KbPassageVector).values(
                passage_id=passage.id, model_key=kb_retrieval.model_key(),
                text_sha256=passage.sha256, vector=vector,
            ))
        await db.commit()
        hits = await kb_retrieval.search(db, query="unique_alias_direct_id_token")
        assert hits["retrieval"]["indexed"] == 1
        assert {row["document"]["id"] for row in hits["items"]} == {str(canonical.document_id)}

    # Research context stays retrievable by its study evidence ID and exact quote after resume.
    created = await client.post("/api/research/studies", headers=auth, json={
        "title": "Alias boundary resume", "goal": "Keep task context citable without library promotion",
    })
    assert created.status_code == 201, created.text
    study_id = created.json()["id"]
    source = await client.post(f"/api/research/studies/{study_id}/sources", headers=auth, json={
        "source_key": "covered-alias-resume-context", "title": "Task receipt",
        "locator": "/repo/task/receipt.md", "body": "Research-only receipt exact quote survives resume.",
    })
    assert source.status_code == 201, source.text
    unit = source.json()["units"][0]
    node = await client.post(f"/api/research/studies/{study_id}/nodes", headers=auth, json={
        "kind": "claim", "title": "Receipt", "unit_id": unit["id"],
        "start": 0, "quote": unit["body"],
    })
    assert node.status_code == 201, node.text
    resumed = await client.get(f"/api/research/studies/{study_id}/resume", headers=auth)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["evidence"][0]["quote"] == unit["body"]
    async with sessionmaker()() as db:
        listing = await kb_library.list_documents(db, q=None, limit=100, offset=0)
        assert {row["id"] for row in listing["items"]} == {str(canonical.document_id)}
