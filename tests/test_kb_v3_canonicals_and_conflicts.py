"""지식 도서관(KB v3) 다중 문서 통합 정본 요약·상충 탐지·최신성 및 유계 그래프 검증 (KB3-302).

시나리오:
- S06: 다중 문서 통합 정본(kb_entity_canonicals) 생성 및 리비전 갱신(supersedes_id) 시 과거 주장 자동 superseded 전환 및 원장 보존
- S07: 동일 조건 상충 주장(포트 6379 vs 6380) 자동 탐지(value_contradiction), 양측 disputed 전환, 해결 API(resolve_conflict)
- 조건부 비상충: 버전(1.31 vs 1.32), 모달리티(fact vs recommendation), 유효기간 상이 시 상충 확정 금지
- 극성 상충: 동일 조건 긍정 vs 부정(polarity_contradiction) 탐지
- 5대 해결 계약: resolved_a, resolved_b, resolved_both_valid, merged, kept_separate 검증
- 멱등성 및 버전 안정성: 내용 불변 시 version_no 유지, 중복 충돌 방지
- 숨김/비활성 전파: 문서 hidden=True 시 정본에서 인용 제외 및 재합성
- 가시성 격리(Zero-leakage): 회사(work_only) 주장이 개인(global) 주장의 status/disputed 를 오염시키지 않음
- 유계 온톨로지 그래프: 허브 조회 및 1-hop 서브그래프(원문 증거 인용, offset, limit, truncated)
- 파이프라인 처리기: CanonicalizingProcessor 단계 실행 및 StageProcessorRegistry 연동
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import DDL, event
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.kb_models import (
    KbDocument,
    KbEvidence,
    KbPassage,
    KbRevision,
    KbSource,
)
from ttakkari.kb_ontology.canonicals import (
    get_active_assertions_for_entity,
    synthesize_canonical_for_entity,
    update_superseded_assertions,
)
from ttakkari.kb_ontology.conflicts import (
    detect_conflicts_for_entity,
    list_conflicts,
    resolve_conflict,
)
from ttakkari.kb_ontology.graph_query import (
    get_ontology_hubs,
    query_1hop_subgraph,
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
from ttakkari.kb_pipeline.adapters import StageProcessorRegistry
from ttakkari.kb_pipeline.canonicalizing_processor import CanonicalizingProcessor
from ttakkari.kb_pipeline.types import (
    STAGE_CANONICALIZING,
    STAGE_STATUS_SUCCEEDED,
    StageContext,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.models import utcnow


# SQLite 호환성 처리 (ARRAY 타입 및 PostgreSQL 전용 트리거 DDL 우회)
@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(type_, compiler, **kw):
    return "TEXT"


_orig_ddl_call = DDL.__call__


def _safe_ddl_call(self, target, bind, **kw):
    if bind.dialect.name == "sqlite":
        return
    return _orig_ddl_call(self, target, bind, **kw)


DDL.__call__ = _safe_ddl_call

ALL_V3_TABLES = [
    KbSource.__table__,
    KbDocument.__table__,
    KbDocumentAccess.__table__,
    KbRevision.__table__,
    KbObservation.__table__,
    KbPassage.__table__,
    KbEvidence.__table__,
    KbEntity.__table__,
    KbEntityAlias.__table__,
    KbDocumentEntity.__table__,
    KbAssertion.__table__,
    KbAssertionEvidence.__table__,
    KbRelation.__table__,
    KbRelationEvidence.__table__,
    KbEntityCanonical.__table__,
    KbConflict.__table__,
    KbPipelineJob.__table__,
    KbPipelineStage.__table__,
]


# 상위 conftest.py 의 PostgreSQL 자동 연결을 오버라이드하여 격리된 SQLite in-memory 환경에서 테스트
@pytest.fixture(scope="session", autouse=True)
def _schema_and_app() -> None:
    yield


@pytest.fixture(autouse=True)
def _clean() -> None:
    yield


@pytest_asyncio.fixture
async def db() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)

    def _encode_array_parameters(conn, cursor, statement, parameters, context, executemany):
        def encode_row(row):
            if isinstance(row, (tuple, list)):
                return tuple(
                    json.dumps(value) if isinstance(value, list) else value
                    for value in row
                )
            return row

        if executemany:
            parameters = [encode_row(row) for row in parameters]
        else:
            parameters = encode_row(parameters)
        return statement, parameters

    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        _encode_array_parameters,
        retval=True,
    )
    async with engine.begin() as conn:
        for t in ALL_V3_TABLES:
            await conn.run_sync(t.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


# -----------------------------------------------------------------------------
# 헬퍼 함수: 실제 원장 및 온톨로지 데이터 셋업
# -----------------------------------------------------------------------------
async def create_document_bundle(
    db: AsyncSession,
    *,
    doc_key: str,
    title: str,
    text: str,
    rev_no: int = 1,
    supersedes_id: uuid.UUID | None = None,
    scope: str = "personal",
    visibility: str = "global",
    hidden: bool = False,
    existing_document: KbDocument | None = None,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> tuple[KbDocument, KbRevision, KbPassage, KbEvidence]:
    """불변 원장(문서, 리비전, 패시지, 근거)과 접근 제어 행을 실제 SHA256 과 함께 생성한다."""
    source_id = uuid.uuid4()
    source_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    db.add(
        KbSource(
            id=source_id,
            source_key=doc_key,
            kind="test",
            locator=f"file://{doc_key}",
            sha256=source_sha,
            byte_size=len(text.encode("utf-8")),
            store_path=f"test/{source_sha}",
        )
    )

    if existing_document is not None:
        doc = existing_document
    else:
        doc = KbDocument(doc_key=doc_key, title=title, lang="ko")
        db.add(doc)
        await db.flush()
        db.add(
            KbDocumentAccess(
                document_id=doc.id,
                tenant_id="owner",
                scope=scope,
                origin_scope=scope,
                visibility=visibility,
                hidden=hidden,
            )
        )

    text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    rev = KbRevision(
        document_id=doc.id,
        source_id=source_id,
        rev_no=rev_no,
        parser_version="test-v1",
        body=text,
        text_sha256=text_sha,
        char_count=len(text),
        supersedes_id=supersedes_id,
        valid_from=valid_from if valid_from is not None else utcnow(),
        valid_to=valid_to,
    )
    db.add(rev)
    await db.flush()

    passage_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    pas = KbPassage(
        revision_id=rev.id,
        ord=1,
        body=text,
        char_start=0,
        char_end=len(text),
        sha256=passage_sha,
        token_estimate=len(text.split()),
    )
    db.add(pas)
    await db.flush()

    evidence_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ev = KbEvidence(
        passage_id=pas.id,
        span_start=0,
        span_end=len(text),
        sha256=evidence_sha,
    )
    db.add(ev)
    await db.flush()

    return doc, rev, pas, ev


async def create_entity(
    db: AsyncSession,
    name: str,
    *,
    entity_kind: str = "technology",
    domain: str = "infra",
    visibility: str = "global",
    summary: str = "",
) -> KbEntity:
    ent = KbEntity(
        tenant_id="personal",
        visibility=visibility,
        origin_scope="personal" if visibility == "global" else "work",
        canonical_name=name,
        norm_name=name.lower().replace(" ", ""),
        entity_kind=entity_kind,
        domain=domain,
        summary=summary,
        status="active",
        confidence=0.9,
    )
    db.add(ent)
    await db.flush()
    return ent


async def link_assertion(
    db: AsyncSession,
    *,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    subject_entity_id: uuid.UUID,
    predicate: str,
    object_value: str,
    evidence_id: uuid.UUID,
    applies_condition: dict[str, Any] | None = None,
    modality: str = "fact",
    polarity: str = "positive",
    trust_score: float = 0.9,
    visibility: str = "global",
    status: str = "active",
) -> KbAssertion:
    assertion = KbAssertion(
        document_id=document_id,
        revision_id=revision_id,
        origin_scope="personal" if visibility == "global" else "work",
        visibility=visibility,
        subject_entity_id=subject_entity_id,
        predicate=predicate,
        object_value=object_value,
        norm_object_value=object_value.strip().lower(),
        modality=modality,
        polarity=polarity,
        applies_condition=applies_condition or {},
        trust_score=trust_score,
        status=status,
    )
    db.add(assertion)
    await db.flush()

    db.add(
        KbAssertionEvidence(
            assertion_id=assertion.id,
            evidence_id=evidence_id,
            relation="supports",
        )
    )
    await db.flush()
    return assertion


# -----------------------------------------------------------------------------
# 1. 시나리오 S06: 다중 문서 통합 정본 생성 및 리비전 갱신(supersedes_id)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s06_revision_supersede(db: AsyncSession) -> None:
    """S06 검증:

    문서 1(rev 1): PostgreSQL 기본 최대 연결 수는 100이다.
    문서 1(rev 2, supersedes_id=rev 1): 사내 표준 PostgreSQL 최대 연결 수는 200이다.
    1) rev 1 주장은 status='superseded' 로 자동 전환
    2) kb_entity_canonicals 정본 요약 및 대표 주장이 rev 2(200) 기준으로 자동 갱신
    3) 과거 리비전(rev 1) 근거 원장 데이터(revisions, passages, evidence)는 불변 보존
    """
    ent = await create_entity(db, "PostgreSQL", summary="오픈소스 RDBMS")

    # 리비전 1 적재
    doc, rev1, pas1, ev1 = await create_document_bundle(
        db,
        doc_key="docs/postgres-guide.md",
        title="PostgreSQL 가이드",
        text="PostgreSQL 기본 최대 연결 수는 100이다.",
        rev_no=1,
    )
    a1 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev1.id,
        subject_entity_id=ent.id,
        predicate="max_connections",
        object_value="100",
        evidence_id=ev1.id,
    )

    # 정본 요약 1차 합성
    card1 = await synthesize_canonical_for_entity(db, ent.id)
    assert card1 is not None
    assert card1.version_no == 1
    assert any(f["value"] == "100" for f in card1.key_facts)
    assert str(doc.id) in card1.source_document_ids
    assert str(ev1.id) in card1.supporting_evidence_ids

    # 리비전 2 갱신 적재 (supersedes_id = rev1.id)
    _, rev2, _pas2, ev2 = await create_document_bundle(
        db,
        doc_key="docs/postgres-guide.md",
        title="PostgreSQL 가이드",
        text="사내 표준 PostgreSQL 최대 연결 수는 200이다.",
        rev_no=2,
        supersedes_id=rev1.id,
        existing_document=doc,
    )
    a2 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev2.id,
        subject_entity_id=ent.id,
        predicate="max_connections",
        object_value="200",
        evidence_id=ev2.id,
    )

    # 리비전 2 처리: 과거 리비전 superseded 전환 및 정본 갱신
    superseded_count = await update_superseded_assertions(db, rev2.id)
    assert superseded_count == 1
    await db.flush()

    # 검증 1: a1의 상태는 superseded 로 전환
    await db.refresh(a1)
    assert a1.status == "superseded"
    assert a1.is_canonical is False

    # 검증 2: a2는 active 유지
    await db.refresh(a2)
    assert a2.status == "active"

    # 검증 3: 정본 카드 재합성 시 rev2(200) 기준으로 갱신되고 version_no 증가
    card2 = await synthesize_canonical_for_entity(db, ent.id)
    assert card2 is not None
    assert card2.version_no == 2
    assert any(f["value"] == "200" for f in card2.key_facts)
    assert not any(f["value"] == "100" for f in card2.key_facts)
    assert str(ev2.id) in card2.supporting_evidence_ids
    assert str(ev1.id) not in card2.supporting_evidence_ids

    # 검증 4: 과거 리비전 원장 불변 보존
    r1_check = await db.get(KbRevision, rev1.id)
    p1_check = await db.get(KbPassage, pas1.id)
    e1_check = await db.get(KbEvidence, ev1.id)
    assert r1_check is not None and r1_check.char_count == len(
        "PostgreSQL 기본 최대 연결 수는 100이다."
    )
    assert p1_check is not None and p1_check.body == "PostgreSQL 기본 최대 연결 수는 100이다."
    assert e1_check is not None and e1_check.sha256 == hashlib.sha256(
        "PostgreSQL 기본 최대 연결 수는 100이다.".encode()
    ).hexdigest()


# -----------------------------------------------------------------------------
# 2. 시나리오 S07: 문서 간 상충(포트 6379 vs 6380) 자동 탐지 및 해결 API
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_s07_conflict_detection_and_resolve(db: AsyncSession) -> None:
    """S07 검증:

    문서 A: Valkey 기본 포트는 6379이다.
    문서 B(동일 조건): Valkey 기본 포트는 6380이다.
    1) 양쪽 주장 모두 status='disputed' 로 전환되고 kb_conflicts(value_contradiction, open) 생성
    2) 정본 카드에 has_open_conflict=True 기록
    3) resolve_conflict(resolved_a) 호출 시 지정 주장이 active(is_canonical=True)로 복귀하고 b는 rejected, conflict 는 resolved_a
    """
    ent = await create_entity(db, "Valkey", summary="고성능 인메모리 데이터 스토어")

    # 문서 A 적재
    doc_a, rev_a, _, ev_a = await create_document_bundle(
        db,
        doc_key="docs/valkey-doc-a.md",
        title="Valkey 공식 매뉴얼 A",
        text="Valkey 기본 포트는 6379이다.",
    )
    a1 = await link_assertion(
        db,
        document_id=doc_a.id,
        revision_id=rev_a.id,
        subject_entity_id=ent.id,
        predicate="port",
        object_value="6379",
        evidence_id=ev_a.id,
    )

    # 문서 B 적재 (동일 조건)
    doc_b, rev_b, _, ev_b = await create_document_bundle(
        db,
        doc_key="docs/valkey-doc-b.md",
        title="Valkey 공식 매뉴얼 B",
        text="Valkey 기본 포트는 6380이다.",
    )
    a2 = await link_assertion(
        db,
        document_id=doc_b.id,
        revision_id=rev_b.id,
        subject_entity_id=ent.id,
        predicate="port",
        object_value="6380",
        evidence_id=ev_b.id,
    )

    # 충돌 탐지 실행
    conflicts = await detect_conflicts_for_entity(db, ent.id)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.conflict_kind == "value_contradiction"
    assert c.status == "open"
    assert c.predicate == "port"
    assert {c.assertion_a_id, c.assertion_b_id} == {a1.id, a2.id}
    await db.flush()

    # 양측 주장 상태 disputed 확인
    await db.refresh(a1)
    await db.refresh(a2)
    assert a1.status == "disputed"
    assert a2.status == "disputed"

    # 정본 카드 합성 시 open conflict 플래그 확인
    card = await synthesize_canonical_for_entity(db, ent.id)
    assert card is not None
    assert card.has_open_conflict is True

    # 해결 API: resolved_a (A 주장 채택)
    resolved = await resolve_conflict(
        db,
        c.id,
        resolution="resolved_a",
        resolution_note="사내 표준은 기본 포트 6379를 준수함",
        resolved_by="admin@test.local",
    )
    assert resolved.status == "resolved_a"
    assert resolved.resolved_at is not None

    await db.refresh(a1)
    await db.refresh(a2)
    assert a1.status == "active"
    assert a1.is_canonical is True
    assert a2.status == "rejected"
    assert a2.is_canonical is False

    # 해결 후 정본 카드 재합성 검증: has_open_conflict False 복귀 및 6379 반영
    await db.refresh(card)
    assert card.has_open_conflict is False
    assert any(f["value"] == "6379" for f in card.key_facts)
    assert not any(f["value"] == "6380" for f in card.key_facts)


# -----------------------------------------------------------------------------
# 3. 비상충 조건 (버전·모달리티·유효기간 차이)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_non_conflict_differing_conditions_and_modalities(db: AsyncSession) -> None:
    """버전 조건이나 modality 가 다르면 값 차이가 있어도 충돌로 판정하지 않는다."""
    ent = await create_entity(db, "Kubernetes", summary="컨테이너 오케스트레이션")

    doc, rev, _, ev = await create_document_bundle(
        db,
        doc_key="docs/k8s-versions.md",
        title="K8s 버전별 요구사항",
        text="v1.31 은 최소 Go 1.22 가 필요하며 v1.32 는 Go 1.23 이 필요하다. 권고 메모리는 4GB 다.",
    )

    # 1. 버전 조건 분기: v1.31 vs v1.32
    a1 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="go_version",
        object_value="1.22",
        applies_condition={"version": "1.31"},
        evidence_id=ev.id,
    )
    a2 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="go_version",
        object_value="1.23",
        applies_condition={"version": "1.32"},
        evidence_id=ev.id,
    )

    # 2. modality 차이: fact vs recommendation
    a3 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="min_memory",
        object_value="2GB",
        modality="fact",
        evidence_id=ev.id,
    )
    a4 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="min_memory",
        object_value="4GB",
        modality="recommendation",
        evidence_id=ev.id,
    )

    conflicts = await detect_conflicts_for_entity(db, ent.id)
    assert len(conflicts) == 0  # 충돌이 발생하지 않아야 함!

    await db.refresh(a1)
    await db.refresh(a2)
    await db.refresh(a3)
    await db.refresh(a4)
    assert a1.status == "active"
    assert a2.status == "active"
    assert a3.status == "active"
    assert a4.status == "active"


@pytest.mark.asyncio
async def test_non_overlapping_revision_valid_periods_do_not_conflict(
    db: AsyncSession,
) -> None:
    """동일 조건의 주장도 원천 리비전 유효기간이 겹치지 않으면 상충이 아니다."""
    ent = await create_entity(db, "ServiceVersion", summary="버전별 서비스 설정")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    _, rev_a, _, ev_a = await create_document_bundle(
        db,
        doc_key="docs/service-old.md",
        title="이전 설정",
        text="port=8080",
        valid_from=start,
        valid_to=start + timedelta(days=30),
    )
    _, rev_b, _, ev_b = await create_document_bundle(
        db,
        doc_key="docs/service-new.md",
        title="새 설정",
        text="port=9090",
        valid_from=start + timedelta(days=30),
        valid_to=start + timedelta(days=60),
    )
    await link_assertion(
        db,
        document_id=rev_a.document_id,
        revision_id=rev_a.id,
        subject_entity_id=ent.id,
        predicate="port",
        object_value="8080",
        evidence_id=ev_a.id,
    )
    await link_assertion(
        db,
        document_id=rev_b.document_id,
        revision_id=rev_b.id,
        subject_entity_id=ent.id,
        predicate="port",
        object_value="9090",
        evidence_id=ev_b.id,
    )

    assert await detect_conflicts_for_entity(db, ent.id) == []


# -----------------------------------------------------------------------------
# 4. 극성 상충 (Polarity Contradiction)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_polarity_contradiction_detection(db: AsyncSession) -> None:
    """동일 조건에서 positive vs negative 극성 대립 시 polarity_contradiction 충돌 생성."""
    ent = await create_entity(db, "Redis", summary="인메모리 캐시")

    doc, rev, _, ev = await create_document_bundle(
        db,
        doc_key="docs/redis-features.md",
        title="Redis 기능",
        text="Redis는 멀티 스레드 I/O를 지원한다 / 지원하지 않는다.",
    )

    await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="supports_multithreading",
        object_value="multithreaded_io",
        polarity="positive",
        evidence_id=ev.id,
    )
    await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="supports_multithreading",
        object_value="multithreaded_io",
        polarity="negative",
        evidence_id=ev.id,
    )

    conflicts = await detect_conflicts_for_entity(db, ent.id)
    assert len(conflicts) == 1
    assert conflicts[0].conflict_kind == "polarity_contradiction"


# -----------------------------------------------------------------------------
# 5. 5대 해결 계약 (resolved_both_valid, merged, kept_separate)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_all_resolution_service_contracts(db: AsyncSession) -> None:
    """resolved_both_valid, merged, kept_separate 계약 검증."""
    ent1 = await create_entity(db, "Service", domain="infra")
    # 현재 DB 유니크 키는 kind 까지 포함하므로 후보는 별도 kind 로 구성한다.
    ent2 = await create_entity(
        db, "Service", domain="business", entity_kind="system"
    )

    doc, rev, _, ev = await create_document_bundle(
        db, doc_key="docs/contracts.md", title="계약 테스트", text="테스트 본문"
    )

    # 1. resolved_both_valid
    a1 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent1.id,
        predicate="protocol",
        object_value="HTTP/1.1",
        evidence_id=ev.id,
    )
    a2 = await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent1.id,
        predicate="protocol",
        object_value="gRPC",
        evidence_id=ev.id,
    )
    conflicts = await detect_conflicts_for_entity(db, ent1.id)
    assert len(conflicts) == 1
    await db.flush()

    c_valid = await resolve_conflict(
        db, conflicts[0].id, resolution="resolved_both_valid", resolution_note="둘 다 지원"
    )
    assert c_valid.status == "resolved_both_valid"
    await db.refresh(a1)
    await db.refresh(a2)
    assert a1.status == "active"
    assert a2.status == "active"

    # 2. ambiguous_entity -> merged
    c_merge = KbConflict(
        conflict_kind="ambiguous_entity",
        entity_id=ent1.id,
        candidate_entity_id=ent2.id,
        status="open",
        visibility="global",
    )
    db.add(c_merge)
    await db.flush()

    res_merged = await resolve_conflict(db, c_merge.id, resolution="merged")
    assert res_merged.status == "merged"
    await db.refresh(ent2)
    assert ent2.status == "merged"
    assert ent2.merged_into_id == ent1.id

    # 3. ambiguous_entity -> kept_separate
    res_sep = await resolve_conflict(db, c_merge.id, resolution="kept_separate")
    assert res_sep.status == "kept_separate"
    await db.refresh(ent2)
    assert ent2.status == "active"
    assert ent2.merged_into_id is None


# -----------------------------------------------------------------------------
# 6. 멱등성 및 버전 안정성
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_idempotency_and_version_stability(db: AsyncSession) -> None:
    """내용이 변하지 않으면 재합성해도 version_no 가 증가하지 않으며 충돌도 중복되지 않는다."""
    ent = await create_entity(db, "Nginx", summary="웹 서버 및 리버스 프록시")
    doc, rev, _, ev = await create_document_bundle(
        db, doc_key="docs/nginx.md", title="Nginx", text="Nginx 기본 포트는 80이다."
    )
    await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="default_port",
        object_value="80",
        evidence_id=ev.id,
    )

    card1 = await synthesize_canonical_for_entity(db, ent.id)
    assert card1 is not None
    assert card1.version_no == 1
    assert card1.canonical_definition == ""

    # 재실행: 내용 변화 없음 -> version_no 1 유지
    card2 = await synthesize_canonical_for_entity(db, ent.id)
    assert card2 is not None
    assert card2.version_no == 1

    # 충돌 탐지 재실행 -> 중복 생성 없음
    conflicts = await detect_conflicts_for_entity(db, ent.id)
    assert len(conflicts) == 0


# -----------------------------------------------------------------------------
# 7. 숨김(hidden) 문서 전파 및 정본 제외
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hidden_document_excluded_from_canonicals(db: AsyncSession) -> None:
    """문서가 hidden=True 로 변경되면 해당 문서의 주장은 정본 합성에 포함되지 않는다."""
    ent = await create_entity(db, "Envoy", summary="엣지/서비스 프록시")
    doc, rev, _, ev = await create_document_bundle(
        db, doc_key="docs/envoy-draft.md", title="Envoy 초안", text="Envoy 초안 내용", hidden=True
    )
    await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev.id,
        subject_entity_id=ent.id,
        predicate="role",
        object_value="proxy",
        evidence_id=ev.id,
    )

    pairs = await get_active_assertions_for_entity(db, ent)
    assert len(pairs) == 0  # hidden 문서의 주장은 적격 목록에서 제외됨!

    card = await synthesize_canonical_for_entity(db, ent.id)
    assert card is not None
    assert len(card.key_facts) == 0
    assert len(card.source_document_ids) == 0


# -----------------------------------------------------------------------------
# 8. 가시성 격리 및 사내 정보 누출 방지 (Zero-leakage)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_visibility_partition_leak_prevention(db: AsyncSession) -> None:
    """회사(work_only) 주장이 개인(global) 주장의 status/disputed 를 변경하지 않으며,

    개인 세션에서 work 정보(문서 ID, 근거 ID, 충돌 배지)가 누출되지 않는다.
    """
    ent = await create_entity(db, "DatabasePort", visibility="global")

    # 1. 개인 문서(global): 포트는 5432
    doc_pub, rev_pub, _, ev_pub = await create_document_bundle(
        db,
        doc_key="docs/public-db.md",
        title="공개 DB 가이드",
        text="DB 표준 포트는 5432이다.",
        scope="personal",
        visibility="global",
    )
    a_global = await link_assertion(
        db,
        document_id=doc_pub.id,
        revision_id=rev_pub.id,
        subject_entity_id=ent.id,
        predicate="port",
        object_value="5432",
        evidence_id=ev_pub.id,
        visibility="global",
    )

    # 2. 회사 문서(work_only): 사내 보안 포트는 9999 (상충 관계)
    doc_work, rev_work, _, ev_work = await create_document_bundle(
        db,
        doc_key="docs/secret-company-db.md",
        title="사내 극비 DB 망",
        text="사내 보안 DB 포트는 9999이다.",
        scope="work",
        visibility="work_only",
    )
    a_work = await link_assertion(
        db,
        document_id=doc_work.id,
        revision_id=rev_work.id,
        subject_entity_id=ent.id,
        predicate="port",
        object_value="9999",
        evidence_id=ev_work.id,
        visibility="work_only",
    )

    # 충돌 탐지
    conflicts = await detect_conflicts_for_entity(db, ent.id)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c.visibility == "work_only"  # 혼합 상충의 가시성은 work_only!

    # 중요 검증 1: global 주장은 여전히 active 상태 유지! (개인 세션에 'disputed' 누출 금지)
    await db.refresh(a_global)
    await db.refresh(a_work)
    assert a_global.status == "active"
    assert a_work.status == "disputed"

    # 중요 검증 2: global 정본 카드는 오직 global 정보만 반영하며 has_open_conflict=False 유지
    card = await synthesize_canonical_for_entity(db, ent.id)
    assert card is not None
    assert card.has_open_conflict is False
    assert any(f["value"] == "5432" for f in card.key_facts)
    assert not any(f["value"] == "9999" for f in card.key_facts)
    assert str(doc_work.id) not in card.source_document_ids
    assert str(ev_work.id) not in card.supporting_evidence_ids

    # 중요 검증 3: 개인 세션(global)에서 충돌 목록 조회 시 work_only 충돌 비노출
    global_conflicts = await list_conflicts(db, caller_visibility="global")
    assert len(global_conflicts) == 0
    with pytest.raises(ValueError, match="caller_visibility"):
        await list_conflicts(db, caller_visibility="invalid")
    assert await list_conflicts(db, tenant_id="other") == []
    with pytest.raises(ValueError, match="not found"):
        await resolve_conflict(db, c.id, resolution="resolved_a")
    assert a_global.status == "active"
    assert a_work.status == "disputed"

    # 회사 세션(work_only)에서는 충돌 확인 가능
    work_conflicts = await list_conflicts(db, caller_visibility="work_only")
    assert len(work_conflicts) == 1


# -----------------------------------------------------------------------------
# 9. 유계 온톨로지 그래프 쿼리 (허브 및 1-Hop 서브그래프)
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_bounded_graph_queries(db: AsyncSession) -> None:
    """허브 노드 및 1-hop 서브그래프 유계 조회, 원문 인용 메타, 권한 격리 검증."""
    ent_hub = await create_entity(db, "Kubernetes", summary="K8s Hub")
    ent_node1 = await create_entity(db, "Deployment", summary="Deployment")
    ent_node2 = await create_entity(db, "Pod", summary="Pod")
    ent_secret = await create_entity(db, "SecretProject", visibility="work_only")
    ent_orphan = await create_entity(db, "UnprovenNode", summary="No source")

    # 문서 및 관계 생성
    doc, rev, pas, ev = await create_document_bundle(
        db,
        doc_key="docs/k8s-arch.md",
        title="K8s 아키텍처",
        text="Kubernetes는 Deployment를 관리하며 Deployment는 Pod를 관리한다.",
    )

    # 허브 노드 카운트 설정
    ent_hub.document_count = 5
    ent_hub.mention_count = 20
    ent_node1.document_count = 3
    ent_node1.mention_count = 10
    ent_node2.document_count = 2
    ent_node2.mention_count = 5
    ent_orphan.document_count = 99
    ent_orphan.mention_count = 999

    db.add_all(
        [
            KbDocumentEntity(
                passage_id=pas.id,
                entity_id=entity.id,
                document_id=doc.id,
                revision_id=rev.id,
                evidence_id=ev.id,
            )
            for entity in (ent_hub, ent_node1, ent_node2)
        ]
    )
    await db.flush()

    work_doc, work_rev, work_pas, work_ev = await create_document_bundle(
        db,
        doc_key="docs/private-k8s.md",
        title="사내 Kubernetes 문서",
        text="사내 전용 구성",
        scope="work",
        visibility="work_only",
    )
    db.add(
        KbDocumentEntity(
            passage_id=work_pas.id,
            entity_id=ent_hub.id,
            document_id=work_doc.id,
            revision_id=work_rev.id,
            evidence_id=work_ev.id,
        )
    )
    await db.flush()

    # 관계 및 증거 에지 생성
    rel1 = KbRelation(
        source_entity_id=ent_hub.id,
        target_entity_id=ent_node1.id,
        relation_type="manages",
        weight=1.0,
        evidence_count=1,
        status="active",
        visibility="global",
    )
    rel2 = KbRelation(
        source_entity_id=ent_node1.id,
        target_entity_id=ent_node2.id,
        relation_type="manages",
        weight=1.0,
        evidence_count=1,
        status="active",
        visibility="global",
    )
    db.add_all([rel1, rel2])
    await db.flush()

    db.add(
        KbRelationEvidence(
            relation_id=rel1.id,
            evidence_id=ev.id,
            confidence=1.0,
            relation="supports",
        )
    )
    db.add(
        KbRelationEvidence(
            relation_id=rel2.id,
            evidence_id=ev.id,
            confidence=1.0,
            relation="supports",
        )
    )
    await db.flush()

    # 정본 카드 합성
    await synthesize_canonical_for_entity(db, ent_hub.id)
    await synthesize_canonical_for_entity(db, ent_node1.id)

    # 1. 허브 쿼리 테스트 (limit=2, offset=0)
    hubs_res = await get_ontology_hubs(db, caller_visibility="global", offset=0, limit=2)
    assert len(hubs_res.nodes) == 2
    assert hubs_res.total == 3
    assert hubs_res.truncated is True
    assert {node.canonical_name for node in hubs_res.nodes} <= {
        "Kubernetes",
        "Deployment",
        "Pod",
    }
    assert hubs_res.nodes
    assert ent_orphan.id not in {uuid.UUID(node.id) for node in hubs_res.nodes}
    public_hubs = await get_ontology_hubs(db, offset=0, limit=10)
    public_hub = next(node for node in public_hubs.nodes if node.id == str(ent_hub.id))
    assert public_hub.document_count == 1
    assert public_hub.mention_count == 1
    work_hubs = await get_ontology_hubs(
        db, caller_visibility="work_only", offset=0, limit=10
    )
    work_hub = next(node for node in work_hubs.nodes if node.id == str(ent_hub.id))
    assert work_hub.document_count == 2
    assert work_hub.mention_count == 2

    with pytest.raises(ValueError, match="limit"):
        await get_ontology_hubs(db, limit=0)
    with pytest.raises(ValueError, match="caller_visibility"):
        await query_1hop_subgraph(db, ent_hub.id, caller_visibility="unknown")
    with pytest.raises(ValueError, match="limit"):
        await query_1hop_subgraph(db, ent_hub.id, limit=101)

    # 2. 1-hop 서브그래프 쿼리 (center=Kubernetes)
    subgraph = await query_1hop_subgraph(
        db,
        ent_hub.id,
        caller_visibility="global",
        offset=0,
        limit=10,
    )
    assert subgraph.center is not None
    assert subgraph.center.canonical_name == "Kubernetes"
    assert subgraph.center.is_center is True
    assert subgraph.total_neighbors == 1
    assert subgraph.truncated is False

    # 에지 검증: Kubernetes -> Deployment 에지만 포함 (Pod 는 2-hop 이므로 제외)
    assert len(subgraph.edges) == 1
    edge = subgraph.edges[0]
    assert edge.relation_type == "manages"
    assert edge.source_entity_id == str(ent_hub.id)
    assert edge.target_entity_id == str(ent_node1.id)

    # 원문 증거 인용 메타 검증
    assert len(edge.evidence) == 1
    ev_meta = edge.evidence[0]
    assert ev_meta.evidence_id == str(ev.id)
    assert ev_meta.document_title == "K8s 아키텍처"
    assert "Kubernetes는 Deployment를 관리하며" in (ev_meta.quote or "")

    # 3. 비인가 노드 차단: 개인 세션에서 work_only 노드를 중심으로 요청 시 존재 비노출
    secret_subgraph = await query_1hop_subgraph(
        db,
        ent_secret.id,
        caller_visibility="global",
    )
    assert secret_subgraph.center is None
    assert len(secret_subgraph.nodes) == 0


# -----------------------------------------------------------------------------
# 10. 파이프라인 CanonicalizingProcessor 통합 및 fenced_stage_write 검증
# -----------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_canonicalizing_processor_pipeline_integration(db: AsyncSession) -> None:
    """파이프라인 레지스트리와 CanonicalizingProcessor 의 실제 실행 검증."""
    registry = StageProcessorRegistry()
    proc = registry.get(STAGE_CANONICALIZING)
    assert isinstance(proc, CanonicalizingProcessor)

    # 테스트 데이터 준비
    ent = await create_entity(db, "PostgreSQL", summary="RDBMS")
    doc, rev1, _, ev1 = await create_document_bundle(
        db,
        doc_key="docs/pg.md",
        title="PostgreSQL",
        text="PostgreSQL 15 released.",
        rev_no=1,
    )
    await link_assertion(
        db,
        document_id=doc.id,
        revision_id=rev1.id,
        subject_entity_id=ent.id,
        predicate="version",
        object_value="15",
        evidence_id=ev1.id,
    )

    # 파이프라인 잡 행 생성
    job_id = uuid.uuid4()
    attempt_token = uuid.uuid4()
    job = KbPipelineJob(
        id=job_id,
        document_id=doc.id,
        revision_id=rev1.id,
        status="registered",
        current_stage=STAGE_CANONICALIZING,
        attempt_token=attempt_token,
    )
    db.add(job)
    await db.flush()

    # fenced_session 을 지원하는 세션 팩토리 제공
    engine = db.bind
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    await db.commit()

    ctx = StageContext(
        job_id=job.id,
        document_id=doc.id,
        revision_id=rev1.id,
        stage=STAGE_CANONICALIZING,
        attempt_token=attempt_token,
        session_factory=session_factory,
    )

    # 프로세서 실행
    result = await proc(ctx)
    assert result.status == STAGE_STATUS_SUCCEEDED
    assert result.detail["canonicals_updated"] >= 1

    # DB에 정본 카드 생성 확인
    card = await db.get(KbEntityCanonical, ent.id)
    assert card is not None
    assert card.version_no == 1
    assert any(f["value"] == "15" for f in card.key_facts)
