"""KB v3 실 PostgreSQL 흐름 시연: 등록 -> 워커 claim -> 6단계 -> 지식 행/근거 행, 강제 실패 후 단계 재개.

운영 DB 에는 절대 붙이지 않는다. 이 스크립트는 직접 띄운 임시 pgvector 컨테이너(alembic upgrade head 완료)에만 쓴다.
필수 환경: KB_V3_FLOW_DB_URL=postgresql+asyncpg://...127.0.0.1:<port>/<db>  (DB 이름에 'kbv3' 포함, 호스트는 loopback)
사용: KB_V3_FLOW_DB_URL=... uv run python scripts/kb_v3_real_db_flow.py DOC_A.md DOC_B.md [DOC_C.html [CONFLICT_1.md CONFLICT_2.md [L2_1.md L2_2.md]]]
 - DOC_A: 정상 흐름(6단계 전부 진행)
 - DOC_B: extracting 처리기를 강제로 실패시킨 뒤 retry_pipeline_job(from_stage='extracting')로 재개
 - DOC_C(선택): HTML. Collector 파서(extract_document + 제목 구조 보존)로 정규화한 본문을 kb_delivery.deliver_item 과 같은 순서로 ingest 한다.
   HTTP fetch·outbox 는 거치지 않는다(그 전체 경로는 tests/test_collector_integration.py 가 격리 PG 에서 검증).
 - CONFLICT_*(선택): 같은 엔터티·술어에 상반된 정의를 담은 문서들. canonicalizing 이 충돌 행을 만드는지 본다.
 - L2_1/L2_2(선택): 가짜 L2 어댑터로 동시 처리할 두 문서(일일 예산 1). 실제 구독 CLI·모델 서버는 부르지 않는다.
 모든 문서 처리 뒤에 정본·충돌·근거 없는 alias·heading_spans 를 세고, 검색 함수(kb_retrieval.search)로 어휘·alias·1-hop 결과를 확인한다.
 임베딩은 결정적 해시(TTAKKARI_CONTEXT_EMBEDDER=hash)다: 벡터 경로는 의미 품질 증거가 아니다.
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

from sqlalchemy import func, select, text

from ttakkari.collector import parse as collector_parse
from ttakkari.collector import textproc as collector_textproc
from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.db import sessionmaker
from ttakkari.kb_access import (
    create_owner_context,
    create_personal_agent_context,
)
from ttakkari.kb_library import register_text
from ttakkari.kb_models import KbEvidence, KbPassage
from ttakkari.kb_ontology.conflicts import list_conflicts
from ttakkari.kb_ontology.retrieval import (
    expand_1hop_relations,
    find_matched_entities,
)
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbEntityCanonical,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_pipeline import (
    PIPELINE_STAGES,
    STAGE_EXTRACTING,
    KbPipelineOrchestrator,
    KbPipelineWorker,
    StageProcessorRegistry,
    get_default_worker,
    retry_pipeline_job,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.kb_retrieval import search
from ttakkari.kb_store import ingest_source, segment_revision
from ttakkari.kb_vector_models import KbPassageVector


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


async def register_html(path: Path):
    """Collector 파서 경로로 HTML 을 정규화해 ingest 한다(kb_delivery.deliver_item 과 같은 ingest/access/segment/enqueue 순서)."""
    raw = path.read_bytes()
    extracted = collector_parse.extract_document(raw, url=f"https://docs.example/{path.name}", kind="html")
    norm = collector_textproc.normalize_text(extracted.text)
    async with sessionmaker()() as db:
        result = await ingest_source(
            db, source_key=f"collector:flow:{path.name}", data=raw, kind="web", locator=f"https://docs.example/{path.name}",
            media_type="text/html", title=extracted.title, lang="en",
            parser_version=collector_parse.PARSER_VERSION[:60], normalized_body=norm.text,
            origin_meta={"via": "collector", "heading_structure": extracted.meta.get("heading_structure")},
        )
        if await db.get(KbDocumentAccess, result.document_id) is None:
            db.add(KbDocumentAccess(document_id=result.document_id, tenant_id="owner", scope="personal",
                                    origin_scope="personal", visibility="global"))
        await segment_revision(db, result.revision_id)
        from ttakkari.kb_pipeline import enqueue_pipeline_job

        await enqueue_pipeline_job(db, document_id=result.document_id, revision_id=result.revision_id)
        await db.commit()
        job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.revision_id == result.revision_id))
        say(f"html heading_structure={extracted.meta.get('heading_structure')} body_has_h1={'# ' in norm.text}")
        return result, job.id


async def final_report(html_job_id) -> None:
    say("\n=== 최종 행 수 / 불변식 ===")
    async with sessionmaker()() as db:
        for label, model in (("entities", KbEntity), ("aliases", KbEntityAlias), ("document_entities", KbDocumentEntity),
                             ("assertions", KbAssertion), ("assertion_evidence", KbAssertionEvidence),
                             ("relations", KbRelation), ("relation_evidence", KbRelationEvidence),
                             ("evidence", KbEvidence), ("canonicals", KbEntityCanonical), ("conflicts", KbConflict),
                             ("passages", KbPassage), ("vectors", KbPassageVector)):
            say(f"  {label:20s} {await count(db, model)}")
        # 근거 없는 alias: 추출 출처(extracted)인데 이 엔터티의 어떤 패시지 본문에도 나타나지 않는 alias
        unsupported = (await db.execute(text(
            "select a.alias, a.source from kb_entity_aliases a where a.source = 'extracted' and not exists ("
            " select 1 from kb_document_entities de join kb_passages p on p.id = de.passage_id"
            " where de.entity_id = a.entity_id and strpos(lower(p.body), lower(a.alias)) > 0)"))).all()
        say(f"  extracted alias without passage evidence: {len(unsupported)} {unsupported[:3]}")
        by_source = (await db.execute(text("select source, count(*) from kb_entity_aliases group by source order by 1"))).all()
        say(f"  alias by source: {by_source}")
        say("  canonical rows: " + str((await db.execute(text(
            "select e.canonical_name, c.version_no, jsonb_array_length(c.key_facts) from kb_entity_canonicals c "
            "join kb_entities e on e.id=c.entity_id order by 1 limit 6"))).all()))
        say("  conflict rows: " + str((await db.execute(text(
            "select conflict_kind, status, visibility from kb_conflicts limit 6"))).all()))
        if html_job_id is not None:
            stage = await db.scalar(select(KbPipelineStage).where(KbPipelineStage.job_id == html_job_id,
                                                                   KbPipelineStage.stage == "parsing"))
            say(f"  HTML parsing detail: heading_spans={(stage.detail or {}).get('heading_spans')} "
                f"source_media_type={(stage.detail or {}).get('source_media_type')} status={stage.status}")
        stages = (await db.execute(text("select stage, status, count(*) from kb_pipeline_stages group by 1,2 order by 1,2"))).all()
        say(f"  stage status histogram: {stages}")


async def search_report() -> None:
    say("\n=== 검색 API 함수(kb_retrieval.search, 소유자 컨텍스트) ===")
    owner = create_owner_context()
    async with sessionmaker()() as db:
        for label, query in (("lexical(en)", "rollout undo Deployment ReplicaSets"), ("lexical(ko)", "롤링 업데이트 진행 확인"),
                             ("alias(ko->en)", "쿠버네티스"), ("alias(ko->en,문장)", "쿠버네티스 네트워킹"),
                             ("alias(abbrev)", "k8s")):
            res = await search(db, query=query, access_context=owner, limit=5, mode="hybrid")
            docs = [(i["document"]["title"], i["score"]) for i in res["items"]]
            ents = [(c.get("canonical_name"), c.get("matched_by"), c.get("matched_alias")) for c in res["matched_entities"]]
            say(f"  [{label}] q={query!r} items={len(docs)} top={docs[:3]} coverage={res['coverage']} cards={ents[:3]}")
        lexical_only = await search(db, query="쿠버네티스", access_context=owner, limit=5, mode="lexical")
        say(f"  [lexical 경로만] q='쿠버네티스' items={len(lexical_only['items'])} "
            f"titles={[i['document']['title'] for i in lexical_only['items']]}")
        graph = await search(db, query="쿠버네티스", access_context=owner, limit=10, mode="graph")
        say(f"  [graph 경로만] q='쿠버네티스' items={len(graph['items'])} titles={[i['document']['title'] for i in graph['items']]}")
        matched = await find_matched_entities(db, "쿠버네티스", ["쿠버네티스"], owner)
        say(f"  matched entities for 쿠버네티스: {[(m['canonical_name'], m['matched_by'], m['matched_alias']) for m in matched]}")
        if matched:
            rels = await expand_1hop_relations(db, [m["entity_id"] for m in matched], owner, max_per_entity=5)
            say(f"  1-hop relations: {len(rels)} sample={rels[:2]}")
        personal = await search(db, query="쿠버네티스", access_context=create_personal_agent_context(), limit=5)
        say(f"  [personal agent ctx] items={len(personal['items'])}")

        say("\n=== 충돌·정본 노출 ===")
        conf = await search(db, query="Kubernetes refers to", access_context=owner, limit=10, mode="graph")
        say(f"  graph 경로 'Kubernetes refers to': items={[(i['document']['title'], i['has_conflict'], [a['status'] for a in i['assertions']]) for i in conf['items']]}")
        say(f"  matched card has_open_conflict={[(c.get('canonical_name'), c.get('has_open_conflict')) for c in conf['matched_entities']]}")
        open_conflicts = await list_conflicts(db, access=owner)
        say(f"  list_conflicts(owner ctx)={[(c.conflict_kind, c.status, c.visibility) for c in open_conflicts]}")
        pers = await list_conflicts(db, access=create_personal_agent_context())
        say(f"  list_conflicts(personal ctx)={len(pers)}  (global 문서끼리 충돌이라 보여야 한다)")

        say("\n=== 1-hop 관계 ===")
        hop_q = "Service types"
        cand = (await db.execute(text(
            "select r.id, a.canonical_name, b.canonical_name, r.status, r.evidence_count from kb_relations r "
            "join kb_entities a on a.id=r.source_entity_id join kb_entities b on b.id=r.target_entity_id "
            "where a.canonical_name = 'Service types' or b.canonical_name = 'Service types'"))).all()
        say(f"  relations touching 'Service types' (추출 직후): {[tuple(map(str, row[1:])) for row in cand]}")
        matched = await find_matched_entities(db, hop_q, ["service", "types"], owner)
        rels = await expand_1hop_relations(db, [m["entity_id"] for m in matched], owner, max_per_entity=5)
        say(f"  1-hop(candidate 상태): matched={[m['canonical_name'] for m in matched]} relations={len(rels)}  "
            "<- 추출 파이프라인은 candidate 로만 저장하고 active 승격 경로가 없다")
        if cand:
            # 검토 승인(승격)을 모사: 이미 근거 행(evidence_count>=1)이 있는 관계를 active 로 바꾼다. 파이프라인이 하는 일이 아니다.
            await db.execute(text("update kb_relations set status='active' where id = :rid"), {"rid": cand[0][0]})
            await db.commit()
            rels = await expand_1hop_relations(db, [m["entity_id"] for m in matched], owner, max_per_entity=5)
            res = await search(db, query=hop_q, access_context=owner, limit=10, mode="graph")
            say(f"  1-hop(승격 모사 후): relations={len(rels)} sample={[(r['relation_type'], r['source_entity']['canonical_name'], r['target_entity']['canonical_name']) for r in rels][:2]}")
            say(f"  graph 경로 검색 결과 passages={len(res['items'])} titles={[i['document']['title'] for i in res['items']]} "
                f"cards={[(c.get('canonical_name'), len(c.get('relations') or [])) for c in res['matched_entities']]}")
            personal_hop = await search(db, query=hop_q, access_context=create_personal_agent_context(), limit=10, mode="graph")
            say(f"  personal ctx graph passages={len(personal_hop['items'])}")


class FakeL2:
    """L2 가짜 어댑터. 구독 CLI·모델 서버를 부르지 않는다(실제 L2 호출 없음). 근거 인용이 본문에 있는 항목만 검증기를 통과한다."""

    name = "fake_l2"

    def __init__(self) -> None:
        self.calls = 0

    async def availability(self):
        return True, None

    async def __call__(self, prompt: str, schema: dict):
        self.calls += 1
        quote = "Ingress policy keeps routes together."
        if quote not in prompt:
            return {"entities": [], "assertions": [], "relations": []}
        return {
            "entities": [{"name": "Ingress policy", "aliases": [], "kind": "concept", "domain": "infra"}],
            "assertions": [
                {"subject": "Ingress policy", "predicate": "keeps", "object_entity": None, "object_value": "routes together",
                 "modality": "fact", "polarity": "positive", "applies_version": None, "evidence_quote": quote},
                {"subject": "Ingress policy", "predicate": "hallucinated", "object_entity": None, "object_value": "none",
                 "modality": "fact", "polarity": "positive", "applies_version": None, "evidence_quote": "This sentence is not in the body."},
            ],
            "relations": [],
        }


async def l2_phase(path_a: Path, path_b: Path) -> None:
    """가짜 L2 로 두 문서를 동시에 처리한다. 일일 예산 1 이면 호출은 전체에서 1번뿐이어야 한다(원장으로 직렬화)."""
    from ttakkari.config import get_settings
    from ttakkari.kb_pipeline import ExtractingProcessor

    settings = get_settings()
    settings.kb_l2_daily_budget, settings.kb_l2_max_calls_per_doc = 1, 8
    fake = FakeL2()
    reg = StageProcessorRegistry()
    reg.register(STAGE_EXTRACTING, ExtractingProcessor(adapter_factory=lambda: fake))
    jobs = [(await register(p))[1] for p in (path_a, path_b)]
    # 두 워커가 동시에 같은 예산을 다툰다
    pumped = await asyncio.gather(drain(make_worker(reg)), drain(make_worker(reg)))
    say(f"concurrent workers pumped={pumped} fake_l2.calls={fake.calls} (budget=1)")
    for jid in jobs:
        async with sessionmaker()() as db:
            st = await db.scalar(select(KbPipelineStage).where(KbPipelineStage.job_id == jid, KbPipelineStage.stage == STAGE_EXTRACTING))
            l2 = (st.detail or {}).get("l2", {})
            say(f"  job {jid} extracting={st.status} l2={ {k: l2.get(k) for k in ('adapter', 'reason', 'calls_made')} } "
                f"rejected_assertions={(st.detail or {}).get('rejected_assertions')}")
    async with sessionmaker()() as db:
        res = (await db.execute(text("select status, count(*), sum(reserved_calls) from kb_l2_budget_reservations group by 1"))).all()
        use = (await db.execute(text("select status, count(*) from kb_l2_budget_usage group by 1"))).all()
        tier = (await db.execute(text("select extractor_tier, count(*) from kb_assertions group by 1 order by 1"))).all()
        say(f"  ledger reservations(status,count,reserved)={res} usage(status,count)={use} assertions by tier={tier}")


class ForcedExtractingFailure:
    async def __call__(self, ctx):
        raise RuntimeError("forced extracting failure (demo)")


async def main(doc_a: Path, doc_b: Path, doc_c: Path | None, doc_conflicts: list[Path], l2_docs: list[Path]) -> None:
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

    html_job = None
    if doc_c is not None:
        say("\n=== C. HTML(Collector 파서 경로) 문서 ===")
        res_c, html_job = await register_html(doc_c)
        say(f"worker pumped jobs: {await drain(make_worker())}")
        await show_job(html_job, "C after")
        await knowledge_counts("C knowledge", res_c.revision_id)

    say("\n=== D. 같은 엔터티·술어에 상반된 정의를 가진 두 문서(충돌·정본 행 생성) ===")
    for path in doc_conflicts:
        res_d, _job_d = await register(path)
        say(f"registered {path.name} document={res_d.document_id}")
    say(f"worker pumped jobs: {await drain(make_worker())}")

    if len(l2_docs) == 2:
        say("\n=== E. 가짜 L2 어댑터 + 일일 예산 1 + 동시 워커 2개(실제 구독 CLI 호출 없음) ===")
        await l2_phase(*l2_docs)

    await final_report(html_job)
    await search_report()

    async with sessionmaker()() as db:
        trig = (await db.execute(text(
            "select count(*) from pg_trigger where tgname in ('trg_kb_relation_evidence_sync')"))).scalar_one()
        say(f"\nDB trigger present (relation evidence sync): {bool(trig)}")
        say("job summary: " + str((await db.execute(text("select status, count(*) from kb_pipeline_jobs group by status"))).all()))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]) if len(sys.argv) > 3 else None,
                     [Path(a) for a in sys.argv[4:6]], [Path(a) for a in sys.argv[6:8]]))
