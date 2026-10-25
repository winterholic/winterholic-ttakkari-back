"""KB v3 통합 접근 제어 및 권한 컨텍스트 (KB3-103 공통 접근정책 기반).

단일 KB 제품공간을 지향하되, origin_scope 와 감사(audit) 추적성을 보존한다.
Caller 유형(Role)과 신뢰된 컨텍스트(Trusted Context)에 따라 접근 가능한 문서와
온톨로지 객체(엔터티, 주장, 관계, 정본, 충돌)의 가시성(visibility)을 엄격하게 제한한다.

핵심 보안 원칙:
1. 단일 제품공간 + origin_scope/audit 보존:
   - owner JWT web: tenant 내 authorized 'global' 및 'work_only' 모두 단일 목록/검색 조회.
   - work agent: 허용된 'global' + 'work_only' 조회.
   - personal agent: 'global'만 조회 ('work_only'는 비노출).
   - access 레코드가 없거나 미상인 문서: fail-closed로 격리 (기본 제외).
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

from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import and_, func, not_, or_, select

# 가시성 상수
VISIBILITY_GLOBAL = "global"
VISIBILITY_WORK_ONLY = "work_only"
ALL_VISIBILITIES: tuple[str, ...] = (VISIBILITY_GLOBAL, VISIBILITY_WORK_ONLY)
GLOBAL_ONLY_VISIBILITY: tuple[str, ...] = (VISIBILITY_GLOBAL,)

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


def from_agent_pool(pool: str | None, tenant_id: str = "owner") -> KbAccessContext:
    """에이전트 풀 이름('work', 'personal' 등)에 따라 적절한 에이전트 컨텍스트 생성."""
    if pool == "work":
        return create_work_agent_context(tenant_id=tenant_id)
    return create_personal_agent_context(tenant_id=tenant_id)


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


# =========================================================================
# SQLAlchemy Query Filter Helpers
# =========================================================================

def document_access_filter(ctx: KbAccessContext, doc_model: Any = None):
    """KbDocument 테이블 대상 접근 제어 필터 절 생성.

    문서에 연결된 KbDocumentAccess 레코드가 다음 조건을 만족해야 한다:
    1. tenant_id == ctx.tenant_id
    2. visibility IN ctx.allowed_visibilities
    3. hidden == False (include_hidden이 False일 때)
    4. origin_scope_filter가 지정된 경우 origin_scope == ctx.origin_scope_filter
    5. 레거시 미분류 행: KbDocumentAccess 가 없는 행은 personal_agent/owner(global 허용 시)에만 보존.
    """
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_models import KbDocument

    target_doc = doc_model if doc_model is not None else KbDocument

    conditions = [
        KbDocumentAccess.document_id == target_doc.id,
        KbDocumentAccess.tenant_id == ctx.tenant_id,
        KbDocumentAccess.visibility.in_(ctx.allowed_visibilities),
    ]
    if not ctx.include_hidden:
        conditions.append(KbDocumentAccess.hidden.is_(False))
    if ctx.origin_scope_filter:
        conditions.append(KbDocumentAccess.origin_scope == ctx.origin_scope_filter)

    matched = select(KbDocumentAccess.document_id).where(and_(*conditions)).exists()

    # 레거시 호환: access 레코드가 아예 없는 과거 문서는 global 허용 시에만 노출
    if VISIBILITY_GLOBAL in ctx.allowed_visibilities:
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
        KbDocumentAccess.tenant_id == ctx.tenant_id,
        KbDocumentAccess.visibility.in_(ctx.allowed_visibilities),
    ]
    if not ctx.include_hidden:
        conditions.append(KbDocumentAccess.hidden.is_(False))
    if ctx.origin_scope_filter:
        conditions.append(KbDocumentAccess.origin_scope == ctx.origin_scope_filter)

    matched = select(KbDocumentAccess.document_id).select_from(KbRevision).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(KbRevision.id == p.revision_id, *conditions[2:])).exists()

    if VISIBILITY_GLOBAL in ctx.allowed_visibilities:
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
        e.tenant_id == ctx.tenant_id,
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
        KbDocumentAccess.tenant_id == ctx.tenant_id,
        KbDocumentAccess.visibility.in_(ctx.allowed_visibilities),
    ]
    if not ctx.include_hidden:
        doc_access_cond.append(KbDocumentAccess.hidden.is_(False))

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
        KbDocumentAccess.tenant_id == ctx.tenant_id,
        KbDocumentAccess.visibility.in_(ctx.allowed_visibilities),
        func.strpos(func.lower(KbPassage.body), func.lower(a.alias)) > 0,
    ]
    if not ctx.include_hidden:
        conditions.append(KbDocumentAccess.hidden.is_(False))
    if ctx.origin_scope_filter:
        conditions.append(KbDocumentAccess.origin_scope == ctx.origin_scope_filter)
    return select(KbDocumentEntity.entity_id).select_from(KbDocumentEntity).join(
        KbPassage, KbPassage.id == KbDocumentEntity.passage_id
    ).join(
        KbRevision, KbRevision.id == KbPassage.revision_id
    ).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(a.entity_id == e.id, *conditions)).exists()


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
        KbDocumentAccess.tenant_id == ctx.tenant_id,
        KbDocumentAccess.visibility.in_(ctx.allowed_visibilities),
    ]
    if not ctx.include_hidden:
        doc_cond.append(KbDocumentAccess.hidden.is_(False))

    has_valid_evidence = select(KbAssertionEvidence.assertion_id).select_from(
        KbAssertionEvidence
    ).join(KbEvidence, KbEvidence.id == KbAssertionEvidence.evidence_id).join(
        KbPassage, KbPassage.id == KbEvidence.passage_id
    ).join(KbRevision, KbRevision.id == KbPassage.revision_id).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(*doc_cond)).exists()

    return and_(base_cond, has_valid_evidence)


def relation_access_filter(ctx: KbAccessContext, relation_model: Any = None):
    """KbRelation 대상 접근 제어 필터 절 생성."""
    from ttakkari.collector.control_models import KbDocumentAccess
    from ttakkari.kb_models import KbEvidence, KbPassage, KbRevision
    from ttakkari.kb_ontology_models import KbRelation, KbRelationEvidence

    r = relation_model if relation_model is not None else KbRelation

    base_cond = and_(
        r.visibility.in_(ctx.allowed_visibilities),
        r.status == "active",
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
        KbDocumentAccess.tenant_id == ctx.tenant_id,
        KbDocumentAccess.visibility.in_(ctx.allowed_visibilities),
    ]
    if not ctx.include_hidden:
        doc_cond.append(KbDocumentAccess.hidden.is_(False))

    has_valid_evidence = select(KbRelationEvidence.relation_id).select_from(
        KbRelationEvidence
    ).join(KbEvidence, KbEvidence.id == KbRelationEvidence.evidence_id).join(
        KbPassage, KbPassage.id == KbEvidence.passage_id
    ).join(KbRevision, KbRevision.id == KbPassage.revision_id).join(
        KbDocumentAccess, KbDocumentAccess.document_id == KbRevision.document_id
    ).where(and_(*doc_cond)).exists()

    return and_(base_cond, has_valid_evidence)
