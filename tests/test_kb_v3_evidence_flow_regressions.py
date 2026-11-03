"""Regressions for evidence based KB persistence and Collector HTML structure."""

from __future__ import annotations

from sqlalchemy import func, select

from ttakkari.collector.parse import extract_document
from ttakkari.db import sessionmaker
from ttakkari.kb_library import register_text
from ttakkari.kb_models import KbPassage
from ttakkari.kb_ontology_models import KbDocumentEntity, KbEntity, KbEntityAlias
from ttakkari.kb_pipeline import enqueue_pipeline_job
from ttakkari.kb_pipeline.extractor_step import (
    ExtractionStageResult,
    persist_extracted_knowledge,
)
from ttakkari.kb_pipeline.quote_verifier import (
    CandidateEntity,
    CandidateMention,
    verify_extraction_candidates,
)
from ttakkari.kb_pipeline.stages import DefaultParsingProcessor
from ttakkari.kb_pipeline.types import StageContext
from ttakkari.kb_store import ingest_source, segment_revision


async def test_unanchored_candidate_entity_and_alias_are_not_persisted():
    """A candidate cannot promote its own alias_source label into a global alias."""
    async with sessionmaker()() as db:
        registered = await register_text(
            db,
            title="Evidence regression",
            text="이 본문은 후보 엔터티 이름을 언급하지 않는다.",
            source_key=None,
            lang="ko",
            locator=None,
            scope="personal",
        )
        await db.commit()

    async with sessionmaker()() as db:
        await persist_extracted_knowledge(
            db,
            ExtractionStageResult(
                status="succeeded",
                entities=[CandidateEntity(
                    name="Kubernetes",
                    canonical_name="Kubernetes",
                    aliases=["쿠버네티스"],
                    kind="technology",
                    domain="infra",
                    alias_source="seed",
                )],
            ),
            document_id=registered.document_id,
            revision_id=registered.revision_id,
        )
        await db.commit()

    async with sessionmaker()() as db:
        assert await db.scalar(select(func.count()).select_from(KbEntity).where(
            KbEntity.canonical_name == "Kubernetes"
        )) == 0
        assert await db.scalar(select(func.count()).select_from(KbEntityAlias).where(
            KbEntityAlias.norm_alias == "쿠버네티스"
        )) == 0


def test_collector_html_extraction_preserves_headings_as_markdown_structure():
    raw = b"""<!doctype html><html><body>
    <h1>Guide</h1><p>Introduction with enough words to survive extraction.</p>
    <h2>Networking</h2><p>Service connects pods and exposes the workload.</p>
    <h3>Service policy</h3><p>Keep the exact policy wording here for evidence.</p>
    </body></html>"""
    parsed = extract_document(raw, url="https://example.test/guide", kind="html")

    assert parsed.text.startswith("# Guide")
    assert "## Networking" in parsed.text
    assert "### Service policy" in parsed.text

    compact = extract_document(
        b"<html><body><h1>Outer</h1><h2>Inner</h2><p>One sufficiently long paragraph follows both headings.</p></body></html>",
        url="https://example.test/compact",
        kind="html",
    )
    assert compact.text.index("# Outer") < compact.text.index("## Inner") < compact.text.index("One sufficiently long")


async def test_grounded_bilingual_alias_resolves_and_retry_is_idempotent():
    body = "QuokkaDB (쿼카DB) stores records with deterministic snapshots."
    async with sessionmaker()() as db:
        registered = await register_text(
            db,
            title="Bilingual alias regression",
            text=body,
            source_key=None,
            lang="en",
            locator=None,
            scope="personal",
        )
        passage = (await db.scalars(select(KbPassage).where(
            KbPassage.revision_id == registered.revision_id
        ))).one()
        name = "QuokkaDB (쿼카DB)"
        start = passage.body.index(name)
        candidates = verify_extraction_candidates(
            [passage],
            [],
            [],
            [CandidateMention(
                passage_id=passage.id,
                entity_name=name,
                evidence_quote=name,
                span_start=start,
                span_end=start + len(name),
            )],
        )
        extraction = ExtractionStageResult(
            status="succeeded",
            entities=[CandidateEntity(
                name=name,
                canonical_name=name,
                aliases=["Not present in source"],
                kind="technology",
                domain="infra",
                alias_source="seed",  # untrusted caller label; must not elevate the alias
            )],
            mentions=candidates.verified_mentions,
            evidences=candidates.verified_evidences,
        )
        await db.commit()

    for _ in range(2):
        async with sessionmaker()() as db:
            await persist_extracted_knowledge(
                db,
                extraction,
                document_id=registered.document_id,
                revision_id=registered.revision_id,
            )
            await db.commit()

    async with sessionmaker()() as db:
        entity = (await db.scalars(select(KbEntity).where(
            KbEntity.canonical_name == "QuokkaDB"
        ))).one()
        aliases = list(await db.scalars(select(KbEntityAlias).where(
            KbEntityAlias.entity_id == entity.id
        )))
        assert any(a.alias == "쿼카DB" and a.source == "extracted" for a in aliases)
        assert not any(a.alias == "Not present in source" for a in aliases)
        assert await db.scalar(select(func.count()).select_from(KbDocumentEntity).where(
            KbDocumentEntity.entity_id == entity.id
        )) == 1
        assert entity.mention_count == 1


async def test_legacy_collector_html_without_heading_structure_is_held_for_raw_reprocess():
    async with sessionmaker()() as db:
        registered = await ingest_source(
            db,
            source_key="legacy-html-structure-regression",
            data=b"<html><body><h1>Old title</h1><p>Old normalized paragraph.</p></body></html>",
            kind="web",
            locator="https://example.test/legacy",
            media_type="text/html",
            title="Old title",
            lang="en",
            parser_version="trafilatura-1.0+parse-1",
            normalized_body="Old normalized paragraph.",
        )
        await segment_revision(db, registered.revision_id)
        job = await enqueue_pipeline_job(
            db,
            document_id=registered.document_id,
            revision_id=registered.revision_id,
        )
        await db.commit()
        ctx = StageContext(
            job_id=job.id,
            document_id=registered.document_id,
            revision_id=registered.revision_id,
            stage="parsing",
            attempt_token=job.attempt_token,
            session_factory=sessionmaker(),
        )

    result = await DefaultParsingProcessor()(ctx)
    assert result.status == "held"
    assert result.error_message == "collector_html_structure_revision_required"
    assert result.detail["structure_recoverable_from_revision_body"] is False
    assert result.detail["raw_reprocess_required"] is True
