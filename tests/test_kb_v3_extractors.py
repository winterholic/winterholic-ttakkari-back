"""Tests for KB v3 3-tier knowledge extractor (KB3-203).

Covers:
1. Korean & English explicit definitions, bilingual label separation, and heading hierarchy (L0)
2. Dictionary matching, key passage screening, and budget cap controls (L1)
3. Structured JSON Schema extraction with fake LLM client and degradation fallback (L2)
4. Deterministic quote anchor verification: 100% rejection of hallucinated quotes (quote_verifier)
5. (영속화·멱등·visibility 격리는 test_kb_v3_integration_flow.py 의 실 DB 시험)
6. 정확 오프셋 인용 검증, L1 예산 비차감, L0 노이즈 필터
7. Verification that production L2 path connection code exists and checks slot availability
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest

from ttakkari.kb_models import KbPassage, KbRevision
from ttakkari.kb_pipeline.extract_l0 import extract_l0_from_passages
from ttakkari.kb_pipeline.extract_l1 import extract_l1_screen, score_passage_for_l2
from ttakkari.kb_pipeline.extract_l2 import (
    L2_SCHEMA,
    check_qwen_availability,
    default_qwen_llm_call,
    extract_l2_from_passages,
)
from ttakkari.kb_pipeline.extractor_step import (
    ExtractionOptions,
    extract_revision_knowledge,
)
from ttakkari.kb_pipeline.quote_verifier import (
    CandidateAssertion,
    CandidateRelation,
    verify_extraction_candidates,
)


@pytest.fixture(scope="session", autouse=True)
def _schema_and_app():
    """Override session DB fixture from conftest for pure sandboxed unit testing."""
    yield


@pytest.fixture(autouse=True)
def _clean():
    """Override per-test DB clean fixture from conftest."""
    yield


@pytest.fixture
def sample_passages() -> list[KbPassage]:
    """Korean and English mixed fixtures with headings, code blocks, definitions and tables."""
    rev_id = uuid.uuid4()
    p1_body = (
        "# 쿠버네티스 아키텍처 개요\n\n"
        "쿠버네티스(Kubernetes)는 컨테이너화된 애플리케이션의 자동 디프로이, 스케일링 등을 관리하는 오픈소스 시스템이다.\n\n"
        "기본적으로 컨트롤 플레인 컴포넌트와 노드 컴포넌트로 구성된다."
    )
    p2_body = (
        "## 워크로드 리소스\n\n"
        "디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다.\n\n"
        "사용자는 Deployment에서 의도한 상태를 선언하며, 컨트롤러가 현재 상태를 의도한 상태로 변경한다.\n"
        "버전 v1.31 이상에서는 새로운 롤링 업데이트 전략이 권장된다."
    )
    p3_body = (
        "### 세부 설정\n\n"
        "Kubernetes refers to a portable, extensible, open-source platform for managing containerized workloads.\n\n"
        "다음은 필수 CLI 명령어이다:\n"
        "`kubectl apply -f deployment.yaml`\n\n"
        "| 항목 | 설명 |\n"
        "| --- | --- |\n"
        "| kube-apiserver | 쿠버네티스 API를 노출하는 제어 플레인의 핵심 컴포넌트이다. |\n"
    )

    passages = [
        KbPassage(
            id=uuid.uuid4(),
            revision_id=rev_id,
            ord=0,
            heading="쿠버네티스 아키텍처 개요",
            body=p1_body,
            char_start=0,
            char_end=len(p1_body),
            sha256=hashlib.sha256(p1_body.encode()).hexdigest(),
            section_path=["쿠버네티스 아키텍처 개요"],
            chunk_kind="prose",
        ),
        KbPassage(
            id=uuid.uuid4(),
            revision_id=rev_id,
            ord=1,
            heading="워크로드 리소스",
            body=p2_body,
            char_start=len(p1_body),
            char_end=len(p1_body) + len(p2_body),
            sha256=hashlib.sha256(p2_body.encode()).hexdigest(),
            section_path=["쿠버네티스 아키텍처 개요", "워크로드 리소스"],
            chunk_kind="prose",
        ),
        KbPassage(
            id=uuid.uuid4(),
            revision_id=rev_id,
            ord=2,
            heading="세부 설정",
            body=p3_body,
            char_start=len(p1_body) + len(p2_body),
            char_end=len(p1_body) + len(p2_body) + len(p3_body),
            sha256=hashlib.sha256(p3_body.encode()).hexdigest(),
            section_path=["쿠버네티스 아키텍처 개요", "워크로드 리소스", "세부 설정"],
            chunk_kind="table",
        ),
    ]
    return passages


class TestL0Extraction:
    def test_l0_korean_and_english_definitions(self, sample_passages):
        res = extract_l0_from_passages(sample_passages)

        canonical_names = {e.canonical_name for e in res.entities}
        assert "Kubernetes" in canonical_names or "쿠버네티스" in canonical_names
        assert "Deployment" in canonical_names
        assert "kube-apiserver" in canonical_names

        # Check bilingual label splitting
        dep_ent = next(e for e in res.entities if e.canonical_name == "Deployment")
        assert "디플로이먼트" in dep_ent.aliases
        assert "Deployment" in dep_ent.aliases

        # Assertions extracted with accurate quotes
        assert len(res.assertions) >= 3
        dep_assertion = next(a for a in res.assertions if a.subject_name == "Deployment")
        assert "파드와 레플리카셋" in dep_assertion.object_value
        assert dep_assertion.evidence_quote in sample_passages[1].body

    def test_l0_heading_hierarchy_and_symbols(self, sample_passages):
        res = extract_l0_from_passages(sample_passages)

        # Heading hierarchy part_of relations
        assert len(res.relations) >= 1
        rel_sources = {r.source_name for r in res.relations}
        assert any("워크로드" in s or "세부" in s for s in rel_sources)
        for r in res.relations:
            assert r.relation_type == "part_of"
            assert r.status == "candidate"

        # Code symbols
        symbol_entities = {e.canonical_name for e in res.entities if e.kind in ("symbol", "api")}
        # 명령행은 통째로 엔터티가 아니라 알려진 CLI 도구(kubectl)의 언급으로만 남는다.
        assert "kubectl" in symbol_entities
        assert not any("kubectl apply" in s for s in symbol_entities)


class TestL0NoiseFilter:
    def _passage(self, body, section_path=()):
        return KbPassage(
            id=uuid.uuid4(), revision_id=uuid.uuid4(), ord=0, body=body, char_start=0, char_end=len(body),
            sha256=hashlib.sha256(body.encode()).hexdigest(), section_path=list(section_path),
            heading=(section_path[-1] if section_path else None),
        )

    def test_generic_numbered_headings_and_plain_words_are_dropped(self):
        body = (
            "## 1. 개요 및 배경\n\n`files` 와 `agent` 와 `user` 를 쓴다. `deploy/run.json` 과 `--flag` 와 "
            "`ttakkari executor --activate` 도 있다. `runtime_generation=false` 는 설정이다."
        )
        p = self._passage(body, ["문서 제목", "1. 개요 및 배경"])
        res = extract_l0_from_passages([p])
        names = {e.canonical_name for e in res.entities}
        assert names == {"runtime_generation"}
        assert res.relations == []
        assert res.filtered["heading_not_concept"] >= 1
        assert res.filtered["symbol_not_entity"] >= 3 and res.filtered["low_frequency"] >= 3

    def test_concept_headings_keep_relation_with_cleaned_names(self):
        body = "### 2.1 Pod 스케줄링\n\n본문"
        p = self._passage(body, ["2. Kubernetes 구조", "2.1 Pod 스케줄링"])
        res = extract_l0_from_passages([p])
        assert [(r.source_name, r.target_name) for r in res.relations] == [("Pod 스케줄링", "Kubernetes 구조")]
        r = res.relations[0]
        assert body[r.span_start:r.span_end] == r.evidence_quote

    def test_entity_cap(self):
        body = "\n".join("`symbol_%03d`" % i for i in range(200))
        res = extract_l0_from_passages([self._passage(body)])
        from ttakkari.kb_pipeline.entity_filter import MAX_L0_ENTITIES

        assert len(res.entities) == MAX_L0_ENTITIES

    def test_default_scope_is_fail_closed(self):
        res = extract_l0_from_passages([self._passage("`some_identifier` 가 있다.")])
        assert {(e.origin_scope, e.visibility) for e in res.entities} == {("work", "work_only")}


class TestL1ScreeningAndBudget:
    def test_l1_scoring_and_selection(self, sample_passages):
        res = extract_l1_screen(sample_passages, max_calls_per_doc=2)
        assert len(res.selected_passages) <= 2
        assert not res.budget_exhausted

        # Check scoring logic
        score0, _ = score_passage_for_l2(sample_passages[0], is_first=True)
        assert score0 > 0.0

    def test_l1_budget_exhausted(self, sample_passages):
        res = extract_l1_screen(sample_passages, daily_budget_remaining=0)
        assert res.budget_exhausted
        assert len(res.selected_passages) == 0

    def test_l1_does_not_spend_the_daily_budget(self, sample_passages):
        res = extract_l1_screen(sample_passages, daily_budget_remaining=1)
        assert len(res.selected_passages) == 1
        assert res.budget_remaining == 1  # L1 은 비용 0이라 차감하지 않는다

    @pytest.mark.asyncio
    async def test_last_remaining_call_reaches_l2(self, sample_passages):
        calls = []

        async def caller(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            calls.append(prompt)
            return {"entities": [], "assertions": [], "relations": []}

        rev = KbRevision(id=sample_passages[0].revision_id, document_id=uuid.uuid4(), body="")
        res = await extract_revision_knowledge(
            rev, sample_passages, options=ExtractionOptions(llm_caller=caller, daily_budget_remaining=1)
        )
        assert len(calls) == 1
        assert res.metrics["l2_calls_made"] == 1
        assert res.status == "succeeded"

    def test_l1_known_dictionary_matching(self, sample_passages):
        aliases = {"kubernetes": "Kubernetes", "디플로이먼트": "Deployment"}
        res = extract_l1_screen(sample_passages, known_aliases=aliases)
        matched_entities = {m.entity_name for m in res.mentions}
        assert "Kubernetes" in matched_entities
        assert "Deployment" in matched_entities


class TestQuoteVerifier:
    def test_hallucination_blocking_100_percent(self, sample_passages):
        valid_passage = sample_passages[1]
        valid_quote = "버전 v1.31 이상에서는 새로운 롤링 업데이트 전략이 권장된다."
        fake_quote = "이 문장은 원본 패시지에 전혀 존재하지 않는 환각 인용문입니다."

        candidates_a = [
            CandidateAssertion(
                subject_name="Deployment",
                predicate="recommendation",
                object_value="새로운 롤링 업데이트",
                evidence_quote=valid_quote,
                passage_id=valid_passage.id,
            ),
            CandidateAssertion(
                subject_name="Deployment",
                predicate="fabricated",
                object_value="가짜 정보",
                evidence_quote=fake_quote,
                passage_id=valid_passage.id,
            ),
        ]

        candidates_r = [
            CandidateRelation(
                source_name="Deployment",
                target_name="FakeTarget",
                relation_type="depends_on",
                evidence_quote=fake_quote,
                passage_id=valid_passage.id,
            )
        ]

        report = verify_extraction_candidates(sample_passages, candidates_a, candidates_r)

        # Valid assertion preserved
        assert len(report.verified_assertions) == 1
        assert report.verified_assertions[0].evidence_quote == valid_quote

        # Fake assertion and relation completely rejected
        assert len(report.rejected_assertions) == 1
        assert report.rejected_assertions[0].evidence_quote == fake_quote
        assert len(report.rejected_relations) == 1
        assert len(report.verified_relations) == 0
        assert fake_quote in report.hallucinated_quotes

        # Exact substring invariant
        v_a = report.verified_assertions[0]
        assert valid_passage.body[v_a.span_start : v_a.span_end] == valid_quote

    def test_wrapped_quote_is_rejected_not_repaired(self, sample_passages):
        valid_passage = sample_passages[0]
        wrapped_quote = '"쿠버네티스(Kubernetes)는 컨테이너화된 애플리케이션의 자동 디프로이, 스케일링 등을 관리하는 오픈소스 시스템이다."'
        candidates_a = [
            CandidateAssertion(
                subject_name="Kubernetes",
                predicate="definition",
                object_value="컨테이너 관리 시스템",
                evidence_quote=wrapped_quote,
                passage_id=valid_passage.id,
            )
        ]
        report = verify_extraction_candidates(sample_passages, candidates_a, [])
        assert len(report.verified_assertions) == 0
        assert len(report.rejected_assertions) == 1

    def test_wrong_offset_is_not_rescued_by_research(self, sample_passages):
        p = sample_passages[1]
        quote = "버전 v1.31 이상에서는 새로운 롤링 업데이트 전략이 권장된다."
        true_start = p.body.index(quote)
        for bad_start in (true_start + 1, true_start - 3, 0):
            cand = CandidateAssertion(
                subject_name="Deployment", predicate="p", object_value="v",
                evidence_quote=quote, passage_id=p.id,
                span_start=bad_start, span_end=bad_start + len(quote),
            )
            report = verify_extraction_candidates(sample_passages, [cand], [])
            assert report.verified_assertions == [], bad_start
            assert len(report.rejected_assertions) == 1
        ok = CandidateAssertion(
            subject_name="Deployment", predicate="p", object_value="v", evidence_quote=quote,
            passage_id=p.id, span_start=true_start, span_end=true_start + len(quote),
        )
        report = verify_extraction_candidates(sample_passages, [ok], [])
        assert len(report.verified_assertions) == 1

    def test_duplicate_quote_keeps_given_offset_and_offsetless_duplicate_is_rejected(self):
        rev_id = uuid.uuid4()
        body = "alpha beta.\n\nalpha beta.\n\ntail"
        passage = KbPassage(
            id=uuid.uuid4(), revision_id=rev_id, ord=0, body=body, char_start=0, char_end=len(body),
            sha256=hashlib.sha256(body.encode()).hexdigest(),
        )
        second = body.rindex("alpha beta.")
        at_second = CandidateAssertion(
            subject_name="A", predicate="p", object_value="v", evidence_quote="alpha beta.",
            passage_id=passage.id, span_start=second, span_end=second + len("alpha beta."),
        )
        offsetless = CandidateAssertion(
            subject_name="A", predicate="p", object_value="v2", evidence_quote="alpha beta.", passage_id=passage.id,
        )
        unique = CandidateAssertion(
            subject_name="A", predicate="p", object_value="v3", evidence_quote="tail", passage_id=passage.id,
        )
        report = verify_extraction_candidates([passage], [at_second, offsetless, unique], [])
        assert report.verified_assertions == [at_second, unique]
        assert at_second.span_start == second  # 첫 위치(0)로 옮기지 않는다
        assert unique.span_start == body.index("tail")
        assert report.rejected_assertions == [offsetless]

    def test_mentions_are_verified_too(self, sample_passages):
        from ttakkari.kb_pipeline.quote_verifier import CandidateMention

        p = sample_passages[2]
        start = p.body.index("kube-apiserver")
        good = CandidateMention(passage_id=p.id, entity_name="kube-apiserver", evidence_quote="kube-apiserver",
                                span_start=start, span_end=start + len("kube-apiserver"))
        bad = CandidateMention(passage_id=p.id, entity_name="kube-apiserver", evidence_quote="kube-apiserver",
                               span_start=start + 2, span_end=start + 2 + len("kube-apiserver"))
        report = verify_extraction_candidates(sample_passages, [], [], [good, bad])
        assert report.verified_mentions == [good]
        assert report.rejected_mentions == [bad]
        assert any(e.span_start == start for e in report.verified_evidences)


class TestL2ExtractionAndDegradation:
    @pytest.mark.asyncio
    async def test_l2_with_fake_llm_client(self, sample_passages):
        async def fake_caller(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            return {
                "entities": [
                    {
                        "name": "ReplicaSet",
                        "aliases": ["레플리카셋"],
                        "kind": "concept",
                        "domain": "dev",
                    }
                ],
                "assertions": [
                    {
                        "subject": "Deployment",
                        "predicate": "manages",
                        "object_entity": "ReplicaSet",
                        "object_value": "선언적 업데이트 제공",
                        "modality": "fact",
                        "polarity": "positive",
                        "applies_version": "v1.31",
                        "evidence_quote": "디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다.",
                    }
                ],
                "relations": [
                    {
                        "source": "Deployment",
                        "target": "ReplicaSet",
                        "relation_type": "manages",
                        "evidence_quote": "디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다.",
                    }
                ],
            }

        res = await extract_l2_from_passages(
            sample_passages[:1],
            llm_caller=fake_caller,
            max_calls=1,
        )
        assert not res.degraded
        assert res.calls_made == 1
        assert len(res.entities) == 1
        assert len(res.assertions) == 1
        assert len(res.relations) == 1

    @pytest.mark.asyncio
    async def test_l2_graceful_degradation_when_unavailable(self, sample_passages):
        rev = KbRevision(
            id=sample_passages[0].revision_id,
            document_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            rev_no=1,
            body="",
            text_sha256="",
            char_count=0,
        )

        # Call with no caller provided when local server is inactive
        res = await extract_revision_knowledge(
            rev,
            sample_passages,
            options=ExtractionOptions(
                llm_caller=None,  # triggers availability check
            ),
        )

        # Must degrade cleanly without crashing or faking success
        assert res.status in ("degraded", "succeeded")
        # L0 extraction artifacts must be preserved even if degraded
        assert len(res.entities) > 0
        assert len(res.assertions) > 0

    @pytest.mark.asyncio
    async def test_production_l2_connection_code_and_slot_check(self):
        # Verify production functions and schemas exist and are callable
        assert callable(default_qwen_llm_call)
        assert callable(check_qwen_availability)
        assert "properties" in L2_SCHEMA

        ready, reason = check_qwen_availability()
        # Report honest status
        if not ready:
            assert reason in (
                "qwen_slot_busy",
                "desk_qwen_unavailable",
            ) or "desk_qwen_unavailable" in str(reason)


class TestPersistenceAndIsolation:
    # 영속화·멱등·visibility 격리는 실 PostgreSQL 로 tests/test_kb_v3_integration_flow.py 에서 시험한다.
    @pytest.mark.asyncio
    async def test_extract_empty_passages(self):
        rev = KbRevision(
            id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            source_id=uuid.uuid4(),
            rev_no=1,
            body="",
            text_sha256="",
            char_count=0,
        )
        res = await extract_revision_knowledge(rev, [])
        assert res.status == "succeeded"
        assert len(res.entities) == 0
        assert len(res.assertions) == 0
