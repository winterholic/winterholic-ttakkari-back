"""KB v3 통합 흐름(실 PostgreSQL): 등록 -> 워커 claim -> 6단계 -> 지식 행/근거 행, 재개, 가시성, L2 어댑터.

이 파일은 conftest 가 띄우는 임시 PostgreSQL 클러스터(또는 지정된 격리 DB)에서 돈다. 파서·구조 청커·추출기·해소기·임베딩·
스키마 트리거(관계 근거 개수 동기화, 원장 불변)를 모두 실제로 지난다. 모델 서버는 부르지 않는다(가짜 L2 어댑터 주입).
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

import pytest
from sqlalchemy import func, select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.kb_library import register_text
from ttakkari.kb_models import KbEvidence, KbPassage
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbDocumentEntity,
    KbEntity,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_pipeline import (
    STAGE_CANONICALIZING,
    STAGE_CHUNKING,
    STAGE_EXTRACTING,
    STAGE_INDEXING,
    STAGE_LINKING,
    STAGE_PARSING,
    ExtractingProcessor,
    KbPipelineOrchestrator,
    KbPipelineWorker,
    LinkingProcessor,
    StageProcessorRegistry,
    StageResult,
    retry_pipeline_job,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.kb_store import originals_root

DOC_TEXT = """# 쿠버네티스 운영 가이드

쿠버네티스(Kubernetes)는 컨테이너화된 애플리케이션의 배포와 확장을 관리하는 오픈소스 플랫폼이다.

## 워크로드 리소스

디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다.

### 롤링 업데이트

버전 v1.31 이상에서는 `rolling_update_strategy` 설정이 권장된다. `kubectl rollout status` 로 진행을 확인한다.

## 네트워크

서비스는 Pod 집합을 노출한다. 외부 노출은 반드시 서비스로 한다. Docker 이미지와 Helm 차트로 배포한다.
"""

WORK_TEXT = DOC_TEXT.replace("운영 가이드", "사내 운영 가이드(비공개)")


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    root = originals_root()
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    s = get_settings()
    monkeypatch.setattr(s, "kb_semantic_enabled", True)  # 해시 임베딩 백엔드(테스트 conftest) 로 실제 pgvector 행을 만든다
    monkeypatch.setattr(s, "kb_l2_adapter", "disabled")
    yield
    if root.exists():
        shutil.rmtree(root)


async def _register(text: str, scope: str = "personal"):
    async with sessionmaker()() as db:
        res = await register_text(
            db, title="운영 가이드", text=text, source_key=None, lang="ko", locator=None, scope=scope
        )
        await db.commit()
        job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.revision_id == res.revision_id))
        return res, job.id


def _worker(registry: StageProcessorRegistry | None = None) -> KbPipelineWorker:
    sf = sessionmaker()
    return KbPipelineWorker(
        session_factory=sf,
        orchestrator=KbPipelineOrchestrator(session_factory=sf, registry=registry, base_backoff_seconds=0.0),
    )


async def _drain(worker: KbPipelineWorker, rounds: int = 6) -> int:
    pumped = 0
    for _ in range(rounds):
        n = await worker.pump()
        if worker._tasks:
            await asyncio.gather(*list(worker._tasks))
        pumped += n
        if n == 0:
            break
    return pumped


async def _stages(job_id) -> dict[str, KbPipelineStage]:
    async with sessionmaker()() as db:
        rows = await db.scalars(select(KbPipelineStage).where(KbPipelineStage.job_id == job_id))
        return {r.stage: r for r in rows}


async def _job(job_id) -> KbPipelineJob:
    async with sessionmaker()() as db:
        return await db.get(KbPipelineJob, job_id)


async def _count(model, *where) -> int:
    async with sessionmaker()() as db:
        return await db.scalar(select(func.count()).select_from(model).where(*where)) or 0


class FakeL2:
    name = "fake_l2"

    def __init__(self, reachable: bool = True) -> None:
        self.reachable = reachable
        self.calls = 0

    async def availability(self):
        return (True, None) if self.reachable else (False, "fake_unreachable")

    async def __call__(self, prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        quote = "서비스는 Pod 집합을 노출한다."
        if quote not in prompt:
            return {"entities": [], "assertions": [], "relations": []}
        return {
            "entities": [{"name": "서비스", "aliases": [], "kind": "concept", "domain": "infra"}],
            "assertions": [
                {
                    "subject": "서비스", "predicate": "exposes", "object_entity": None, "object_value": "Pod 집합",
                    "modality": "fact", "polarity": "positive", "applies_version": None, "evidence_quote": quote,
                },
                {  # 환각 인용: 검증기가 버려야 한다
                    "subject": "서비스", "predicate": "hallucinated", "object_entity": None, "object_value": "없음",
                    "modality": "fact", "polarity": "positive", "applies_version": None,
                    "evidence_quote": "이 문장은 본문에 없다.",
                },
            ],
            "relations": [
                {"source": "서비스", "target": "Pod", "relation_type": "uses", "evidence_quote": quote},
                {"source": "서비스", "target": "Docker", "relation_type": "uses", "evidence_quote": "본문에 없는 근거"},
            ],
        }


async def test_register_then_worker_runs_all_six_stages_with_real_processors():
    res, job_id = await _register(DOC_TEXT)
    assert await _drain(_worker()) == 1

    job = await _job(job_id)
    st = await _stages(job_id)
    assert {k: v.status for k, v in st.items()} == {
        STAGE_PARSING: "succeeded",
        STAGE_CHUNKING: "succeeded",
        STAGE_EXTRACTING: "degraded",  # L2 기본 꺼짐: 사유를 남기고 L0/L1 결과는 저장
        STAGE_LINKING: "succeeded",
        STAGE_INDEXING: "succeeded",
        STAGE_CANONICALIZING: "held",  # 구현 없음: 성공으로 위장하지 않는다
    }
    assert st[STAGE_EXTRACTING].detail["l2"] == {
        "adapter": "disabled", "available": False, "reason": "l2_disabled", "calls_made": 0,
        "daily_budget_remaining_before": get_settings().kb_l2_daily_budget,
    }
    assert st[STAGE_CANONICALIZING].detail["missing_capability"] == STAGE_CANONICALIZING
    assert job.status == "degraded" and job.status != "ready"
    assert job.metrics["stage_statuses"][STAGE_INDEXING] == "succeeded"

    # 구조 청킹이 실제로 section_path 를 만들었고, 임베딩 행이 실제로 pgvector 에 들어갔다
    async with sessionmaker()() as db:
        passages = list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == res.revision_id)))
        assert any(p.section_path for p in passages)
    assert st[STAGE_INDEXING].detail["vectors_total"] == len(passages) >= 1

    # 추출 결과: 엔터티·언급·주장·관계 후보와 근거 행
    ent_n = await _count(KbEntity)
    assert ent_n > 0
    assert await _count(KbDocumentEntity) > 0
    assert await _count(KbAssertion) >= 2
    rel_n = await _count(KbRelation)
    assert rel_n > 0
    assert await _count(KbRelationEvidence) >= rel_n  # 관계마다 실제 근거 행
    assert await _count(KbRelation, KbRelation.status != "candidate") == 0
    assert await _count(KbRelation, KbRelation.evidence_count < 1) == 0  # DB 트리거가 근거 수를 동기화
    assert await _count(KbAssertionEvidence) >= 2
    assert await _count(KbEvidence) > 0
    # 링킹은 해소기를 실제로 호출했다
    assert st[STAGE_LINKING].detail["entities_linked"] > 0


async def test_work_and_personal_documents_are_isolated_and_derived_rows_follow_sources():
    await _register(DOC_TEXT, scope="personal")
    work_res, work_job = await _register(WORK_TEXT, scope="work")
    assert await _drain(_worker()) == 2

    async with sessionmaker()() as db:
        work_doc_id = work_res.document_id
        # work 문서에서 만든 모든 지식 행은 work_only
        for model in (KbAssertion,):
            rows = list(await db.scalars(select(model).where(model.document_id == work_doc_id)))
            assert rows and {(r.origin_scope, r.visibility) for r in rows} == {("work", "work_only")}
        work_entities = list(
            await db.scalars(
                select(KbEntity).join(KbDocumentEntity, KbDocumentEntity.entity_id == KbEntity.id).where(
                    KbDocumentEntity.document_id == work_doc_id
                )
            )
        )
        assert work_entities and {e.visibility for e in work_entities} == {"work_only"}
        # work 근거로 만든 관계가 global 로 새지 않는다
        leaked = await db.scalar(
            select(func.count()).select_from(KbRelation).join(
                KbRelationEvidence, KbRelationEvidence.relation_id == KbRelation.id
            ).join(KbEvidence, KbEvidence.id == KbRelationEvidence.evidence_id).join(
                KbPassage, KbPassage.id == KbEvidence.passage_id
            ).where(KbPassage.revision_id == work_res.revision_id, KbRelation.visibility != "work_only")
        )
        assert leaked == 0
        # personal 문서의 지식은 global
        assert await db.scalar(select(func.count()).select_from(KbEntity).where(KbEntity.visibility == "global")) > 0
        # 공개 엔터티의 카운트에 work 문서 언급이 섞이지 않는다
        global_docs = await db.scalars(select(KbEntity.document_count).where(KbEntity.visibility == "global"))
        assert max(global_docs) <= 1


async def test_extracting_failure_isolated_then_retry_from_extracting_resumes():
    res, job_id = await _register(DOC_TEXT)

    class FailExtracting:
        calls = 0

        async def __call__(self, ctx):
            FailExtracting.calls += 1
            raise RuntimeError("forced extracting failure")

    reg = StageProcessorRegistry()
    reg.register(STAGE_EXTRACTING, FailExtracting())
    async with sessionmaker()() as db:  # 첫 실패를 곧 최종으로
        job = await db.get(KbPipelineJob, job_id)
        job.max_attempts = 1
        await db.commit()

    assert await _drain(_worker(reg)) == 1
    job = await _job(job_id)
    st = await _stages(job_id)
    assert st[STAGE_EXTRACTING].status == "failed" and "forced extracting failure" in st[STAGE_EXTRACTING].error_message
    assert st[STAGE_LINKING].status == "skipped"
    assert st[STAGE_INDEXING].status == "succeeded"  # 추출 실패가 검색 색인을 막지 않았다
    assert job.status == "degraded" and job.error_code == "max_attempts_exceeded"
    assert await _count(KbRelation) == 0

    # 재개: extracting 부터. 앞선 parsing/chunking 은 다시 돌지 않는다.
    parsing_started = st[STAGE_PARSING].started_at
    out = await retry_pipeline_job(sessionmaker(), job_id, from_stage=STAGE_EXTRACTING)
    assert out.requeued and out.from_stage == STAGE_EXTRACTING
    assert (await _job(job_id)).status == "registered"
    assert await _drain(_worker()) == 1  # 기본(실제) 처리기로 재개

    job2 = await _job(job_id)
    st2 = await _stages(job_id)
    assert st2[STAGE_PARSING].started_at == parsing_started
    assert st2[STAGE_EXTRACTING].status == "degraded" and st2[STAGE_LINKING].status == "succeeded"
    assert st2[STAGE_INDEXING].status == "succeeded"
    assert job2.status == "degraded" and job2.error_code is None
    assert await _count(KbRelation) > 0

    # 재개 후 한 번 더 같은 구간을 돌려도 행이 늘지 않는다(멱등)
    counts = [await _count(m) for m in (KbEntity, KbAssertion, KbRelation, KbRelationEvidence, KbEvidence, KbDocumentEntity)]
    assert (await retry_pipeline_job(sessionmaker(), job_id, from_stage=STAGE_EXTRACTING)).requeued
    assert await _drain(_worker()) == 1
    assert counts == [await _count(m) for m in (KbEntity, KbAssertion, KbRelation, KbRelationEvidence, KbEvidence, KbDocumentEntity)]


async def test_linking_without_resolver_is_held_but_indexing_still_completes(monkeypatch):
    from ttakkari.kb_pipeline import stages

    monkeypatch.setattr(stages, "_load_resolver", lambda: None)
    _res, job_id = await _register(DOC_TEXT)
    assert await _drain(_worker()) == 1
    st = await _stages(job_id)
    assert st[STAGE_LINKING].status == "held" and st[STAGE_LINKING].detail["missing_capability"] == "kb_ontology.resolver"
    assert st[STAGE_INDEXING].status == "succeeded"
    assert st[STAGE_CANONICALIZING].status == "skipped"
    assert (await _job(job_id)).status == "degraded"


async def test_linking_passes_access_not_a_visibility_to_the_resolver():
    _res, job_id = await _register(WORK_TEXT, scope="work")
    seen: list[dict[str, Any]] = []

    from ttakkari.kb_ontology import resolve_entity

    async def spy(db, name, **kwargs):
        seen.append(kwargs)
        return await resolve_entity(db, name, **kwargs)

    reg = StageProcessorRegistry()
    reg.register(STAGE_LINKING, LinkingProcessor(resolver=spy))
    assert await _drain(_worker(reg)) == 1
    assert seen
    for kw in seen:
        assert "visibility" not in kw and "origin_scope" not in kw
        assert kw["source_access"] is not None and kw["source_access"].scope == "work"
    assert (await _stages(job_id))[STAGE_LINKING].status == "succeeded"


async def test_l2_adapter_runs_verifies_quotes_and_counts_the_budget():
    _res, job_id = await _register(DOC_TEXT)
    fake = FakeL2()
    reg = StageProcessorRegistry()
    reg.register(STAGE_EXTRACTING, ExtractingProcessor(adapter_factory=lambda: fake))
    assert await _drain(_worker(reg)) == 1

    st = (await _stages(job_id))[STAGE_EXTRACTING]
    assert st.status == "succeeded"  # L2 가 가용하고 예산 안이면 저하가 아니다
    assert st.detail["l2"]["adapter"] == "fake_l2" and st.detail["l2"]["calls_made"] == fake.calls >= 1
    assert st.detail["rejected_assertions"] == 1 and st.detail["rejected_relations"] == 1
    assert st.detail["hallucinated_quotes_count"] >= 2
    async with sessionmaker()() as db:
        exposes = list(await db.scalars(select(KbAssertion).where(KbAssertion.predicate == "exposes")))
        assert len(exposes) == 1 and exposes[0].extractor_tier == "L2"
        assert not list(await db.scalars(select(KbAssertion).where(KbAssertion.predicate == "hallucinated")))
        rel = (await db.scalars(select(KbRelation).where(KbRelation.relation_type == "uses"))).all()
        assert len(rel) == 1 and rel[0].status == "candidate"
        # 정확 오프셋으로 검증된 근거: 저장된 구간이 패시지 본문과 글자 그대로 같다
        ev = (await db.scalars(select(KbEvidence).join(KbRelationEvidence, KbRelationEvidence.evidence_id == KbEvidence.id).where(
            KbRelationEvidence.relation_id == rel[0].id))).one()
        passage = await db.get(KbPassage, ev.passage_id)
        assert passage.body[ev.span_start:ev.span_end] == "서비스는 Pod 집합을 노출한다."


async def test_l2_unreachable_is_degraded_with_reason_and_l0_l1_results_kept():
    _res, job_id = await _register(DOC_TEXT)
    reg = StageProcessorRegistry()
    reg.register(STAGE_EXTRACTING, ExtractingProcessor(adapter_factory=lambda: FakeL2(reachable=False)))
    assert await _drain(_worker(reg)) == 1
    st = (await _stages(job_id))[STAGE_EXTRACTING]
    assert st.status == "degraded" and st.detail["l2"]["reason"] == "fake_unreachable"
    assert await _count(KbEntity) > 0 and await _count(KbRelation) > 0


async def test_daily_budget_is_shared_across_documents_and_exhaustion_degrades(monkeypatch):
    monkeypatch.setattr(get_settings(), "kb_l2_daily_budget", 1)
    monkeypatch.setattr(get_settings(), "kb_l2_max_calls_per_doc", 8)
    fake = FakeL2()
    reg = StageProcessorRegistry()
    reg.register(STAGE_EXTRACTING, ExtractingProcessor(adapter_factory=lambda: fake))
    _r1, job1 = await _register(DOC_TEXT)
    assert await _drain(_worker(reg)) == 1
    _r2, job2 = await _register(DOC_TEXT + "\n\n다른 문단이 하나 더 있다. `another_identifier` 를 쓴다.\n")
    assert await _drain(_worker(reg)) == 1
    first = (await _stages(job1))[STAGE_EXTRACTING]
    second = (await _stages(job2))[STAGE_EXTRACTING]
    assert first.detail["l2"]["calls_made"] == 1  # 마지막 남은 1회가 L2 에 도달한다(L1 이 먼저 깎지 않는다)
    assert second.status == "degraded" and second.detail["l2"]["reason"] == "daily_budget_exhausted"
    assert second.detail["l2"]["calls_made"] == 0 and fake.calls == 1


async def test_relation_without_verified_evidence_is_never_stored():
    from ttakkari.kb_pipeline.extractor_step import ExtractionStageResult, persist_extracted_knowledge
    from ttakkari.kb_pipeline.quote_verifier import CandidateEntity, CandidateRelation

    res, _job = await _register(DOC_TEXT)
    extraction = ExtractionStageResult(
        status="succeeded",
        entities=[CandidateEntity(name="A", canonical_name="A"), CandidateEntity(name="B", canonical_name="B")],
        relations=[CandidateRelation(source_name="A", target_name="B", relation_type="uses", evidence_quote="x")],
    )
    async with sessionmaker()() as db:
        summary = await persist_extracted_knowledge(
            db, extraction, document_id=res.document_id, revision_id=res.revision_id
        )
        await db.commit()
    assert summary.relations_created == 0 and await _count(KbRelation) == 0


async def test_persistence_defaults_to_work_only_when_document_has_no_access_row():
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_pipeline.extractor_step import ExtractionStageResult, persist_extracted_knowledge
    from ttakkari.kb_pipeline.quote_verifier import CandidateEntity

    res, _job = await _register(DOC_TEXT)
    async with sessionmaker()() as db:
        access = await db.get(KbDocumentAccess, res.document_id)
        await db.delete(access)
        await db.commit()
    async with sessionmaker()() as db:
        summary = await persist_extracted_knowledge(
            db,
            ExtractionStageResult(status="succeeded", entities=[CandidateEntity(name="Z", canonical_name="Z", visibility="global", origin_scope="personal")]),
            document_id=res.document_id,
            revision_id=res.revision_id,
        )
        await db.commit()
    assert (summary.origin_scope, summary.visibility) == ("work", "work_only")
    async with sessionmaker()() as db:
        ent = (await db.scalars(select(KbEntity).where(KbEntity.canonical_name == "Z"))).one()
        assert (ent.origin_scope, ent.visibility) == ("work", "work_only")
