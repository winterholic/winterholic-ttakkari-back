"""KB v3 실 PostgreSQL 흐름 시연: 등록 -> 워커 claim -> 6단계 -> 지식 행/근거 행, 강제 실패 후 단계 재개.

운영 DB 에는 절대 붙이지 않는다. 이 스크립트는 직접 띄운 임시 pgvector 컨테이너(alembic upgrade head 완료)에만 쓴다.
필수 환경: KB_V3_FLOW_DB_URL=postgresql+asyncpg://...127.0.0.1:<port>/<db>  (DB 이름에 'kbv3' 포함, 호스트는 loopback)
사용: KB_V3_FLOW_DB_URL=... uv run python scripts/kb_v3_real_db_flow.py DOC_A.md DOC_B.md
 - DOC_A: 정상 흐름(6단계 전부 진행)
 - DOC_B: extracting 처리기를 강제로 실패시킨 뒤 retry_pipeline_job(from_stage='extracting')로 재개
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

URL = os.environ.get("KB_V3_FLOW_DB_URL", "")
parsed = make_url(URL) if URL else None
if parsed is None or "kbv3" not in (parsed.database or "") or parsed.host not in ("127.0.0.1", "localhost"):
    sys.exit("KB_V3_FLOW_DB_URL 이 임시 컨테이너(loopback, DB 이름에 kbv3)를 가리키지 않아 중단한다")

HOME = tempfile.mkdtemp(prefix="kbv3-flow-home-")
os.environ.update(
    {
        "TTAKKARI_NO_ENV_FILE": "1",
        "TTAKKARI_ENV": "dev",
        "TTAKKARI_DATABASE_URL": URL,
        "TTAKKARI_HOME": HOME,
        "TTAKKARI_CONTEXT_EMBEDDER": "hash",  # 결정적 해시 임베딩(실제 pgvector 행을 만든다). 모델 서버는 쓰지 않는다
        "TTAKKARI_KB_SEMANTIC_ENABLED": "true",
        "TTAKKARI_COLLECTOR_CONTROL_ENABLED": "false",
    }
)

from sqlalchemy import func, select, text  # noqa: E402

from ttakkari.db import sessionmaker  # noqa: E402
from ttakkari.kb_library import register_text  # noqa: E402
from ttakkari.kb_models import KbEvidence, KbPassage  # noqa: E402
from ttakkari.kb_ontology_models import (  # noqa: E402
    KbAssertion,
    KbAssertionEvidence,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_pipeline import (  # noqa: E402
    PIPELINE_STAGES,
    STAGE_EXTRACTING,
    KbPipelineOrchestrator,
    KbPipelineWorker,
    StageProcessorRegistry,
    get_default_worker,
    retry_pipeline_job,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage  # noqa: E402
from ttakkari.kb_vector_models import KbPassageVector  # noqa: E402


def say(msg: str = "") -> None:
    print(msg, flush=True)


async def count(db, model, *where) -> int:
    return await db.scalar(select(func.count()).select_from(model).where(*where)) or 0


async def show_job(job_id, label: str) -> None:
    async with sessionmaker()() as db:
        job = await db.get(KbPipelineJob, job_id)
        stages = {s.stage: s for s in await db.scalars(select(KbPipelineStage).where(KbPipelineStage.job_id == job_id))}
        say(f"[{label}] job={job_id} status={job.status} current_stage={job.current_stage} attempts={job.attempt_count}/{job.max_attempts}"
            f" hold_reason={job.hold_reason!r} error_code={job.error_code!r}")
        for name in PIPELINE_STAGES:
            s = stages.get(name)
            if s is None:
                say(f"    {name:15s} (no row)")
                continue
            det = s.detail or {}
            brief = {k: det[k] for k in ("passage_count", "structured_passages", "vectors_total", "entities_linked",
                                          "resolution_steps", "missing_capability", "reason", "blocked_by") if k in det}
            if "l2" in det:
                brief["l2"] = {k: det["l2"][k] for k in ("adapter", "available", "reason", "calls_made")}
            if "persisted" in det:
                p = det["persisted"]
                brief["persisted"] = {k: p[k] for k in ("entities_created", "assertions_created", "relations_created",
                                                       "relation_evidence_created", "evidences_created", "mentions_created")}
            say(f"    {name:15s} {s.status:10s} attempts={s.attempt_count} {s.duration_ms}ms {brief} err={s.error_message!r}")


async def knowledge_counts(label: str, revision_id) -> None:
    async with sessionmaker()() as db:
        passages = [p.id for p in await db.scalars(select(KbPassage).where(KbPassage.revision_id == revision_id))]
        ev_ids = select(KbEvidence.id).where(KbEvidence.passage_id.in_(passages))
        say(f"[{label}] passages={len(passages)} vectors={await count(db, KbPassageVector, KbPassageVector.passage_id.in_(passages))}"
            f" entities(total)={await count(db, KbEntity)} aliases={await count(db, KbEntityAlias)}"
            f" doc_entity_links(rev)={await count(db, KbDocumentEntity, KbDocumentEntity.revision_id == revision_id)}")
        say(f"    assertions(rev)={await count(db, KbAssertion, KbAssertion.revision_id == revision_id)}"
            f" assertion_evidence={await count(db, KbAssertionEvidence, KbAssertionEvidence.evidence_id.in_(ev_ids))}"
            f" evidence(rev)={await count(db, KbEvidence, KbEvidence.passage_id.in_(passages))}")
        rel_ids = select(KbRelationEvidence.relation_id).where(KbRelationEvidence.evidence_id.in_(ev_ids))
        say(f"    relations(rev)={await count(db, KbRelation, KbRelation.id.in_(rel_ids))}"
            f" relation_evidence(rev)={await count(db, KbRelationEvidence, KbRelationEvidence.evidence_id.in_(ev_ids))}"
            f" relations_active={await count(db, KbRelation, KbRelation.status == 'active')}"
            f" relations_candidate_without_evidence={await count(db, KbRelation, KbRelation.status == 'candidate', KbRelation.evidence_count < 1)}")
        rows = (await db.execute(text(
            "select r.relation_type, r.status, r.evidence_count, r.visibility, a.canonical_name, b.canonical_name "
            "from kb_relations r join kb_entities a on a.id=r.source_entity_id join kb_entities b on b.id=r.target_entity_id "
            "order by r.created_at limit 5"))).all()
        for row in rows:
            say(f"    sample relation: {row}")


def make_worker(registry: StageProcessorRegistry | None = None) -> KbPipelineWorker:
    sf = sessionmaker()
    return KbPipelineWorker(
        session_factory=sf,
        orchestrator=KbPipelineOrchestrator(session_factory=sf, registry=registry, base_backoff_seconds=0.0),
        worker_id="kbv3-flow-demo",
    )


async def drain(worker: KbPipelineWorker) -> int:
    total = 0
    while True:
        n = await worker.pump()
        if worker._tasks:
            await asyncio.gather(*list(worker._tasks))
        total += n
        if n == 0:
            return total


async def register(path: Path, scope: str = "personal"):
    async with sessionmaker()() as db:
        res = await register_text(db, title=path.stem, text=path.read_text(encoding="utf-8"), source_key=None,
                                  lang="ko", locator=str(path), scope=scope)
        await db.commit()
        job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.revision_id == res.revision_id))
        return res, job.id


class ForcedExtractingFailure:
    async def __call__(self, ctx):
        raise RuntimeError("forced extracting failure (demo)")


async def main(doc_a: Path, doc_b: Path) -> None:
    get_default_worker().pause_claiming()  # 이 스크립트의 워커만 claim 한다
    say(f"db={parsed.host}:{parsed.port}/{parsed.database}  home={HOME}")

    say("\n=== A. 정상 흐름: 등록 -> 워커 claim -> 6단계 ===")
    res_a, job_a = await register(doc_a)
    say(f"registered document={res_a.document_id} revision={res_a.revision_id} passages_at_register={res_a.passage_count}")
    await show_job(job_a, "A before")
    say(f"worker pumped jobs: {await drain(make_worker())}")
    await show_job(job_a, "A after")
    await knowledge_counts("A knowledge", res_a.revision_id)

    say("\n=== B. extracting 강제 실패 -> retry_pipeline_job(from_stage='extracting') 재개 ===")
    res_b, job_b = await register(doc_b)
    async with sessionmaker()() as db:
        job = await db.get(KbPipelineJob, job_b)
        job.max_attempts = 1  # 첫 실패를 곧 최종 실패로
        await db.commit()
    failing = StageProcessorRegistry()
    failing.register(STAGE_EXTRACTING, ForcedExtractingFailure())
    say(f"worker pumped jobs (extracting forced to fail): {await drain(make_worker(failing))}")
    await show_job(job_b, "B after forced failure")
    await knowledge_counts("B knowledge (extraction rows absent, passages/vectors present)", res_b.revision_id)

    outcome = await retry_pipeline_job(sessionmaker(), job_b, from_stage="extracting")
    say(f"retry_pipeline_job -> requeued={outcome.requeued} from_stage={outcome.from_stage} reset={outcome.reset_stages}")
    again = await retry_pipeline_job(sessionmaker(), job_b, from_stage="extracting")
    say(f"retry_pipeline_job (두 번째 호출, 멱등) -> requeued={again.requeued} reason={again.reason}")
    await show_job(job_b, "B after retry request")
    say(f"worker pumped jobs (real processors): {await drain(make_worker())}")
    await show_job(job_b, "B after resume")
    await knowledge_counts("B knowledge after resume", res_b.revision_id)

    async with sessionmaker()() as db:
        trig = (await db.execute(text(
            "select count(*) from pg_trigger where tgname in ('trg_kb_relation_evidence_sync')"))).scalar_one()
        say(f"\nDB trigger present (relation evidence sync): {bool(trig)}")
        say("job summary: " + str((await db.execute(text("select status, count(*) from kb_pipeline_jobs group by status"))).all()))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2])))
