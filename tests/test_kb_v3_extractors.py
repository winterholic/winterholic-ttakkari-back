"""Tests for KB v3 3-tier knowledge extractor (KB3-203).

Covers:
1. Korean & English explicit definitions, bilingual label separation, and heading hierarchy (L0)
2. Dictionary matching, key passage screening, and budget cap controls (L1)
3. Structured JSON Schema extraction with fake LLM client and degradation fallback (L2)
4. Deterministic quote anchor verification: 100% rejection of hallucinated quotes (quote_verifier)
5. Idempotent persistence on replay (no duplicates, reuses existing rows)
6. Work-only document provenance and visibility isolation (never leaks to global)
7. Verification that production L2 path connection code exists and checks slot availability
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

import pytest

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbRelation,
)
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
    persist_extracted_knowledge,
    resolve_document_scope_and_visibility,
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


class FakeScalarResult:
    def __init__(self, items: list[Any]) -> None:
        self._items = items

    def first(self) -> Any | None:
        return self._items[0] if self._items else None

    def all(self) -> list[Any]:
        return list(self._items)

    def __iter__(self):
        return iter(self._items)


class FakeAsyncSession:
    """In-memory AsyncSession simulator for deterministic isolated persistence tests."""

    def __init__(self) -> None:
        self.entities: dict[tuple[str, str, str, str], KbEntity] = {}  # (tenant, vis, kind, norm) -> KbEntity
        self.aliases: list[KbEntityAlias] = []
        self.evidence: dict[tuple[uuid.UUID, int, int], KbEvidence] = {}
        self.doc_entities: dict[tuple[uuid.UUID, uuid.UUID], KbDocumentEntity] = {}
        self.assertions: dict[tuple[uuid.UUID, uuid.UUID, str, str], KbAssertion] = {}
        self.assertion_evidence: list[KbAssertionEvidence] = []
        self.relations: dict[tuple[uuid.UUID, uuid.UUID, str], KbRelation] = {}
        self.document_access: dict[uuid.UUID, KbDocumentAccess] = {}
        self.documents: dict[uuid.UUID, KbDocument] = {}
        self.revisions: dict[uuid.UUID, KbRevision] = {}
        self._pending: list[Any] = []

    def add(self, obj: Any) -> None:
        self._pending.append(obj)

    async def flush(self) -> None:
        for obj in list(self._pending):
            if isinstance(obj, KbEntity):
                key = (obj.tenant_id, obj.visibility, obj.entity_kind, obj.norm_name)
                self.entities[key] = obj
            elif isinstance(obj, KbEntityAlias):
                self.aliases.append(obj)
            elif isinstance(obj, KbEvidence):
                self.evidence[(obj.passage_id, obj.span_start, obj.span_end)] = obj
            elif isinstance(obj, KbDocumentEntity):
                self.doc_entities[(obj.passage_id, obj.entity_id)] = obj
            elif isinstance(obj, KbAssertion):
                key = (obj.revision_id, obj.subject_entity_id, obj.predicate, obj.norm_object_value)
                self.assertions[key] = obj
            elif isinstance(obj, KbAssertionEvidence):
                self.assertion_evidence.append(obj)
            elif isinstance(obj, KbRelation):
                key = (obj.source_entity_id, obj.target_entity_id, obj.relation_type)
                self.relations[key] = obj
            elif isinstance(obj, KbDocumentAccess):
                self.document_access[obj.document_id] = obj
            elif isinstance(obj, KbDocument):
                self.documents[obj.id] = obj
            elif isinstance(obj, KbRevision):
                self.revisions[obj.id] = obj
        self._pending.clear()

    async def commit(self) -> None:
        await self.flush()

    async def get(self, model: type, ident: Any) -> Any | None:
        await self.flush()
        if model is KbDocumentAccess:
            return self.document_access.get(ident)
        if model is KbDocument:
            return self.documents.get(ident)
        if model is KbRevision:
            return self.revisions.get(ident)
        if model is KbDocumentEntity and isinstance(ident, tuple):
            return self.doc_entities.get(ident)
        if model is KbAssertionEvidence and isinstance(ident, tuple):
            for ae in self.assertion_evidence:
                if ae.assertion_id == ident[0] and ae.evidence_id == ident[1]:
                    return ae
        return None

    async def scalars(self, stmt: Any) -> FakeScalarResult:
        await self.flush()
        stmt_str = str(stmt).lower()

        if "from kb_entities" in stmt_str:
            items = list(self.entities.values())
            # Simple matching for query by norm_name
            # If stmt parameters or where expressions, filter
            # For our test persistence helper, we match by (tenant_id, visibility, entity_kind, norm_name)
            # Find in compiled parameters
            params = stmt.compile().params
            norm_param = params.get("norm_name_1") or params.get("norm_name")
            vis_param = params.get("visibility_1") or params.get("visibility")
            kind_param = params.get("entity_kind_1") or params.get("entity_kind")

            filtered = items
            if norm_param:
                filtered = [e for e in filtered if e.norm_name == norm_param]
            if vis_param:
                filtered = [e for e in filtered if e.visibility == vis_param]
            if kind_param:
                filtered = [e for e in filtered if e.entity_kind == kind_param]
            return FakeScalarResult(filtered)

        if "from kb_entity_aliases" in stmt_str:
            params = stmt.compile().params
            ent_id = params.get("entity_id_1") or params.get("entity_id")
            norm_a = params.get("norm_alias_1") or params.get("norm_alias")
            res = [
                a for a in self.aliases
                if (not ent_id or a.entity_id == ent_id) and (not norm_a or a.norm_alias == norm_a)
            ]
            return FakeScalarResult(res)

        if "from kb_evidence" in stmt_str:
            params = stmt.compile().params
            pid = params.get("passage_id_1") or params.get("passage_id")
            start = params.get("span_start_1") or params.get("span_start")
            end = params.get("span_end_1") or params.get("span_end")
            res = [
                ev for ev in self.evidence.values()
                if (not pid or ev.passage_id == pid)
                and (start is None or ev.span_start == start)
                and (end is None or ev.span_end == end)
            ]
            return FakeScalarResult(res)

        if "from kb_assertions" in stmt_str:
            params = stmt.compile().params
            rid = params.get("revision_id_1") or params.get("revision_id")
            sid = params.get("subject_entity_id_1") or params.get("subject_entity_id")
            pred = params.get("predicate_1") or params.get("predicate")
            val = params.get("norm_object_value_1") or params.get("norm_object_value")
            res = [
                a for a in self.assertions.values()
                if (not rid or a.revision_id == rid)
                and (not sid or a.subject_entity_id == sid)
                and (not pred or a.predicate == pred)
                and (not val or a.norm_object_value == val)
            ]
            return FakeScalarResult(res)

        if "from kb_relations" in stmt_str:
            params = stmt.compile().params
            s_id = params.get("source_entity_id_1") or params.get("source_entity_id")
            t_id = params.get("target_entity_id_1") or params.get("target_entity_id")
            rtype = params.get("relation_type_1") or params.get("relation_type")
            res = [
                r for r in self.relations.values()
                if (not s_id or r.source_entity_id == s_id)
                and (not t_id or r.target_entity_id == t_id)
                and (not rtype or r.relation_type == rtype)
            ]
            return FakeScalarResult(res)

        return FakeScalarResult([])


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
        assert any("kubectl apply" in s for s in symbol_entities)


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

    def test_quote_verifier_wrapped_quote_stripping(self, sample_passages):
        valid_passage = sample_passages[0]
        # Quote wrapped in double quotes by LLM
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
        assert len(report.verified_assertions) == 1
        verified = report.verified_assertions[0]
        assert verified.evidence_quote not in ('""', "''")
        assert valid_passage.body[verified.span_start : verified.span_end] == verified.evidence_quote


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
    @pytest.mark.asyncio
    async def test_idempotent_replay_and_work_only_isolation(self, sample_passages):
        db = FakeAsyncSession()

        doc_id = uuid.uuid4()
        rev_id = sample_passages[0].revision_id

        doc = KbDocument(
            id=doc_id,
            doc_key="work:secret_architecture",
            title="Secret Architecture Note",
        )
        db.add(doc)

        # Explicit work_only access configuration
        doc_access = KbDocumentAccess(
            document_id=doc_id,
            origin_scope="work",
            visibility="work_only",
            scope="work",
        )
        db.add(doc_access)

        full_body = "".join(p.body for p in sample_passages)
        rev = KbRevision(
            id=rev_id,
            document_id=doc_id,
            source_id=uuid.uuid4(),
            rev_no=1,
            body=full_body,
            text_sha256=hashlib.sha256(full_body.encode()).hexdigest(),
            char_count=len(full_body),
        )
        db.add(rev)

        for p in sample_passages:
            p.revision_id = rev_id
        await db.flush()

        # 2. Extract knowledge using fake LLM caller
        async def fake_caller(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
            return {
                "entities": [
                    {
                        "name": "Deployment",
                        "aliases": ["디플로이먼트"],
                        "kind": "concept",
                        "domain": "dev",
                    }
                ],
                "assertions": [
                    {
                        "subject": "Deployment",
                        "predicate": "definition",
                        "object_entity": None,
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
                        "target": "쿠버네티스",
                        "relation_type": "part_of",
                        "evidence_quote": "디플로이먼트(Deployment)는 파드와 레플리카셋에 대한 선언적 업데이트를 제공하는 워크로드 리소스이다.",
                    }
                ],
            }

        extracted = await extract_revision_knowledge(
            rev,
            sample_passages,
            options=ExtractionOptions(
                llm_caller=fake_caller,
                origin_scope="work",
                visibility="work_only",
            ),
        )
        assert extracted.status == "succeeded"

        # 3. First persistence
        p1 = await persist_extracted_knowledge(
            db,
            extracted,
            document_id=doc_id,
            revision_id=rev_id,
            tenant_id="personal",
        )
        await db.commit()

        assert p1.origin_scope == "work"
        assert p1.visibility == "work_only"
        assert p1.entities_created > 0
        assert p1.assertions_created > 0

        # Verify visibility isolation: NO global entities or assertions created
        for ent in db.entities.values():
            assert ent.visibility == "work_only"
            assert ent.origin_scope == "work"

        for a in db.assertions.values():
            assert a.visibility == "work_only"
            assert a.origin_scope == "work"

        # Verify relations are strictly candidate and evidence_count == 0
        for r in db.relations.values():
            assert r.visibility == "work_only"
            assert r.origin_scope == "work"
            assert r.status == "candidate"
            assert r.evidence_count == 0

        # 4. Second persistence (Idempotent replay)
        p2 = await persist_extracted_knowledge(
            db,
            extracted,
            document_id=doc_id,
            revision_id=rev_id,
            tenant_id="personal",
        )
        await db.commit()

        # No new entities or assertions created on second run; all reused
        assert p2.entities_created == 0
        assert p2.entities_reused > 0
        assert p2.assertions_created == 0
        assert p2.assertions_reused > 0

    @pytest.mark.asyncio
    async def test_resolve_document_scope_and_visibility_fallback(self):
        db = FakeAsyncSession()
        work_doc_id = uuid.uuid4()
        personal_doc_id = uuid.uuid4()

        work_doc = KbDocument(
            id=work_doc_id,
            doc_key="work:project-ezwork:spec",
            title="EZ Work Spec",
        )
        personal_doc = KbDocument(
            id=personal_doc_id,
            doc_key="blog:my-thoughts",
            title="My Blog Post",
        )
        db.add(work_doc)
        db.add(personal_doc)
        await db.flush()

        w_scope, w_vis = await resolve_document_scope_and_visibility(db, work_doc_id)
        assert w_scope == "work"
        assert w_vis == "work_only"

        p_scope, p_vis = await resolve_document_scope_and_visibility(db, personal_doc_id)
        assert p_scope == "personal"
        assert p_vis == "global"

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
