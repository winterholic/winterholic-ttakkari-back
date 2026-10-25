"""KB3-302의 PostgreSQL ORM 왕복과 불변 원장 제약 검증.

이 모듈은 tests/conftest.py가 제공하는 TTAKKARI_TEST_DB만 사용한다.
그 값은 본 테스트용 disposable pgvector 인스턴스로 지정해야 한다.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.db import sessionmaker
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource
from ttakkari.kb_ontology.canonicals import (
    get_active_assertions_for_entity,
    synthesize_canonical_for_entity,
)
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbEntity,
)


@pytest_asyncio.fixture
async def db() -> AsyncSession:
    async with sessionmaker()() as session:
        yield session


@pytest.mark.asyncio
async def test_canonical_postgresql_roundtrip_and_ledger_constraints(
    db: AsyncSession,
) -> None:
    body = "Valkey 기본 포트는 6379이다."
    body_sha = hashlib.sha256(body.encode()).hexdigest()
    source = KbSource(
        source_key="pg-test://kb3-302",
        kind="test",
        locator="pg-test://kb3-302",
        sha256=body_sha,
        byte_size=len(body.encode()),
        store_path=f"test/{body_sha}",
    )
    document = KbDocument(doc_key=f"kb3-302/{uuid.uuid4()}", title="Valkey", lang="ko")
    db.add_all([source, document])
    await db.flush()
    db.add(
        KbDocumentAccess(
            document_id=document.id,
            tenant_id="owner",
            scope="personal",
            origin_scope="personal",
            visibility="global",
            hidden=False,
        )
    )
    revision = KbRevision(
        document_id=document.id,
        source_id=source.id,
        rev_no=1,
        parser_version="test-v1",
        body=body,
        text_sha256=body_sha,
        char_count=len(body),
        valid_from=datetime.now(UTC),
    )
    db.add(revision)
    await db.flush()
    passage = KbPassage(
        revision_id=revision.id,
        ord=1,
        body=body,
        char_start=0,
        char_end=len(body),
        sha256=body_sha,
        token_estimate=len(body.split()),
    )
    db.add(passage)
    await db.flush()
    evidence = KbEvidence(
        passage_id=passage.id,
        span_start=0,
        span_end=len(body),
        sha256=body_sha,
    )
    entity = KbEntity(
        tenant_id="personal",
        origin_scope="personal",
        visibility="global",
        canonical_name="Valkey",
        norm_name="valkey",
        entity_kind="technology",
        domain="infra",
        status="active",
    )
    db.add_all([evidence, entity])
    await db.flush()
    assertion = KbAssertion(
        document_id=document.id,
        revision_id=revision.id,
        origin_scope="personal",
        visibility="global",
        subject_entity_id=entity.id,
        predicate="port",
        object_value="6379",
        norm_object_value="6379",
        modality="fact",
        polarity="positive",
        status="active",
        trust_score=0.9,
    )
    db.add(assertion)
    await db.flush()
    db.add(
        KbAssertionEvidence(
            assertion_id=assertion.id,
            evidence_id=evidence.id,
            relation="supports",
        )
    )
    await db.flush()

    eligible = await get_active_assertions_for_entity(db, entity)
    assert [(claim.id, citation_id) for claim, citation_id in eligible] == [
        (assertion.id, evidence.id)
    ]
    canonical = await synthesize_canonical_for_entity(db, entity.id)
    assert canonical is not None
    assert canonical.key_facts[0]["evidence_id"] == str(evidence.id)
    assert canonical.key_facts[0]["document_id"] == str(document.id)
    assert canonical.version_no == 1

    # PostgreSQL check constraint 및 DB trigger가 ORM 경로에서도 원장을 거부한다.
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(
                KbRevision(
                    document_id=document.id,
                    source_id=source.id,
                    rev_no=0,
                    parser_version="invalid",
                    body=body,
                    text_sha256=body_sha,
                    char_count=len(body),
                )
            )
            await db.flush()
    with pytest.raises(IntegrityError):
        async with db.begin_nested():
            db.add(
                KbPassage(
                    revision_id=revision.id,
                    ord=2,
                    body=body,
                    char_start=0,
                    char_end=len(body),
                    sha256="0" * 64,
                )
            )
            await db.flush()
