"""KB v3 온톨로지 한·영 시드 별칭 사전 및 4단계 결정적 엔터티 해소기 검증 테스트.

테스트 영역:
1. 시드 별칭 사전(170개) 목록, 한·영·약어 매핑 및 멱등 시딩 검증
2. 1단계 (Step 1): 정규화 키(norm_name) 정확 일치
3. 2단계 (Step 2): 별칭 및 괄호 병기(Kubernetes/쿠버네티스/k8s) 단일 엔터티 통합
4. 3단계 (Step 3): 임베딩 & 이름 유사도 통과 시 동일 엔터티 연결
5. 4단계 (Step 4): 경계 유사도 구간 자동 병합 금지 및 ambiguous_entity 격리
6. 4단계 (Step 4): 동음이의어(Python 언어 vs 비단뱀, Go 언어 vs 동사/바둑) 격리
7. 증분 연결 및 재실행 멱등성 검증
8. work_only 가시성 격리 및 global 엔터티 오염 방지 검증
9. PostgreSQL 방언 DDL 렌더링 및 쿼리 컴파일 정합성 검증
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateTable

from ttakkari.kb_ontology import (
    EntityResolutionStep,
    bootstrap_seed_aliases,
    find_seed_by_norm,
    get_seed_entities,
    is_seed_alias,
    resolve_entity,
)
from ttakkari.kb_ontology_models import (
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
)


# 상위 conftest.py의 PostgreSQL 세션 자동 픽스처를 오버라이드하여
# 샌드박스 환경(루프백 소켓 차단)에서도 SQLite in-memory로 독립 격리 테스트를 수행하도록 설정.
@pytest.fixture(scope="session", autouse=True)
def _schema_and_app() -> None:
    yield


@pytest.fixture(autouse=True)
def _clean() -> None:
    yield


@pytest_asyncio.fixture
async def async_db() -> AsyncGenerator[AsyncSession]:
    """SQLite in-memory 기반의 실제 테이블 왕복 테스트 세션."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    # 필요한 온톨로지 테이블 직접 생성
    tables = [
        KbEntity.__table__,
        KbEntityAlias.__table__,
        KbDocumentEntity.__table__,
        KbConflict.__table__,
    ]
    async with engine.begin() as conn:
        for t in tables:
            await conn.run_sync(t.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session

    await engine.dispose()


# ---------------------------------------------------------------------
# 1. 시드 별칭 사전 단위 테스트
# ---------------------------------------------------------------------
def test_seed_aliases_inventory() -> None:
    """시드 사전이 150개 이상의 핵심 기술 용어를 포함하고 한·영·약어가 바르게 매핑되어 있는지 검증."""
    seeds = get_seed_entities()
    assert len(seeds) >= 150, f"Expected at least 150 seeds, got {len(seeds)}"

    # 필수 기술 용어 존재 여부 확인
    names = {s.canonical_name for s in seeds}
    required = {
        "Kubernetes",
        "Pod",
        "Deployment",
        "StatefulSet",
        "DaemonSet",
        "ConfigMap",
        "Secret",
        "PersistentVolume",
        "PostgreSQL",
        "MySQL",
        "Redis",
        "Apache Kafka",
        "Python",
        "Go",
        "Rust",
        "FastAPI",
        "Large Language Model",
        "Retrieval-Augmented Generation",
        "Transformer",
        "OAuth",
        "JSON Web Token",
    }
    assert required.issubset(names), f"Missing required seeds: {required - names}"

    # 한·영 및 약어 정규화 색인 조회 검증
    k8s_seed = find_seed_by_norm("k8s")
    assert k8s_seed is not None
    assert k8s_seed.canonical_name == "Kubernetes"

    ko_k8s = find_seed_by_norm("쿠버네티스")
    assert ko_k8s is not None
    assert ko_k8s.canonical_name == "Kubernetes"

    postgres_seed = find_seed_by_norm("postgres")
    assert postgres_seed is not None
    assert postgres_seed.canonical_name == "PostgreSQL"

    assert is_seed_alias("쿠버네티스") is True
    assert is_seed_alias("k8s") is True
    assert is_seed_alias("파이썬") is True
    assert is_seed_alias("알수없는신조어1234") is False


@pytest.mark.asyncio
async def test_bootstrap_seed_aliases_idempotent(async_db: AsyncSession) -> None:
    """시드 사전의 DB 적재가 정상 동작하고, 재실행 시 중복 없이 멱등성을 유지하는지 검증."""
    # 1회차 부트스트랩
    inserted_1 = await bootstrap_seed_aliases(async_db, tenant_id="personal", visibility="global")
    assert inserted_1 >= 150

    # DB에 적재된 엔터티 및 별칭 확인
    k8s_stmt = select(KbEntity).where(KbEntity.norm_name == "kubernetes")
    k8s = (await async_db.execute(k8s_stmt)).scalar_one_or_none()
    assert k8s is not None
    assert k8s.canonical_name == "Kubernetes"
    assert k8s.domain == "infra"

    alias_stmt = select(KbEntityAlias.alias).where(KbEntityAlias.entity_id == k8s.id)
    aliases = {r[0] for r in (await async_db.execute(alias_stmt)).all()}
    assert "쿠버네티스" in aliases
    assert "k8s" in aliases

    # 2회차 부트스트랩: 추가 삽입 0건 (완전한 멱등성)
    inserted_2 = await bootstrap_seed_aliases(async_db, tenant_id="personal", visibility="global")
    assert inserted_2 == 0


# ---------------------------------------------------------------------
# 2. 1단계: 정규화 키(norm_name) 일치 테스트
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step1_exact_norm_name_resolution(async_db: AsyncSession) -> None:
    """대소문자, 하이픈, 밑줄 차이에도 1단계에서 동일 엔터티로 즉시 해소되는지 검증."""
    # 사전 엔터티 생성 (시드에 없는 순수 신규 기술명)
    first_res = await resolve_entity(
        db=async_db,
        raw_name="AcmeCloudEngine",
        entity_kind="technology",
        domain="infra",
        summary="커스텀 클라우드 엔진",
    )
    assert first_res.created is True
    assert first_res.step == EntityResolutionStep.NEW_ENTITY
    e_id = first_res.entity.id

    # 다양한 표기로 1단계 정규화 일치 조회
    variations = [
        "acmecloudengine",
        "Acme-Cloud-Engine",
        "ACME_CLOUD_ENGINE",
        " acme cloud engine ",
    ]
    for var in variations:
        res = await resolve_entity(
            db=async_db,
            raw_name=var,
            entity_kind="technology",
            domain="infra",
        )
        assert res.created is False
        assert res.step == EntityResolutionStep.EXACT_NORM_NAME
        assert res.entity.id == e_id
        assert res.entity.canonical_name == "AcmeCloudEngine"


# ---------------------------------------------------------------------
# 3. 2단계: 괄호 병기 및 한·영 별칭 통합 테스트 (Kubernetes/쿠버네티스/k8s)
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step2_bilingual_parenthetical_unification(async_db: AsyncSession) -> None:
    """'쿠버네티스(k8s)', '디플로이먼트(Deployment)' 괄호 병기 분리 및 단일 엔터티 통합 검증."""
    # 1. 시드 사전을 부트스트랩하지 않은 빈 DB 상태에서 괄호 병기 입력으로 첫 엔터티 등록
    res1 = await resolve_entity(
        db=async_db,
        raw_name="디플로이먼트(Deployment)",
        entity_kind="concept",
        domain="infra",
        summary="파드 선언적 배포 컨트롤러",
    )
    assert res1.created is True
    assert res1.entity.canonical_name == "Deployment"
    assert res1.entity.norm_name == "deployment"
    dep_id = res1.entity.id

    # 한글 '디플로이먼트'로 단독 조회 시 Step 2(별칭 일치)로 Deployment에 연결되어야 함
    res2 = await resolve_entity(
        db=async_db,
        raw_name="디플로이먼트",
        entity_kind="concept",
        domain="infra",
    )
    assert res2.created is False
    assert res2.step == EntityResolutionStep.ALIAS_OR_PARENTHETICAL
    assert res2.entity.id == dep_id

    # 2. Kubernetes / 쿠버네티스 / k8s 3중 통합 검증
    # DB에 아직 k8s가 없어도 시드 사전을 통해 자동으로 단일 엔터티에 해소되어야 함
    res_k8s = await resolve_entity(
        db=async_db,
        raw_name="k8s",
        entity_kind="technology",
        domain="infra",
    )
    k8s_id = res_k8s.entity.id
    assert res_k8s.entity.canonical_name == "Kubernetes"

    # 한국어 '쿠버네티스'로 조회 시 동일 엔터티 반환
    res_ko = await resolve_entity(
        db=async_db,
        raw_name="쿠버네티스",
        domain="infra",
    )
    assert res_ko.entity.id == k8s_id
    assert res_ko.step in (EntityResolutionStep.EXACT_NORM_NAME, EntityResolutionStep.ALIAS_OR_PARENTHETICAL)

    # 영문 표준 'Kubernetes'로 조회 시 동일 엔터티 반환
    res_en = await resolve_entity(
        db=async_db,
        raw_name="Kubernetes",
        domain="infra",
    )
    assert res_en.entity.id == k8s_id


# ---------------------------------------------------------------------
# 4. 3단계: 임베딩 및 이름 유사도 일치 테스트
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step3_similarity_match(async_db: AsyncSession) -> None:
    """코사인 유사도 >= 0.88 AND Jaro-Winkler >= 0.85일 때 동일 엔터티로 자동 연결 (Step 3)."""
    # 원본 엔터티 등록
    v_orig = [0.1] * 128
    res_orig = await resolve_entity(
        db=async_db,
        raw_name="Elasticsearch",
        entity_kind="technology",
        domain="dev",
        summary="오픈소스 분산 검색 엔진",
    )
    orig_id = res_orig.entity.id

    # 약간의 철자 변형(예: "Elastiksearch") + 높은 임베딩 유사도(0.95)
    v_query = [0.105] * 128  # 코사인 유사도 ~ 1.0
    cand_embeddings = {orig_id: v_orig}

    res_sim = await resolve_entity(
        db=async_db,
        raw_name="Elastiksearch",  # 오타
        entity_kind="technology",
        domain="dev",
        embedding=v_query,
        candidate_embeddings=cand_embeddings,
    )

    assert res_sim.created is False
    assert res_sim.step == EntityResolutionStep.SIMILARITY_MATCH
    assert res_sim.entity.id == orig_id
    # 새 표기가 별칭으로 안전하게 학습되었는지 확인
    assert "Elastiksearch" in res_sim.learned_aliases


# ---------------------------------------------------------------------
# 5. 4단계: 경계 유사도 구간 오병합 방지 테스트
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step4_borderline_similarity_isolated(async_db: AsyncSession) -> None:
    """경계 유사도 구간(0.78 <= cos < 0.88)에서는 절대로 자동 병합하지 않고 ambiguous_entity 로 격리."""
    v_orig = [1.0, 0.0, 0.0, 0.0] + [0.0] * 124
    res_orig = await resolve_entity(
        db=async_db,
        raw_name="Microservices",
        entity_kind="concept",
        domain="dev",
        summary="독립 서비스 분산 아키텍처",
    )
    orig_id = res_orig.entity.id

    # 유사하지만 다른 개념 (예: "Miniservices")
    # 코사인 유사도를 의도적으로 0.82 (경계 구간)로 설정
    v_border = [0.82, 0.57, 0.0, 0.0] + [0.0] * 124  # cos_sim 약 0.82
    cand_embeddings = {orig_id: v_orig}

    res_border = await resolve_entity(
        db=async_db,
        raw_name="Miniservices",
        entity_kind="concept",
        domain="dev",
        summary="마이크로서비스보다 큰 단위의 서비스 분할",
        embedding=v_border,
        candidate_embeddings=cand_embeddings,
    )

    # 자동 병합되지 않고 신규 생성 + conflict 격리
    assert res_border.created is True
    assert res_border.step == EntityResolutionStep.AMBIGUOUS_ISOLATED
    assert res_border.entity.id != orig_id
    assert res_border.conflict is not None
    assert res_border.conflict.conflict_kind == "ambiguous_entity"
    assert res_border.conflict.entity_id == orig_id
    assert res_border.conflict.candidate_entity_id == res_border.entity.id
    assert res_border.conflict.status == "open"


# ---------------------------------------------------------------------
# 6. 4단계: 동음이의어(Python, Go, Service) 격리 테스트
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step4_homonyms_isolation(async_db: AsyncSession) -> None:
    """동음이의어(Python 언어 vs 비단뱀, Go 언어 vs 동사/게임): 도메인이 상충하면 절대 병합하지 않음."""
    # 1. dev 도메인의 Python (프로그래밍 언어)
    res_py_dev = await resolve_entity(
        db=async_db,
        raw_name="Python",
        entity_kind="technology",
        domain="dev",
        summary="파이썬 프로그래밍 언어",
    )
    assert res_py_dev.created is True
    py_dev_id = res_py_dev.entity.id

    # 2. science 도메인의 Python (파이톤 비단뱀)
    res_py_snake = await resolve_entity(
        db=async_db,
        raw_name="Python",
        entity_kind="concept",
        domain="science",
        summary="비단뱀과에 속하는 파충류 동물",
    )
    # dev의 Python과 절대로 자동 병합되지 않아야 함!
    assert res_py_snake.created is True
    assert res_py_snake.step == EntityResolutionStep.AMBIGUOUS_ISOLATED
    assert res_py_snake.entity.id != py_dev_id
    assert res_py_snake.conflict is not None
    assert res_py_snake.conflict.conflict_kind == "ambiguous_entity"
    assert res_py_snake.conflict.entity_id == py_dev_id

    # 3. Go (dev 프로그래밍 언어) vs Go (general 동사/바둑)
    res_go_dev = await resolve_entity(
        db=async_db,
        raw_name="Go",
        entity_kind="technology",
        domain="dev",
        summary="구글이 만든 Golang",
    )
    go_dev_id = res_go_dev.entity.id

    res_go_verb = await resolve_entity(
        db=async_db,
        raw_name="Go",
        entity_kind="concept",
        domain="general",
        summary="가다(동사) 또는 바둑 게임",
    )
    assert res_go_verb.created is True
    assert res_go_verb.step == EntityResolutionStep.AMBIGUOUS_ISOLATED
    assert res_go_verb.entity.id != go_dev_id


# ---------------------------------------------------------------------
# 7. 증분 연결 및 멱등성 테스트
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_incremental_linking_and_idempotency(async_db: AsyncSession) -> None:
    """새 문서의 언급 증분 연결(kb_document_entities) 및 반복 실행 멱등성 검증."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()
    passage_id = uuid.uuid4()
    ev_id = uuid.uuid4()

    # 첫 번째 언급 해소
    res1 = await resolve_entity(
        db=async_db,
        raw_name="RabbitMQ",
        domain="infra",
        document_id=doc_id,
        revision_id=rev_id,
        passage_id=passage_id,
        evidence_id=ev_id,
        role="mentioned",
        salience=0.8,
    )
    assert res1.created is True
    ent_id = res1.entity.id
    assert res1.entity.mention_count == 1

    # 증분 연결 확인
    link_stmt = select(KbDocumentEntity).where(
        KbDocumentEntity.passage_id == passage_id,
        KbDocumentEntity.entity_id == ent_id,
    )
    link = (await async_db.execute(link_stmt)).scalar_one_or_none()
    assert link is not None
    assert link.document_id == doc_id
    assert link.evidence_id == ev_id
    assert link.salience == 0.8

    # 동일 요청 재호출 (멱등성 검증)
    res2 = await resolve_entity(
        db=async_db,
        raw_name="RabbitMQ",
        domain="infra",
        document_id=doc_id,
        revision_id=rev_id,
        passage_id=passage_id,
        evidence_id=ev_id,
    )
    assert res2.created is False
    assert res2.entity.id == ent_id
    assert res2.step == EntityResolutionStep.EXACT_NORM_NAME

    # 중복 kb_document_entities 생성 없음 검증
    all_links = (await async_db.execute(link_stmt)).scalars().all()
    assert len(all_links) == 1


# ---------------------------------------------------------------------
# 8. work_only 가시성 격리 및 global 오염 방지 테스트
# ---------------------------------------------------------------------
@pytest.mark.asyncio
async def test_work_only_visibility_boundary_and_no_pollution(async_db: AsyncSession) -> None:
    """work_only 언급이 global 엔터티의 별칭/카운트를 오염시키지 않고, fail-closed 격리되는지 검증."""
    # 1. global 엔터티 생성
    res_global = await resolve_entity(
        db=async_db,
        raw_name="PostgreSQL",
        domain="dev",
        visibility="global",
        origin_scope="personal",
    )
    pg_id = res_global.entity.id
    init_mention_count = res_global.entity.mention_count

    # 2. 사내 비공개 문서(visibility='work_only')에서 global PostgreSQL을 언급하면서
    #    사내 전용 프로젝트 코드명("Project-Secret-DB")을 별칭으로 포함하여 호출
    res_work = await resolve_entity(
        db=async_db,
        raw_name="PostgreSQL",
        aliases=["Project-Secret-DB"],
        domain="dev",
        visibility="work_only",
        origin_scope="work",
        evidence_id=uuid.uuid4(),
    )
    assert res_work.entity.id == pg_id

    # 불변 규칙 검증: global 엔터티의 별칭에 사내 코드명이 오염되어 들어가면 안 됨!
    alias_stmt = select(KbEntityAlias.alias).where(KbEntityAlias.entity_id == pg_id)
    aliases = {r[0] for r in (await async_db.execute(alias_stmt)).all()}
    assert "Project-Secret-DB" not in aliases, "Security violation: work_only alias leaked into global entity!"

    # 불변 규칙 검증: global 엔터티의 카운트가 work_only 문서로 인해 오염(증가)되면 안 됨!
    assert res_global.entity.mention_count == init_mention_count

    # 3. work_only 로 신규 엔터티 생성
    res_internal = await resolve_entity(
        db=async_db,
        raw_name="InternalHyperEzCore",
        domain="dev",
        visibility="work_only",
        origin_scope="work",
    )
    assert res_internal.created is True
    assert res_internal.entity.visibility == "work_only"
    int_id = res_internal.entity.id

    # 4. global 컨텍스트에서 검색 시 work_only 엔터티는 절대 조회되지 않아야 함 (fail-closed)
    res_isolated = await resolve_entity(
        db=async_db,
        raw_name="InternalHyperEzCore",
        domain="dev",
        visibility="global",  # global 사용자 세션
    )
    # global 세션에는 work_only 가 노출되지 않으므로, 새 엔터티로 생성되거나 매칭되지 않아야 함
    assert res_isolated.entity.id != int_id
    assert res_isolated.entity.visibility == "global"


# ---------------------------------------------------------------------
# 9. PostgreSQL 스키마 DDL 컴파일 정합성 검증
# ---------------------------------------------------------------------
def test_postgresql_ddl_compilation() -> None:
    """PostgreSQL 방언으로 온톨로지 테이블 DDL 및 인덱스가 완벽히 컴파일되는지 검증."""
    dialect = postgresql.dialect()

    for table in [
        KbEntity.__table__,
        KbEntityAlias.__table__,
        KbDocumentEntity.__table__,
        KbConflict.__table__,
    ]:
        create_sql = str(CreateTable(table).compile(dialect=dialect))
        assert "CREATE TABLE" in create_sql
        assert table.name in create_sql
