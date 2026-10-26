"""Minimal pinned-source probes for the alias bypass and Collector HTML structure blockers.

The same file is run unchanged at the pinned integration SHA and on the corrected branch.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select
from test_collector_integration import BASE, make_source, runner
from test_collector_pipeline import env  # noqa: F401 -- isolated HTTP/PG fixture

from ttakkari import kb_delivery
from ttakkari.collector.control_models import KbObservation
from ttakkari.db import sessionmaker
from ttakkari.kb_library import register_text
from ttakkari.kb_models import KbPassage, KbRevision, KbSource
from ttakkari.kb_ontology_models import KbEntity, KbEntityAlias
from ttakkari.kb_pipeline import get_default_worker
from ttakkari.kb_pipeline.extractor_step import (
    ExtractionStageResult,
    persist_extracted_knowledge,
)
from ttakkari.kb_pipeline.quote_verifier import CandidateEntity
from ttakkari.kb_pipeline.stages import DefaultParsingProcessor
from ttakkari.kb_pipeline.types import StageContext
from ttakkari.kb_pipeline_models import KbPipelineJob


async def test_unanchored_alias_candidate_does_not_pollute_global_identity():
    async with sessionmaker()() as db:
        registered = await register_text(
            db,
            title="Pinned alias bypass probe",
            text="This body does not mention the candidate entity or its proposed alias.",
            source_key=None,
            lang="en",
            locator=None,
            scope="personal",
        )
        await db.commit()

    async with sessionmaker()() as db:
        await persist_extracted_knowledge(
            db,
            ExtractionStageResult(
                status="succeeded",
                entities=[
                    CandidateEntity(
                        name="Kubernetes",
                        canonical_name="Kubernetes",
                        aliases=["쿠버네티스"],
                        kind="technology",
                        domain="infra",
                        alias_source="seed",
                    )
                ],
            ),
            document_id=registered.document_id,
            revision_id=registered.revision_id,
        )
        await db.commit()

    async with sessionmaker()() as db:
        entity_count = await db.scalar(
            select(func.count()).select_from(KbEntity).where(
                KbEntity.canonical_name == "Kubernetes"
            )
        )
        alias_count = await db.scalar(
            select(func.count()).select_from(KbEntityAlias).where(
                KbEntityAlias.norm_alias == "쿠버네티스"
            )
        )
    assert (entity_count, alias_count) == (0, 0)


async def test_real_collector_html_delivery_preserves_headings_and_source_ledger(
    client, auth, env, monkeypatch  # noqa: F811
):
    """Run local HTTP fetch -> Collector envelope -> delivery -> revision -> passage -> parsing."""
    monkeypatch.setattr(kb_delivery, "default_blob_store", lambda: env.blobs)
    html = """<!doctype html><html><head><title>Structured guide</title></head><body>
    <article><h1>Structured guide</h1>
    <p>The introduction describes the system and preserves a long, exact sentence for evidence.</p>
    <h2>Network design</h2>
    <p>Service endpoints connect workloads across the cluster with explicit policy controls.</p>
    <h3>Traffic policy</h3>
    <p>Traffic policy keeps ingress routes and access restrictions together.</p></article></body></html>"""
    raw = html.encode()
    env.server.page("/pinned-structured", raw)
    source = await make_source(
        client,
        auth,
        env,
        [env.server.url("/pinned-structured")],
        key="pinned-html-heading-probe",
    )
    await client.post(BASE + f"/sources/{source['id']}/run", headers=auth)
    assert await runner.consume_once(limit=1, managed_only=True) == 1

    worker = get_default_worker()
    was_paused = worker.claiming_paused
    worker.pause_claiming()
    try:
        assert await kb_delivery.deliver_once() == 1
        async with sessionmaker()() as db:
            revision = (await db.scalars(select(KbRevision))).one()
            source_row = await db.get(KbSource, revision.source_id)
            passages = list(
                await db.scalars(
                    select(KbPassage)
                    .where(KbPassage.revision_id == revision.id)
                    .order_by(KbPassage.ord)
                )
            )
            job = (
                await db.scalars(
                    select(KbPipelineJob).where(KbPipelineJob.revision_id == revision.id)
                )
            ).one()
            observation = (
                await db.scalars(
                    select(KbObservation).where(KbObservation.revision_id == revision.id)
                )
            ).one()
            assert source_row.media_type == "text/html"
            assert source_row.sha256 == hashlib.sha256(raw).hexdigest()
            assert source_row.origin_meta["raw_handle"]
            assert observation.event_time == source_row.collected_at
            assert passages
            assert all(revision.body[p.char_start : p.char_end] == p.body for p in passages)
            assert all(p.sha256 == hashlib.sha256(p.body.encode()).hexdigest() for p in passages)
            assert len({(p.char_start, p.char_end, p.sha256) for p in passages}) == len(passages)
            assert "# Structured guide" in revision.body
            assert "## Network design" in revision.body
            assert "### Traffic policy" in revision.body
            assert source_row.origin_meta["normalized_sha256"] == revision.text_sha256
            assert source_row.origin_meta["heading_structure"] == "dom-headings-to-markdown-v1"
            ctx = StageContext(
                job_id=job.id,
                document_id=revision.document_id,
                revision_id=revision.id,
                stage="parsing",
                attempt_token=job.attempt_token,
                session_factory=sessionmaker(),
            )
        parsed = await DefaultParsingProcessor()(ctx)
        assert parsed.status == "succeeded", parsed
        assert parsed.detail["heading_spans"] >= 3
    finally:
        if not was_paused:
            worker.resume_claiming()
