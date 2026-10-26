import pytest


@pytest.fixture(autouse=True)
def enable_semantic():
    from ttakkari.config import get_settings
    s = get_settings()
    old = s.kb_semantic_enabled
    s.kb_semantic_enabled = True
    yield
    s.kb_semantic_enabled = old


async def register(client, auth, text, scope="personal"):
    r = await client.post("/api/assistant/kb/documents", headers=auth,
                          json={"title": text[:30], "text": text, "scope": scope})
    assert r.status_code == 201
    return r.json()


async def test_search_mode_is_validated(client, auth):
    r = await client.get("/api/assistant/kb/search?q=test&mode=invalid", headers=auth)
    assert r.status_code == 422


async def test_hybrid_projection_is_idempotent_and_scope_filtered(client, auth):
    one = await register(client, auth, "트랜잭션 격리 수준과 직렬화 충돌을 설명한다.")
    two = await register(client, auth, "기상청은 남부 지역에 많은 비가 내릴 것으로 예측했다.")
    private = await register(client, auth, "트랜잭션 격리 회사전용기록", scope="work")
    from ttakkari import kb_retrieval
    assert await kb_retrieval.index_pending(limit=30) == 3
    assert await kb_retrieval.index_pending(limit=30) == 0
    hits = await client.get("/api/assistant/kb/search?q=트랜잭션&mode=hybrid", headers=auth)
    assert hits.status_code == 200, hits.text
    body = hits.json()
    assert body["retrieval"]["quality_gate"] == "unverified"
    assert body["retrieval"]["model"] == "hash"
    assert body["items"][0]["document"]["id"] == one["document_id"]
    assert private["document_id"] not in {item["document"]["id"] for item in body["items"]}
    assert two["document_id"] != one["document_id"]


async def test_metadata_source_and_time_filters_run_on_postgres(client, auth):
    await register(client, auth, "deployment rollback search metadata filter")
    from ttakkari import kb_retrieval
    from ttakkari.db import sessionmaker

    async with sessionmaker()() as db:
        result = await kb_retrieval.search(
            db,
            query="deployment rollback",
            mode="lexical",
            filters={
                "type": "upload",
                "collected_after": "2000-01-01T00:00:00+00:00",
            },
        )
    assert result["items"]


async def test_entity_alias_graph_and_canonical_paths_use_source_evidence(client, auth):
    import hashlib

    from sqlalchemy import select

    from ttakkari import kb_retrieval
    from ttakkari.common.entity_norm import norm_name
    from ttakkari.db import sessionmaker
    from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision
    from ttakkari.kb_ontology_models import (
        KbDocumentEntity,
        KbEntity,
        KbEntityAlias,
        KbEntityCanonical,
        KbRelation,
        KbRelationEvidence,
    )

    body = "Aurora (오로라) manages Nimbus service."
    registered = await register(client, auth, body)
    work_registered = await register(
        client, auth, "Work-only project codename: Polaris.", scope="work"
    )
    async with sessionmaker()() as db:
        document = await db.get(KbDocument, registered["document_id"])
        revision = await db.get(KbRevision, registered["revision_id"])
        passage = await db.scalar(
            select(KbPassage).where(KbPassage.revision_id == revision.id)
        )
        # 실제 파이프라인(해소기·추출기)이 쓰는 온톨로지 tenant 는 "personal" 이다. 문서 접근 tenant("owner")와 다른 축이다.
        aurora = KbEntity(
            tenant_id="personal", origin_scope="personal", visibility="global",
            canonical_name="Aurora", norm_name=norm_name("Aurora"),
            entity_kind="concept", domain="ai", summary="Aurora service",
        )
        nimbus = KbEntity(
            tenant_id="personal", origin_scope="personal", visibility="global",
            canonical_name="Nimbus", norm_name=norm_name("Nimbus"),
            entity_kind="concept", domain="ai", summary="Nimbus service",
        )
        db.add_all([aurora, nimbus])
        await db.flush()
        evidence_body = "Aurora (오로라) manages Nimbus"
        span_start = body.index(evidence_body)
        evidence = KbEvidence(
            passage_id=passage.id,
            span_start=span_start,
            span_end=span_start + len(evidence_body),
            sha256=hashlib.sha256(evidence_body.encode()).hexdigest(),
            note="fixture source evidence",
        )
        db.add(evidence)
        await db.flush()
        db.add_all([
            KbEntityAlias(
                entity_id=aurora.id, alias="오로라", norm_alias=norm_name("오로라"),
                alias_kind="synonym", source="extracted",
            ),
            KbDocumentEntity(
                passage_id=passage.id, entity_id=aurora.id,
                document_id=document.id, revision_id=revision.id, evidence_id=evidence.id,
            ),
            KbDocumentEntity(
                passage_id=passage.id, entity_id=nimbus.id,
                document_id=document.id, revision_id=revision.id, evidence_id=evidence.id,
            ),
            KbEntityCanonical(
                entity_id=aurora.id, visibility="global",
                canonical_definition="Aurora manages Nimbus",
                source_document_ids=[str(document.id)],
                supporting_evidence_ids=[str(evidence.id)],
                has_open_conflict=True,
            ),
        ])
        work_document = await db.get(KbDocument, work_registered["document_id"])
        work_revision = await db.get(KbRevision, work_registered["revision_id"])
        work_passage = await db.scalar(
            select(KbPassage).where(KbPassage.revision_id == work_revision.id)
        )
        db.add_all([
            KbEntityAlias(
                entity_id=aurora.id, alias="Polaris", norm_alias=norm_name("Polaris"),
                alias_kind="synonym", source="extracted",
            ),
            KbDocumentEntity(
                passage_id=work_passage.id, entity_id=aurora.id,
                document_id=work_document.id, revision_id=work_revision.id,
            ),
        ])
        relation = KbRelation(
            source_entity_id=aurora.id, target_entity_id=nimbus.id,
            relation_type="manages", origin_scope="personal", visibility="global",
            status="active", evidence_count=1, representative_evidence_id=evidence.id,
        )
        db.add(relation)
        await db.flush()
        db.add(KbRelationEvidence(relation_id=relation.id, evidence_id=evidence.id))
        await db.commit()

        result = await kb_retrieval.search(db, query="오로라", mode="graph")
        personal_result_for_work_alias = await kb_retrieval.search(
            db, query="Polaris", mode="graph"
        )

    assert result["matched_entities"]
    card = result["matched_entities"][0]
    assert card["canonical_name"] == "Aurora"
    assert card["source_document_ids"] == [registered["document_id"]]
    assert card["supporting_evidence_ids"]
    assert card["has_open_conflict"] is False
    assert card["relations"][0]["target_entity"]["canonical_name"] == "Nimbus"
    assert personal_result_for_work_alias["matched_entities"] == []


async def test_failed_dense_query_is_reported_not_silently_relabelled(client, auth, monkeypatch):
    from ttakkari.context import embedder
    async def fail(*args, **kwargs):
        raise RuntimeError("deliberate model failure")
    monkeypatch.setattr(embedder, "embed", fail)
    r = await client.get("/api/assistant/kb/search?q=hello&mode=hybrid", headers=auth)
    assert r.status_code == 503


async def test_oversize_passage_does_not_starve_following_documents(client, auth, monkeypatch):
    # 구조 청커는 긴 구간을 나누므로 한 덩어리 패시지를 직접 만들어 임베딩 상한 격리만 시험한다.
    import hashlib

    from ttakkari import kb_store
    from ttakkari.common.chunking import StructuredChunk

    def one_chunk(body, *args, **kwargs):
        text = body.strip()
        return [StructuredChunk(ord=0, char_start=body.index(text), char_end=body.index(text) + len(text), body=text,
                                sha256=hashlib.sha256(text.encode()).hexdigest())]

    monkeypatch.setattr(kb_store, "chunk_structured_document", one_chunk)
    await register(client, auth, "x" * 50001)
    await register(client, auth, "searchable ordinary passage")
    from ttakkari import kb_retrieval
    assert await kb_retrieval.index_pending(limit=30) == 1
    assert await kb_retrieval.index_pending(limit=30) == 0
    summary = (await client.get("/api/assistant/kb/summary", headers=auth)).json()
    assert summary["retrieval"]["indexed"] == 1
    assert summary["retrieval"]["failed"] == 1


@pytest.mark.parametrize("text", ["a" * 2000, "한글 시험입니다." * 300, "line\n" * 800])
def test_embedding_windows_cover_the_whole_passage(text):
    from ttakkari.kb_retrieval import windows
    chunks = windows(text)
    reconstructed = chunks[0]
    for chunk in chunks[1:]:
        reconstructed += chunk[64:]
    assert reconstructed == text
    assert max(map(len, chunks)) <= 384

pytestmark = pytest.mark.security
