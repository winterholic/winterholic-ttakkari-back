"""KB v3 통합 접근 제어 및 권한 컨텍스트 (KB3-103 공통 접근정책 기반).

단일 KB 제품공간을 지향하되, origin_scope 와 감사(audit) 추적성을 보존한다.
Caller 유형(Role)과 신뢰된 컨텍스트(Trusted Context)에 따라 접근 가능한 문서와
온톨로지 객체(엔터티, 주장, 관계, 정본, 충돌)의 가시성(visibility)을 엄격하게 제한한다.

핵심 보안 원칙:
1. 단일 제품공간 + origin_scope/audit 보존:
   - owner JWT web: tenant 내 authorized 'global' 및 'work_only' 모두 단일 목록/검색 조회.
   - work agent: 허용된 'global' + 'work_only' 조회.
   - personal agent: 'global'만 조회 ('work_only'는 비노출).
   - access 레코드가 없거나 미상인 문서: work_only 로 취급한다(kb_visibility 와 같은 규칙). personal 컨텍스트에서는 보이지 않고,
     work_only 를 허용하는 컨텍스트에서만 보인다.
   - hidden=True 문서: 명시적 요청이 없는 한 기본 제외.
2. Caller 임의 scope='all' 위조 방지:
   - caller가 임의 문자열 'all' 등을 보내더라도 보안 경계를 넓히지 못하도록
     정형화된 Typed Explicit Context(KbAccessContext)를 통해서만 권한을 부여.
3. 노드 visibility만으로 불충분 (Strict Provenance Filter):
   - 엔터티 alias/count/정본/relations/conflicts/matched_entities와 snippet/evidence 모두
     허용된 원문 doc join을 근거로 필터링한다.
   - guessed UUID, hidden/current-only, work entity가 global node alias/count를 오염하는 반례 차단.
   - 다른 tenant 자료 절대 누출 금지.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Literal

from sqlalchemy import and_, func, not_, or_, select

# 가시성 상수
VISIBILITY_GLOBAL = "global"
VISIBILITY_WORK_ONLY = "work_only"
ALL_VISIBILITIES: tuple[str, ...] = (VISIBILITY_GLOBAL, VISIBILITY_WORK_ONLY)
GLOBAL_ONLY_VISIBILITY: tuple[str, ...] = (VISIBILITY_GLOBAL,)

# 온톨로지(엔터티·별칭) 네임스페이스. 문서 접근 tenant 와 별개다.
ONTOLOGY_TENANT_ID = "personal"

# 문서 출현 근거 없이도 신뢰하는 별칭 출처
CURATED_ALIAS_SOURCES: tuple[str, ...] = ("seed", "user_verified")

# 스코프 상수
SCOPE_PERSONAL = "personal"
SCOPE_WORK = "work"

# 역할(Role) 타입
CallerRole = Literal["owner", "work_agent", "personal_agent", "system"]


@dataclass(frozen=True)
class KbAccessContext:
    """지식 도서관 조회를 위한 타입 보장 접근 제어 컨텍스트.

    후속 API 워커 및 백필 파이프라인에서 공통으로 재사용하는 신뢰 경계 계약이다.
    """

    tenant_id: str = "owner"
    caller_role: CallerRole = "owner"
    allowed_visibilities: tuple[str, ...] = field(default_factory=lambda: ALL_VISIBILITIES)
    include_hidden: bool = False
    strict_provenance: bool = True
    origin_scope_filter: str | None = None
    # kb_entities.tenant_id 는 온톨로지 네임스페이스(해소기·추출기 기본값 "personal")이고, 문서 접근 행의 tenant_id("owner")와
    # 다른 축이다. 두 값을 같은 문자열로 비교하면 실제 추출된 엔터티가 전부 걸러진다(실 PG 흐름에서 확인).
    ontology_tenant_id: str = ONTOLOGY_TENANT_ID
    # Explicit company-engine grants are independent of source classification.
    engine_pool: str | None = None

    def can_access_visibility(self, visibility: str | None) -> bool:
        """주어진 visibility 에 접근 가능한지 검사."""
        if not visibility:
            return False
        return visibility in self.allowed_visibilities

    def is_work_accessible(self) -> bool:
        """work_only 자원에 접근 가능한 컨텍스트인지 확인."""
        return VISIBILITY_WORK_ONLY in self.allowed_visibilities


def create_owner_context(
    tenant_id: str = "owner",
    *,
    origin_scope_filter: str | None = None,
    include_hidden: bool = False,
) -> KbAccessContext:
    """웹 UI(owner JWT)를 위한 컨텍스트: tenant 내 global 및 work_only 단일 목록/검색 조회."""
    return KbAccessContext(
        tenant_id=tenant_id,
        caller_role="owner",
        allowed_visibilities=ALL_VISIBILITIES,
        include_hidden=include_hidden,
        strict_provenance=True,
        origin_scope_filter=origin_scope_filter,
    )


def create_work_agent_context(
    tenant_id: str = "owner",
    *,
    include_hidden: bool = False,
) -> KbAccessContext:
    """회사 세션(pool == 'work') 에이전트를 위한 컨텍스트: global + work_only 모두 허용."""
    return KbAccessContext(
        tenant_id=tenant_id,
        caller_role="work_agent",
        allowed_visibilities=ALL_VISIBILITIES,
        include_hidden=include_hidden,
        strict_provenance=True,
        origin_scope_filter=None,
    )


def create_personal_agent_context(
    tenant_id: str = "owner",
    *,
    include_hidden: bool = False,
) -> KbAccessContext:
    """개인 세션(pool != 'work') 에이전트를 위한 컨텍스트: global 만 허용 (work_only 격리)."""
    return KbAccessContext(
        tenant_id=tenant_id,
        caller_role="personal_agent",
        allowed_visibilities=GLOBAL_ONLY_VISIBILITY,
        include_hidden=include_hidden,
        strict_provenance=True,
        origin_scope_filter=None,
    )


def create_web_context(
    tenant_id: str = "owner",
    *,
    include_hidden: bool = False,
) -> KbAccessContext:
    """소유자가 이미 접근 가능한 자료를 하나의 도서관에서 조회한다."""
    return create_owner_context(tenant_id=tenant_id, include_hidden=include_hidden)


def from_web_request(scope: str | None, tenant_id: str = "owner") -> KbAccessContext:
    """웹 API 의 (선택적·deprecated) scope 쿼리를 단일 규칙의 컨텍스트로 바꾼다.

    생략하면 통합 도서관(create_web_context). 'work' 만 기존 회사 범위 조회를 명시적으로 고르는 레거시 경로로 남긴다.
    'personal' 은 통합 도서관과 같다. 그 밖의 값은 거부한다(fail-closed, 'all' 위조로 경계를 넓힐 수 없다).
    """
    if scope in (None, "", SCOPE_PERSONAL):
        return create_web_context(tenant_id=tenant_id)
    if scope == SCOPE_WORK:
        return from_legacy_scope(SCOPE_WORK, tenant_id=tenant_id)
    raise ValueError("scope must be 'personal' or 'work' when given")


def from_agent_pool(pool: str | None, tenant_id: str = "owner") -> KbAccessContext:
    """에이전트 풀 이름('work', 'personal' 등)에 따라 적절한 에이전트 컨텍스트 생성."""
    if pool == "work":
        return with_engine_pool(create_work_agent_context(tenant_id=tenant_id), "work")
    return create_personal_agent_context(tenant_id=tenant_id)


def with_engine_pool(ctx: KbAccessContext, pool: str | None) -> KbAccessContext:
    """Add a trusted engine grant restriction without widening source visibility."""
    return replace(ctx, engine_pool="work") if pool == "work" else ctx


def from_legacy_scope(scope: str | None = "personal", tenant_id: str = "owner") -> KbAccessContext:
    """레거시 scope ('personal' | 'work') 파라미터와의 하위 호환 래퍼.

    기존 보안보다 넓어지지 않도록 엄격하게 매핑한다:
    - 'work' -> work_only 만 허용(또는 work caller)
    - 'personal' -> global 만 허용
    - 'all' 등 임의 입력: 보안상 거부되거나 기본 fail-closed(global only)로 축소.
    """
    if scope == SCOPE_WORK:
        return KbAccessContext(
            tenant_id=tenant_id,
            caller_role="work_agent",
            allowed_visibilities=(VISIBILITY_WORK_ONLY,),
            include_hidden=False,
            strict_provenance=True,
            origin_scope_filter=SCOPE_WORK,
        )
    # 기본값 및 'personal'은 global만 허용하여 회사 기밀 누출 방지
    return KbAccessContext(
        tenant_id=tenant_id,
        caller_role="personal_agent",
        allowed_visibilities=GLOBAL_ONLY_VISIBILITY,
        include_hidden=False,
        strict_provenance=True,
        origin_scope_filter=SCOPE_PERSONAL if scope == SCOPE_PERSONAL else None,
    )


def from_caller_visibility(
    caller_visibility: str, *, ontology_tenant_id: str = ONTOLOGY_TENANT_ID, tenant_id: str = "owner"
) -> KbAccessContext:
    """ontology 조회 API 의 caller_visibility('global'|'work_only')를 같은 단일 접근 규칙의 컨텍스트로 바꾼다.

    'global' 은 개인 세션(global 만), 'work_only' 는 회사 세션(global + work_only)이다. 그 밖의 값은 거부한다(fail-closed).
    """
    if caller_visibility == VISIBILITY_GLOBAL:
        ctx = create_personal_agent_context(tenant_id=tenant_id)
    elif caller_visibility == VISIBILITY_WORK_ONLY:
        ctx = create_work_agent_context(tenant_id=tenant_id)
    else:
        raise ValueError("caller_visibility must be 'global' or 'work_only'")
    return KbAccessContext(
        tenant_id=ctx.tenant_id,
        caller_role=ctx.caller_role,
        allowed_visibilities=ctx.allowed_visibilities,
        include_hidden=ctx.include_hidden,
        strict_provenance=ctx.strict_provenance,
        origin_scope_filter=ctx.origin_scope_filter,
        ontology_tenant_id=ontology_tenant_id,
        engine_pool=ctx.engine_pool,
    )


# =========================================================================
# SQLAlchemy Query Filter Helpers
# =========================================================================

def document_access_conditions(ctx: KbAccessContext, access_model: Any = None) -> list:
    """KbDocumentAccess 행 하나가 ctx 에 허용되는 조건(tenant·visibility·hidden·origin_scope)의 단일 정의.

    문서·패시지·엔터티·주장·관계 필터와 ontology 그래프/충돌/정본 조회가 모두 이 조건을 쓴다. 호출자가 access 행을 조인해 둔 쿼리용이다.
    """
    from ttakkari.collector.control_models import KbDocumentAccess

    access = access_model if access_model is not None else KbDocumentAccess
    conditions = [
        access.tenant_id == ctx.tenant_id,
        access.visibility.in_(ctx.allowed_visibilities),
    ]
    if not ctx.include_hidden:
        conditions.append(access.hidden.is_(False))
    if ctx.origin_scope_filter:
        conditions.append(access.origin_scope == ctx.origin_scope_filter)
    if ctx.engine_pool == "work":
        conditions.append(or_(access.pool == "work", access.scope == "work"))
    return conditions

def unclassified_documents_visible(ctx: KbAccessContext) -> bool:
    """access 행이 없는 문서(미상 = origin 'work', visibility 'work_only')를 ctx 가 볼 수 있는지.

    문서 접근 행이 없다는 것은 scope 를 알 수 없다는 뜻이라 kb_visibility 와 똑같이 ('work', 'work_only') 로 본다.
    work_only 를 허용하지 않는 컨텍스트(personal)나 origin_scope 가 personal 로 제한된 컨텍스트에서는 절대 보이지 않는다.
    """
    if ctx.engine_pool == "work" or VISIBILITY_WORK_ONLY not in ctx.allowed_visibilities:
        return False
    return ctx.origin_scope_filter in (None, SCOPE_WORK)


def document_access_filter(ctx: KbAccessContext, doc_model: Any = None):
    """KbDocument 테이블 대상 접근 제어 필터 절 생성.

    문서에 연결된 KbDocumentAccess 레코드가 다음 조건을 만족해야 한다:
    1. tenant_id == ctx.tenant_id
    2. visibility IN ctx.allowed_visibilities
    3. hidden == False (include_hidden이 False일 때)
    4. origin_scope_filter가 지정된 경우 origin_scope == ctx.origin_scope_filter
    5. 접근 행이 없는 문서는 work_only 로 취급한다(unclassified_documents_visible). personal 에는 보이지 않는다.
    """
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_models import KbDocument

    target_doc = doc_model if doc_model is not None else KbDocument

    conditions = [
        KbDocumentAccess.document_id == target_doc.id,
        *document_access_conditions(ctx, KbDocumentAccess),
    ]

    matched = select(KbDocumentAccess.document_id).where(and_(*conditions)).exists()

    # access 행이 없거나 미상이면 work_only 로 취급한다: work_only 를 볼 수 있는 컨텍스트에만 노출한다.
    if unclassified_documents_visible(ctx):
        any_access = select(KbDocumentAccess.document_id).where(
            KbDocumentAccess.document_id == target_doc.id
        ).exists()
        return or_(matched, not_(any_access))

    return matched


def passage_access_filter(ctx: KbAccessContext, passage_model: Any = None):
    """KbPassage 대상 접근 제어 필터 절 생성.

    Passage -> Revision -> Document -> Access 연결 검증.
    """
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_models import KbPassage, KbRevision

    p = passage_model if passage_model is not None else KbPassage

    conditions = [
        KbDocumentAccess.document_id == KbRevision.document_id,
        KbRevision.id == p.revision_id,
        *document_access_conditions(ctx, KbDocumentAccess),
    ]

    matched = select(KbDocumentAccess.document_id).select_from(KbRevision).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(KbRevision.id == p.revision_id, *conditions[2:])).exists()

    if unclassified_documents_visible(ctx):
        any_access = select(KbDocumentAccess.document_id).select_from(KbRevision).where(
            KbRevision.id == p.revision_id,
            KbDocumentAccess.document_id == KbRevision.document_id,
        ).exists()
        return or_(matched, not_(any_access))

    return matched


def entity_access_filter(ctx: KbAccessContext, entity_model: Any = None):
    """KbEntity 대상 접근 제어 필터 절 생성.

    노드 자체의 visibility 체크뿐 아니라,
    strict_provenance=True 일 때는 허용된 원본 문서(KbDocumentEntity -> KbDocumentAccess)에
    실제로 존재하는지 또는 원천이 없는 안전한 global 시드 엔터티인지 검증한다.
    """
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_ontology_models import KbDocumentEntity, KbEntity

    e = entity_model if entity_model is not None else KbEntity

    # 1. 엔티티 자체 visibility 및 tenant 확인
    base_cond = and_(
        e.tenant_id == ctx.ontology_tenant_id,
        e.visibility.in_(ctx.allowed_visibilities),
    )
    if ctx.origin_scope_filter:
        base_cond = and_(base_cond, e.origin_scope == ctx.origin_scope_filter)

    if not ctx.strict_provenance:
        return base_cond

    # 2. strict_provenance: 허용된 문서 출현 검증
    doc_access_cond = [
        KbDocumentEntity.entity_id == e.id,
        KbDocumentAccess.document_id == KbDocumentEntity.document_id,
        *document_access_conditions(ctx, KbDocumentAccess),
    ]

    has_permitted_doc = select(KbDocumentEntity.entity_id).select_from(KbDocumentEntity).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbDocumentEntity.document_id
    ).where(and_(*doc_access_cond)).exists()

    # 문서 연결이 아직 없는 시드 엔터티의 경우: global 허용 컨텍스트에서만 허용
    is_pure_seed = and_(
        e.visibility == VISIBILITY_GLOBAL,
        e.origin_scope == SCOPE_PERSONAL,
        not_(select(KbDocumentEntity.entity_id).where(KbDocumentEntity.entity_id == e.id).exists()),
    )

    return and_(base_cond, or_(has_permitted_doc, is_pure_seed))


def entity_alias_access_filter(
    ctx: KbAccessContext, entity_model: Any = None, alias_model: Any = None
):
    """Require an alias to occur in a passage from an authorized source document.

    Alias rows have no source-document foreign key. The correlated occurrence check
    prevents an alias learned only from hidden/work-only material from contaminating
    a globally visible entity that also has public provenance.
    """
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_models import KbPassage, KbRevision
    from ttakkari.kb_ontology_models import KbDocumentEntity, KbEntity, KbEntityAlias

    e = entity_model or KbEntity
    a = alias_model or KbEntityAlias
    conditions = [
        KbDocumentEntity.entity_id == e.id,
        KbPassage.id == KbDocumentEntity.passage_id,
        KbRevision.id == KbPassage.revision_id,
        KbDocumentAccess.document_id == KbRevision.document_id,
        *document_access_conditions(ctx, KbDocumentAccess),
        func.strpos(func.lower(KbPassage.body), func.lower(a.alias)) > 0,
    ]
    occurs_in_authorized_passage = select(KbDocumentEntity.entity_id).select_from(KbDocumentEntity).join(
        KbPassage, KbPassage.id == KbDocumentEntity.passage_id
    ).join(
        KbRevision, KbRevision.id == KbPassage.revision_id
    ).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(a.entity_id == e.id, *conditions)).exists()
    # seed/user_verified 별칭은 코드·사용자가 정한 사전이다. 문서에서 배운 값이 아니므로 어떤 문서 본문에도 나타나지 않아도 된다
    # (영어 문서만 있는 엔터티를 한국어 별칭으로 찾는 경로). 추출 출처(extracted) 별칭만 출현 근거를 요구한다.
    return or_(a.source.in_(CURATED_ALIAS_SOURCES), occurs_in_authorized_passage)


def assertion_access_filter(ctx: KbAccessContext, assertion_model: Any = None):
    """KbAssertion 대상 접근 제어 필터 절 생성."""
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_models import KbEvidence, KbPassage, KbRevision
    from ttakkari.kb_ontology_models import KbAssertion, KbAssertionEvidence

    a = assertion_model if assertion_model is not None else KbAssertion

    base_cond = and_(
        a.visibility.in_(ctx.allowed_visibilities),
    )
    if ctx.origin_scope_filter:
        base_cond = and_(base_cond, a.origin_scope == ctx.origin_scope_filter)

    if not ctx.strict_provenance:
        return base_cond

    # 주장-근거 연결을 통해 원본 문서 접근 권한 확인 (KbAssertionEvidence -> KbEvidence -> KbPassage -> KbRevision -> KbDocumentAccess)
    doc_cond = [
        KbAssertionEvidence.assertion_id == a.id,
        KbEvidence.id == KbAssertionEvidence.evidence_id,
        KbPassage.id == KbEvidence.passage_id,
        KbRevision.id == KbPassage.revision_id,
        KbDocumentAccess.document_id == KbRevision.document_id,
        *document_access_conditions(ctx, KbDocumentAccess),
    ]

    has_valid_evidence = select(KbAssertionEvidence.assertion_id).select_from(
        KbAssertionEvidence
    ).join(KbEvidence, KbEvidence.id == KbAssertionEvidence.evidence_id).join(
        KbPassage, KbPassage.id == KbEvidence.passage_id
    ).join(KbRevision, KbRevision.id == KbPassage.revision_id).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(*doc_cond)).exists()

    return and_(base_cond, has_valid_evidence)


RELATION_STATUS_WITH_CANDIDATES: tuple[str, ...] = ("active", "candidate")


def relation_access_filter(
    ctx: KbAccessContext, relation_model: Any = None, *, include_candidates: bool = False
):
    """KbRelation 대상 접근 제어 필터 절 생성.

    기본은 active 관계만이다. include_candidates 는 근거 행이 있는 candidate 도 포함하는 요청 옵션이며,
    호출자가 '후보' 표시와 낮은 가중치를 직접 적용해야 한다(검색 1-hop·그래프).
    """
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_models import KbEvidence, KbPassage, KbRevision
    from ttakkari.kb_ontology_models import KbRelation, KbRelationEvidence

    r = relation_model if relation_model is not None else KbRelation

    base_cond = and_(
        r.visibility.in_(ctx.allowed_visibilities),
        r.status.in_(RELATION_STATUS_WITH_CANDIDATES) if include_candidates else r.status == "active",
        r.evidence_count >= 1,
    )
    if ctx.origin_scope_filter:
        base_cond = and_(base_cond, r.origin_scope == ctx.origin_scope_filter)

    if not ctx.strict_provenance:
        return base_cond

    # 관계-근거 연결을 통해 원본 문서 접근 권한 확인
    doc_cond = [
        KbRelationEvidence.relation_id == r.id,
        KbEvidence.id == KbRelationEvidence.evidence_id,
        KbPassage.id == KbEvidence.passage_id,
        KbRevision.id == KbPassage.revision_id,
        KbDocumentAccess.document_id == KbRevision.document_id,
        *document_access_conditions(ctx, KbDocumentAccess),
    ]

    has_valid_evidence = select(KbRelationEvidence.relation_id).select_from(
        KbRelationEvidence
    ).join(KbEvidence, KbEvidence.id == KbRelationEvidence.evidence_id).join(
        KbPassage, KbPassage.id == KbEvidence.passage_id
    ).join(KbRevision, KbRevision.id == KbPassage.revision_id).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(*doc_cond)).exists()

    return and_(base_cond, has_valid_evidence)
