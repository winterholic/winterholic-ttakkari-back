"""KB v3 온톨로지: 한·영 시드 별칭 사전과 결정적 엔터티 해소기 검증 (KB3-301 교정판).

판정 기준: 이름·철자·임베딩 유사도만으로 동일성을 확정하지 않는다. 비슷하면 병합 후보로 남긴다.
한국어 표기로 영어 지식(엔터티)을 찾을 수 있어야 한다.

영역:
1. 시드 사전 무결성(내부 중복·동음이의), 멱등 시딩
2. Step 1 정규화 키 일치와 kind·도메인 호환
3. Step 2 한·영 별칭, 같은 패시지 괄호 병기 학습 조건
4. 반례: React/React Native, PostgreSQL/PostGIS, Java/JavaScript, Spark/Spark AR, Swift/SWIFT 자동 병합 금지
5. Step 3 유사도는 후보만 생성, 운영 E5 경로 어댑터 연결
6. 멱등성: 같은 패시지 재처리 시 카운트·링크·별칭 불변
7. visibility 파생과 work_only 오염 방지
8. 실제 PostgreSQL 왕복(KB3_PG_URL 지정 시)과 DDL 컴파일
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_ontology import (
    KNOWN_HOMONYMS,
    EntityResolutionStep,
    bootstrap_seed_aliases,
    derive_scope,
    find_seed_by_norm,
    find_seeds_by_norm,
    get_seed_entities,
    is_seed_alias,
    kb_embed_fn,
    resolve_entity,
    validate_seed_inventory,
)
from ttakkari.kb_ontology_models import (
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
)
from ttakkari.models import EMBED_DIM

ONTOLOGY_TABLES = [
    KbEntity.__table__,
    KbEntityAlias.__table__,
    KbDocumentEntity.__table__,
    KbConflict.__table__,
    KbDocumentAccess.__table__,
]


# 상위 conftest.py 의 PostgreSQL 세션 자동 픽스처를 오버라이드해 SQLite in-memory 로 독립 격리 테스트를 한다.
# 실제 PostgreSQL 왕복은 KB3_PG_URL 로 따로 확인한다(하단 test_postgres_roundtrip_*).
@pytest.fixture(scope="session", autouse=True)
def _schema_and_app() -> None:
    yield


@pytest.fixture(autouse=True)
def _clean() -> None:
    yield


@pytest_asyncio.fixture
async def async_db() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        for t in ONTOLOGY_TABLES:
            await conn.run_sync(t.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


class Doc:
    """문서·리비전·패시지 id 묶음. 접근 행(kb_document_access)을 함께 만든다."""

    def __init__(self, scope: str | None = "personal", **access_kwargs: object) -> None:
        self.document_id = uuid.uuid4()
        self.revision_id = uuid.uuid4()
        self.passage_id = uuid.uuid4()
        self.evidence_id = uuid.uuid4()
        self.scope = scope
        self.access_kwargs = access_kwargs

    async def register(self, db: AsyncSession) -> Doc:
        if self.scope is not None:
            db.add(KbDocumentAccess(document_id=self.document_id, scope=self.scope, **self.access_kwargs))
            await db.flush()
        return self

    def kw(self, passage: uuid.UUID | None = None) -> dict[str, object]:
        return {
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "passage_id": passage or self.passage_id,
            "evidence_id": self.evidence_id,
        }


async def _aliases(db: AsyncSession, entity_id: uuid.UUID) -> set[str]:
    rows = await db.execute(select(KbEntityAlias.norm_alias).where(KbEntityAlias.entity_id == entity_id))
    return {r[0] for r in rows.all()}


async def _conflicts(db: AsyncSession) -> list[KbConflict]:
    return list((await db.execute(select(KbConflict))).scalars().all())


def _pair(c: KbConflict) -> frozenset[uuid.UUID]:
    return frozenset({c.entity_id, c.candidate_entity_id})


# ---------------------------------------------------------------------
# 1. 시드 사전
# ---------------------------------------------------------------------
def test_seed_inventory_has_no_internal_duplicates_or_shared_norms() -> None:
    assert validate_seed_inventory() == []
    seeds = get_seed_entities()
    assert len(seeds) >= 150
    names = {s.canonical_name for s in seeds}
    assert {"Kubernetes", "PostgreSQL", "Python", "Go", "Swift", "Large Language Model", "OAuth"} <= names


def test_seed_review_removed_wrong_equivalences() -> None:
    """동일 대상이 아닌 상위 개념·유사 개념·짧은 약어가 별칭으로 남아 있지 않다."""
    for wrong in ("컨테이너오케스트레이션", "gpt", "torch", "tf", "es", "ssl", "quic", "서비스메시", "노드", "바이트", "오스"):
        assert not is_seed_alias(wrong), wrong
    iac = find_seed_by_norm("iac")  # 'IaC' 는 Terraform 별칭이 아니라 개념 시드의 별칭이다
    assert iac is not None and (iac.canonical_name, iac.entity_kind) == ("Infrastructure as Code", "concept")
    assert find_seed_by_norm("terraform").canonical_name == "Terraform"  # type: ignore[union-attr]


def test_seed_bilingual_lookup_and_homonym_registry() -> None:
    assert find_seed_by_norm("k8s").canonical_name == "Kubernetes"  # type: ignore[union-attr]
    assert find_seed_by_norm("쿠버네티스").canonical_name == "Kubernetes"  # type: ignore[union-attr]
    assert find_seed_by_norm("postgres").canonical_name == "PostgreSQL"  # type: ignore[union-attr]
    assert is_seed_alias("파이썬") and not is_seed_alias("알수없는신조어1234")
    # 동음이의 표제어가 시드에 있으면 모두 도메인 구분 사전에 등록되어 있어야 한다.
    for norm in ("swift", "ruby", "pulsar", "spark", "flux", "helm", "loki", "secret", "job", "transformer", "gemini"):
        assert norm in KNOWN_HOMONYMS and find_seeds_by_norm(norm), norm


@pytest.mark.asyncio
async def test_bootstrap_seed_aliases_idempotent(async_db: AsyncSession) -> None:
    inserted_1 = await bootstrap_seed_aliases(async_db)
    assert inserted_1 >= 150
    k8s = (await async_db.execute(select(KbEntity).where(KbEntity.norm_name == "kubernetes"))).scalar_one()
    assert k8s.visibility == "global" and k8s.domain == "infra"
    assert {"쿠버네티스", "k8s"} <= await _aliases(async_db, k8s.id)

    counts = [
        (await async_db.execute(select(func.count()).select_from(t))).scalar_one()
        for t in (KbEntity, KbEntityAlias)
    ]
    assert await bootstrap_seed_aliases(async_db) == 0
    assert counts == [
        (await async_db.execute(select(func.count()).select_from(t))).scalar_one() for t in (KbEntity, KbEntityAlias)
    ]


@pytest.mark.asyncio
async def test_bootstrap_enriches_preexisting_same_slot_entity_without_duplicating(async_db: AsyncSession) -> None:
    """먼저 만들어진 같은 (kind, norm) 엔터티에는 시드 별칭만 보충하고 새 엔터티를 만들지 않는다."""
    doc = await Doc().register(async_db)
    first = await resolve_entity(async_db, "Redis", entity_kind="technology", domain="infra", **doc.kw())
    await bootstrap_seed_aliases(async_db)
    redis_rows = (await async_db.execute(select(KbEntity).where(KbEntity.norm_name == "redis"))).scalars().all()
    assert [e.id for e in redis_rows] == [first.entity.id]
    assert "레디스" in await _aliases(async_db, first.entity.id)


# ---------------------------------------------------------------------
# 2. Step 1: 정규화 키 일치, kind·도메인 호환
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step1_exact_norm_name_resolution(async_db: AsyncSession) -> None:
    first = await resolve_entity(
        async_db, "AcmeCloudEngine", entity_kind="technology", domain="infra", source_access="personal"
    )
    assert first.created and first.step == EntityResolutionStep.NEW_ENTITY
    for var in ("acmecloudengine", "Acme-Cloud-Engine", "ACME_CLOUD_ENGINE", " acme cloud engine "):
        res = await resolve_entity(
            async_db, var, entity_kind="technology", domain="infra", source_access="personal"
        )
        assert (res.created, res.step, res.entity.id) == (False, EntityResolutionStep.EXACT_NORM_NAME, first.entity.id)


@pytest.mark.asyncio
async def test_same_name_different_kind_is_candidate_not_merge(async_db: AsyncSession) -> None:
    tech = await resolve_entity(async_db, "Mercury", entity_kind="technology", domain="dev", source_access="personal")
    concept = await resolve_entity(async_db, "Mercury", entity_kind="concept", domain="dev", source_access="personal")
    assert concept.entity.id != tech.entity.id
    assert concept.step == EntityResolutionStep.AMBIGUOUS_ISOLATED
    assert [_pair(c) for c in concept.conflicts] == [frozenset({tech.entity.id, concept.entity.id})]
    assert concept.conflict.conflict_kind == "ambiguous_entity" and concept.conflict.status == "open"  # type: ignore[union-attr]
    # 재처리: 같은 kind 로 다시 부르면 각자 자기 엔터티에 붙고 후보는 늘지 않는다
    again_t = await resolve_entity(async_db, "mercury", entity_kind="technology", domain="dev", source_access="personal")
    again_c = await resolve_entity(async_db, "mercury", entity_kind="concept", domain="dev", source_access="personal")
    assert (again_t.entity.id, again_c.entity.id) == (tech.entity.id, concept.entity.id)
    assert len(await _conflicts(async_db)) == 1


@pytest.mark.asyncio
async def test_seed_kind_mismatch_is_candidate_not_merge(async_db: AsyncSession) -> None:
    """시드는 Kubernetes=technology. concept 으로 들어온 'Kubernetes' 는 자동 병합하지 않는다."""
    res = await resolve_entity(async_db, "Kubernetes", entity_kind="concept", domain="infra", source_access="personal")
    assert res.created and res.step == EntityResolutionStep.AMBIGUOUS_ISOLATED
    seed_entity = (
        await async_db.execute(select(KbEntity).where(KbEntity.entity_kind == "technology", KbEntity.norm_name == "kubernetes"))
    ).scalar_one()
    assert res.entity.id != seed_entity.id
    assert [_pair(c) for c in res.conflicts] == [frozenset({seed_entity.id, res.entity.id})]


@pytest.mark.asyncio
async def test_homonym_same_kind_gets_separate_entity_and_is_idempotent(async_db: AsyncSession) -> None:
    """kind 가 같아도 동음이의 도메인이면 병합하지 않는다(유일 키 충돌 없이 구분 키로 분리)."""
    dev = await resolve_entity(async_db, "Python", entity_kind="technology", domain="dev", source_access="personal")
    snake = await resolve_entity(async_db, "Python", entity_kind="technology", domain="science", source_access="personal")
    assert snake.created and snake.step == EntityResolutionStep.AMBIGUOUS_ISOLATED
    assert snake.entity.id != dev.entity.id and snake.entity.norm_name == "python~science"
    assert [_pair(c) for c in snake.conflicts] == [frozenset({dev.entity.id, snake.entity.id})]
    again = await resolve_entity(async_db, "Python", entity_kind="technology", domain="science", source_access="personal")
    assert (again.created, again.entity.id) == (False, snake.entity.id)
    assert (await resolve_entity(async_db, "파이썬", entity_kind="technology", domain="dev", source_access="personal")).entity.id == dev.entity.id
    assert len(await _conflicts(async_db)) == 1

    go_dev = await resolve_entity(async_db, "Go", entity_kind="technology", domain="dev", source_access="personal")
    go_verb = await resolve_entity(async_db, "Go", entity_kind="concept", domain="general", source_access="personal")
    assert go_verb.entity.id != go_dev.entity.id and go_verb.step == EntityResolutionStep.AMBIGUOUS_ISOLATED


# ---------------------------------------------------------------------
# 3. Step 2: 한·영 별칭, 괄호 병기 학습 조건
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_korean_query_finds_english_entity_via_seed_and_learned_alias(async_db: AsyncSession) -> None:
    """핵심 요구: 한국어 표기로 영어 엔터티를 찾는다."""
    doc = await Doc().register(async_db)
    en = await resolve_entity(async_db, "Kubernetes", entity_kind="technology", domain="infra", **doc.kw())
    for ko in ("쿠버네티스", "k8s", "K8S"):
        res = await resolve_entity(async_db, ko, entity_kind="technology", domain="infra", source_access="personal")
        assert res.entity.id == en.entity.id and res.created is False
        assert res.step == EntityResolutionStep.ALIAS_OR_PARENTHETICAL
    # 시드에 없는 용어: 같은 패시지 괄호 병기로 배운 뒤에는 한국어 단독 표기로 찾을 수 있다
    learned = await resolve_entity(
        async_db, "롤아웃도구(Rolloutizer)", entity_kind="concept", domain="infra", **doc.kw()
    )
    assert learned.entity.canonical_name == "Rolloutizer"
    assert "롤아웃도구" in learned.learned_aliases
    found = await resolve_entity(async_db, "롤아웃도구", entity_kind="concept", domain="infra", source_access="personal")
    assert found.entity.id == learned.entity.id and found.step != EntityResolutionStep.NEW_ENTITY


@pytest.mark.asyncio
async def test_parenthetical_alias_requires_passage_anchor(async_db: AsyncSession) -> None:
    anchored = await resolve_entity(
        async_db, "프리즘엔진(PrismEngine)", entity_kind="technology", domain="dev", **(await Doc().register(async_db)).kw()
    )
    assert "프리즘엔진" in anchored.learned_aliases
    unanchored = await resolve_entity(
        async_db, "넥서스코어(NexusCore)", entity_kind="technology", domain="dev", source_access="personal"
    )
    assert unanchored.learned_aliases == [] and "넥서스코어" in unanchored.rejected_aliases
    assert await _aliases(async_db, unanchored.entity.id) == {"nexuscore"}


@pytest.mark.asyncio
async def test_extra_aliases_must_appear_in_passage_text(async_db: AsyncSession) -> None:
    doc = await Doc().register(async_db)
    res = await resolve_entity(
        async_db,
        "QuillDB",
        aliases=["퀼디비", "Project-Secret-DB"],
        passage_text="QuillDB(퀼디비)는 문서형 저장소다.",
        entity_kind="technology",
        domain="dev",
        **doc.kw(),
    )
    assert res.learned_aliases == ["퀼디비"]
    assert res.rejected_aliases == ["Project-Secret-DB"]
    # passage_text 가 없으면 호출자가 준 별칭은 하나도 배우지 않는다
    res2 = await resolve_entity(
        async_db, "QuillDB", aliases=["퀼"], entity_kind="technology", domain="dev", **doc.kw(uuid.uuid4())
    )
    assert res2.learned_aliases == [] and "퀼" in res2.rejected_aliases


@pytest.mark.asyncio
async def test_qualifier_in_parentheses_and_foreign_alias_are_not_learned(async_db: AsyncSession) -> None:
    doc = await Doc().register(async_db)
    py = await resolve_entity(async_db, "Python (programming language)", entity_kind="technology", domain="dev", **doc.kw())
    assert "programminglanguage" not in await _aliases(async_db, py.entity.id)
    # 다른 엔터티가 이미 쓰는 표기·다른 시드의 이름은 배우지 않고 병합 후보로 남긴다
    other = await resolve_entity(async_db, "Zephyrus", entity_kind="technology", domain="dev", source_access="personal")
    res = await resolve_entity(
        async_db, "Zephyrus (JavaScript)", entity_kind="technology", domain="dev", **doc.kw(uuid.uuid4())
    )
    assert res.entity.id == other.entity.id and res.learned_aliases == []
    assert "JavaScript" in res.rejected_aliases


# ---------------------------------------------------------------------
# 4. 반례: 철자·접두 유사 쌍은 자동 병합하지 않는다
# ---------------------------------------------------------------------
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("a", "b", "kind_a", "dom_a", "kind_b", "dom_b"),
    [
        ("React", "React Native", "technology", "dev", "technology", "dev"),
        ("PostgreSQL", "PostGIS", "technology", "dev", "technology", "dev"),
        ("Java", "JavaScript", "technology", "dev", "technology", "dev"),
        ("Spark", "Spark AR", "technology", "dev", "technology", "dev"),
        ("Swift", "SWIFT", "technology", "dev", "system", "business"),
    ],
)
async def test_counterexamples_never_auto_merge(
    async_db: AsyncSession, a: str, b: str, kind_a: str, dom_a: str, kind_b: str, dom_b: str
) -> None:
    await bootstrap_seed_aliases(async_db)
    doc = await Doc().register(async_db)
    first = await resolve_entity(async_db, a, entity_kind=kind_a, domain=dom_a, **doc.kw())
    second = await resolve_entity(async_db, b, entity_kind=kind_b, domain=dom_b, **doc.kw(uuid.uuid4()))
    assert first.entity.id != second.entity.id
    assert second.step not in (EntityResolutionStep.EXACT_NORM_NAME, EntityResolutionStep.ALIAS_OR_PARENTHETICAL) or (
        b == "JavaScript"  # 시드끼리는 각자 자기 시드 엔터티로 해소된다
    )
    from ttakkari.common.entity_norm import norm_name

    assert norm_name(b) not in await _aliases(async_db, first.entity.id) or norm_name(a) == norm_name(b)
    assert norm_name(a) not in await _aliases(async_db, second.entity.id) or norm_name(a) == norm_name(b)
    # 철자 유사 후보는 자동 병합 대신 후보로 남는다(둘 다 시드이면 각자 확정된 별개 엔터티)
    if b != "JavaScript":
        assert any(first.entity.id in _pair(c) and second.entity.id in _pair(c) for c in await _conflicts(async_db))


# ---------------------------------------------------------------------
# 5. Step 3: 유사도는 후보만 만든다
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_spelling_and_embedding_similarity_only_creates_candidate(async_db: AsyncSession) -> None:
    orig = await resolve_entity(async_db, "Elasticsearch", entity_kind="technology", domain="dev", source_access="personal")
    doc = await Doc().register(async_db)
    res = await resolve_entity(
        async_db,
        "Elastiksearch",
        entity_kind="technology",
        domain="dev",
        embedding=[0.105] * 8,
        candidate_embeddings={orig.entity.id: [0.1] * 8},  # 코사인 ~1.0
        **doc.kw(),
    )
    assert res.created and res.step == EntityResolutionStep.SIMILARITY_CANDIDATE
    assert res.entity.id != orig.entity.id and res.learned_aliases == []
    assert "elastiksearch" not in await _aliases(async_db, orig.entity.id)
    assert [_pair(c) for c in res.conflicts] == [frozenset({orig.entity.id, res.entity.id})]
    assert res.conflict.conflict_kind == "ambiguous_entity"  # type: ignore[union-attr]
    # 재처리해도 같은 엔터티·같은 후보 한 건
    again = await resolve_entity(
        async_db, "Elastiksearch", entity_kind="technology", domain="dev", **doc.kw()
    )
    assert (again.created, again.entity.id) == (False, res.entity.id)
    assert len(await _conflicts(async_db)) == 1


@pytest.mark.asyncio
async def test_embedding_only_candidate_and_below_threshold(async_db: AsyncSession) -> None:
    orig = await resolve_entity(async_db, "Microservices", entity_kind="concept", domain="dev", source_access="personal")
    near = await resolve_entity(
        async_db, "분산 서비스 설계", entity_kind="concept", domain="dev", source_access="personal",
        embedding=[1.0, 0.1, 0.0, 0.0], candidate_embeddings={orig.entity.id: [1.0, 0.0, 0.0, 0.0]},  # cos≈0.995
    )
    assert near.step == EntityResolutionStep.SIMILARITY_CANDIDATE
    far = await resolve_entity(
        async_db, "전혀 다른 개념", entity_kind="concept", domain="dev", source_access="personal",
        embedding=[0.82, 0.57, 0.0, 0.0], candidate_embeddings={orig.entity.id: [1.0, 0.0, 0.0, 0.0]},  # cos≈0.82
    )
    assert far.step == EntityResolutionStep.NEW_ENTITY and far.conflicts == []


@pytest.mark.asyncio
async def test_production_embedding_adapter_is_wired_to_kb_retrieval(async_db: AsyncSession) -> None:
    """kb_embed_fn 은 운영 KB 검색 임베딩(kb_retrieval.embed_complete)을 그대로 쓴다.

    테스트 환경의 TTAKKARI_CONTEXT_EMBEDDER=hash 에서는 같은 경로가 해시 임베더로 동작한다(운영은 E5).
    """
    vectors = await kb_embed_fn(["Elasticsearch", "Elastiksearch"])
    assert len(vectors) == 2 and all(len(v) == EMBED_DIM for v in vectors)

    calls: list[list[str]] = []

    async def spy(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        return await kb_embed_fn(texts)

    orig = await resolve_entity(async_db, "Elasticsearch", entity_kind="technology", domain="dev", source_access="personal")
    res = await resolve_entity(
        async_db, "Elastiksearch", entity_kind="technology", domain="dev", source_access="personal", embed_fn=spy
    )
    assert len(calls) == 1 and calls[0][0].startswith("Elastiksearch")
    assert res.step == EntityResolutionStep.SIMILARITY_CANDIDATE and res.entity.id != orig.entity.id

    async def broken(_texts: list[str]) -> list[list[float]]:
        raise RuntimeError("model unavailable")

    res2 = await resolve_entity(
        async_db, "Elastiksearh", entity_kind="technology", domain="dev", source_access="personal", embed_fn=broken
    )
    assert res2.entity.id != orig.entity.id and "embedding unavailable" in res2.note


# ---------------------------------------------------------------------
# 6. 멱등성
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reprocessing_same_passage_keeps_counts_links_and_aliases(async_db: AsyncSession) -> None:
    doc = await Doc().register(async_db)
    p2 = uuid.uuid4()

    async def run() -> list[uuid.UUID]:
        ids = []
        for passage in (doc.passage_id, p2):
            r = await resolve_entity(
                async_db, "쿠버네티스(Kubernetes)", entity_kind="technology", domain="infra", **doc.kw(passage)
            )
            ids.append(r.entity.id)
        return ids

    ids1 = await run()
    ent = (await async_db.execute(select(KbEntity).where(KbEntity.id == ids1[0]))).scalar_one()
    snapshot = lambda: (  # noqa: E731
        ent.mention_count, ent.document_count,
        None,
    )
    assert (ent.mention_count, ent.document_count) == (2, 1)
    links1 = (await async_db.execute(select(func.count()).select_from(KbDocumentEntity))).scalar_one()
    aliases1 = await _aliases(async_db, ent.id)
    conflicts1 = len(await _conflicts(async_db))
    for _ in range(3):
        assert await run() == ids1
    await async_db.refresh(ent)
    assert (ent.mention_count, ent.document_count) == (2, 1)
    assert (await async_db.execute(select(func.count()).select_from(KbDocumentEntity))).scalar_one() == links1 == 2
    assert await _aliases(async_db, ent.id) == aliases1
    assert len(await _conflicts(async_db)) == conflicts1
    assert snapshot()[0] == 2


@pytest.mark.asyncio
async def test_mention_count_is_unique_entity_passage_rows_across_documents(async_db: AsyncSession) -> None:
    d1, d2 = await Doc().register(async_db), await Doc().register(async_db)
    for d in (d1, d2, d1, d2):
        res = await resolve_entity(async_db, "RabbitMQ", entity_kind="technology", domain="infra", **d.kw())
    assert (res.entity.mention_count, res.entity.document_count) == (2, 2)


# ---------------------------------------------------------------------
# 7. visibility
# ---------------------------------------------------------------------
def test_derive_scope_policy() -> None:
    work = KbDocumentAccess(document_id=uuid.uuid4(), scope="work")
    personal_default = KbDocumentAccess(document_id=uuid.uuid4(), scope="personal")  # visibility 누락 → 스키마 기본 work_only
    assert personal_default.visibility == "work_only"
    assert derive_scope([work]).visibility == "work_only" and derive_scope([work]).origin_scope == "work"
    assert derive_scope([personal_default]).visibility == "global"  # 정책: personal→global(누락 시에도)
    assert derive_scope([personal_default, work]).visibility == "work_only"
    assert derive_scope([]).visibility == "work_only"
    assert derive_scope([None]).visibility == "work_only"
    assert derive_scope(["personal"]).visibility == "global" and derive_scope(["work"]).visibility == "work_only"
    assert derive_scope(["mystery"]).visibility == "work_only"


@pytest.mark.asyncio
async def test_visibility_is_derived_from_document_access_never_defaulted_global(async_db: AsyncSession) -> None:
    work_doc = await Doc("work").register(async_db)
    personal_doc = await Doc("personal").register(async_db)
    no_access_doc = await Doc(None).register(async_db)  # 문서는 있으나 접근 행 없음
    cases = [
        (work_doc.kw(), "work_only", "work"),
        (personal_doc.kw(), "global", "personal"),
        (no_access_doc.kw(), "work_only", "personal"),
        ({}, "work_only", "personal"),  # 문서 정보 자체가 없음
    ]
    for i, (kw, vis, scope) in enumerate(cases):
        res = await resolve_entity(async_db, f"Isolated{i}Thing", entity_kind="technology", domain="dev", **kw)
        assert (res.entity.visibility, res.entity.origin_scope) == (vis, scope), i


@pytest.mark.asyncio
async def test_work_mention_does_not_pollute_global_entity(async_db: AsyncSession) -> None:
    pdoc = await Doc("personal").register(async_db)
    wdoc = await Doc("work").register(async_db)
    g = await resolve_entity(async_db, "PostgreSQL", entity_kind="technology", domain="dev", **pdoc.kw())
    assert g.entity.visibility == "global" and g.entity.mention_count == 1
    aliases_before = await _aliases(async_db, g.entity.id)

    w = await resolve_entity(
        async_db,
        "PostgreSQL (Project-Secret-DB)",
        aliases=["사내코드명"],
        passage_text="PostgreSQL (Project-Secret-DB) 사내코드명",
        entity_kind="technology",
        domain="dev",
        **wdoc.kw(),
    )
    assert w.entity.id == g.entity.id
    assert w.learned_aliases == [] and {"Project-Secret-DB", "사내코드명"} <= set(w.rejected_aliases)
    assert await _aliases(async_db, g.entity.id) == aliases_before
    await async_db.refresh(g.entity)
    assert (g.entity.mention_count, g.entity.document_count) == (1, 1)
    # 문서 연결 자체는 남는다(문서 접근 통제는 문서 쪽 책임)
    assert (await async_db.execute(select(func.count()).select_from(KbDocumentEntity))).scalar_one() == 2

    # 두 번째 personal 문서는 센다. work 문서는 몇 번 다시 처리해도 global 카운트에 들어가지 않는다
    p2 = await Doc("personal").register(async_db)
    await resolve_entity(async_db, "PostgreSQL", entity_kind="technology", domain="dev", **p2.kw())
    for _ in range(2):
        await resolve_entity(async_db, "PostgreSQL", entity_kind="technology", domain="dev", **wdoc.kw())
    await async_db.refresh(g.entity)
    assert (g.entity.mention_count, g.entity.document_count) == (2, 2)


@pytest.mark.asyncio
async def test_work_only_entity_is_invisible_to_global_context_and_learns_its_own_aliases(async_db: AsyncSession) -> None:
    wdoc = await Doc("work").register(async_db)
    internal = await resolve_entity(
        async_db, "하이퍼코어(HyperCore)", entity_kind="system", domain="dev", **wdoc.kw()
    )
    assert internal.entity.visibility == "work_only" and "하이퍼코어" in internal.learned_aliases
    pdoc = await Doc("personal").register(async_db)
    outside = await resolve_entity(async_db, "HyperCore", entity_kind="system", domain="dev", **pdoc.kw())
    assert outside.created and outside.entity.id != internal.entity.id and outside.entity.visibility == "global"
    assert outside.conflicts == []  # global 컨텍스트는 work_only 엔터티를 후보로도 보지 않는다
    # work 컨텍스트에서는 work_only 엔터티를 우선 선택한다
    again = await resolve_entity(async_db, "HyperCore", entity_kind="system", domain="dev", **wdoc.kw(uuid.uuid4()))
    assert again.entity.id == internal.entity.id


@pytest.mark.asyncio
async def test_work_context_homonym_conflict_is_work_only(async_db: AsyncSession) -> None:
    await bootstrap_seed_aliases(async_db)
    wdoc = await Doc("work").register(async_db)
    res = await resolve_entity(async_db, "Swift", entity_kind="system", domain="business", **wdoc.kw())
    assert res.step == EntityResolutionStep.AMBIGUOUS_ISOLATED
    assert res.entity.visibility == "work_only" and {c.visibility for c in res.conflicts} == {"work_only"}


# ---------------------------------------------------------------------
# 8. PostgreSQL
# ---------------------------------------------------------------------
def test_postgresql_ddl_compilation() -> None:
    dialect = postgresql.dialect()
    for table in ONTOLOGY_TABLES:
        sql = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in sql and table.name in sql


PG_URL = os.environ.get("KB3_PG_URL")


@pytest_asyncio.fixture
async def pg_db() -> AsyncGenerator[AsyncSession]:
    """pgvector/pgvector:pg17 컨테이너 등 실제 PostgreSQL(KB3_PG_URL=postgresql+asyncpg://...)에 kb_* 테이블을 만든다."""
    if not PG_URL:
        pytest.skip("KB3_PG_URL 미지정: 실제 PostgreSQL 왕복은 Docker 컨테이너로 따로 실행")
    import ttakkari.collector.control_models  # noqa: F401
    import ttakkari.kb_models  # noqa: F401
    import ttakkari.kb_ontology_models  # noqa: F401
    import ttakkari.kb_pipeline_models  # noqa: F401
    import ttakkari.kb_vector_models  # noqa: F401
    from ttakkari.models import Base

    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.run_sync(
            lambda c: Base.metadata.create_all(c, tables=[t for t in Base.metadata.sorted_tables if t.name.startswith("kb_")])
        )
    async with async_sessionmaker(engine, expire_on_commit=False)() as session:
        yield session
    await engine.dispose()


async def _pg_make_passage(db: AsyncSession, scope: str) -> Doc:
    """FK 를 만족하는 실제 문서·리비전·패시지·근거·접근 행을 만든다."""
    import hashlib

    from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision, KbSource

    n = uuid.uuid4().hex
    body = f"Kubernetes 쿠버네티스 {n}"
    digest = hashlib.sha256(body.encode()).hexdigest()
    source = KbSource(source_key=f"s-{n}", sha256=digest, byte_size=len(body), store_path="x")
    doc = KbDocument(doc_key=f"d-{n}", title="t")
    db.add_all([source, doc])
    await db.flush()
    rev = KbRevision(
        document_id=doc.id, source_id=source.id, rev_no=1, body=body, text_sha256=digest, char_count=len(body)
    )
    db.add(rev)
    await db.flush()
    passage = KbPassage(
        revision_id=rev.id, ord=0, body=body, char_start=0, char_end=len(body), sha256=digest, search_text=body
    )
    db.add(passage)
    await db.flush()
    evidence = KbEvidence(passage_id=passage.id, span_start=0, span_end=len(body), sha256=digest)
    db.add_all([evidence, KbDocumentAccess(document_id=doc.id, scope=scope)])
    await db.flush()
    d = Doc(None)
    d.document_id, d.revision_id, d.passage_id, d.evidence_id = doc.id, rev.id, passage.id, evidence.id
    return d


@pytest.mark.asyncio
async def test_postgres_roundtrip_resolution_idempotency_and_visibility(pg_db: AsyncSession) -> None:
    """실제 PostgreSQL: 삽입·해소·재실행 멱등·work_only 비오염·유일 키·CHECK 제약."""
    await bootstrap_seed_aliases(pg_db)
    assert await bootstrap_seed_aliases(pg_db) == 0
    personal = await _pg_make_passage(pg_db, "personal")
    work = await _pg_make_passage(pg_db, "work")

    async def run() -> list[uuid.UUID]:
        out = []
        for d, name in ((personal, "쿠버네티스"), (work, "Kubernetes (사내클러스터)"), (personal, "Kubernetes")):
            r = await resolve_entity(pg_db, name, entity_kind="technology", domain="infra", **d.kw())
            out.append(r.entity.id)
        return out

    first = await run()
    assert len(set(first)) == 1  # 한국어·영어 표기 모두 같은 시드 엔터티
    ent = await pg_db.get(KbEntity, first[0])
    assert ent is not None and ent.visibility == "global"
    await pg_db.refresh(ent)
    assert (ent.mention_count, ent.document_count) == (1, 1)  # work 문서 연결은 global 카운트에 없음
    assert "사내클러스터" not in {a.alias for a in (await pg_db.execute(select(KbEntityAlias).where(KbEntityAlias.entity_id == ent.id))).scalars()}
    links = (await pg_db.execute(select(func.count()).select_from(KbDocumentEntity))).scalar_one()
    aliases = (await pg_db.execute(select(func.count()).select_from(KbEntityAlias))).scalar_one()
    for _ in range(2):
        assert await run() == first
    await pg_db.refresh(ent)
    assert (ent.mention_count, ent.document_count) == (1, 1)
    assert (await pg_db.execute(select(func.count()).select_from(KbDocumentEntity))).scalar_one() == links
    assert (await pg_db.execute(select(func.count()).select_from(KbEntityAlias))).scalar_one() == aliases

    # work 문서에서만 나온 엔터티는 work_only, 동음이의(같은 kind)는 구분 키로 분리
    internal = await resolve_entity(pg_db, "HyperCore", entity_kind="system", domain="dev", **work.kw())
    assert (internal.entity.visibility, internal.entity.origin_scope) == ("work_only", "work")
    py_dev = await resolve_entity(pg_db, "Python", entity_kind="technology", domain="dev", **personal.kw())
    py_snake = await resolve_entity(pg_db, "Python", entity_kind="technology", domain="science", **personal.kw())
    assert py_snake.entity.id != py_dev.entity.id and py_snake.conflict is not None
    await pg_db.commit()

    # DB 제약: work 출처는 global 불가, 값 도메인, 유일 키, 카운트 음수
    async def expect_violation(**kw: object) -> None:
        base = {"canonical_name": "X", "norm_name": f"x{uuid.uuid4().hex[:6]}", "entity_kind": "concept"}
        with pytest.raises(IntegrityError):
            async with pg_db.begin_nested():
                pg_db.add(KbEntity(**{**base, **kw}))
                await pg_db.flush()

    await expect_violation(origin_scope="work", visibility="global")
    await expect_violation(visibility="public")
    await expect_violation(mention_count=-1)
    await expect_violation(entity_kind="technology", norm_name="kubernetes", visibility="global", tenant_id="personal")
