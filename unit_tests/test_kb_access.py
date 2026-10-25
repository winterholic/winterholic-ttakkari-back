"""KB v3 통합 접근 제어 및 권한 컨텍스트 단위 테스트 (tests/test_kb_access.py)."""

from __future__ import annotations

import pytest

from ttakkari.kb_access import (
    ALL_VISIBILITIES,
    VISIBILITY_GLOBAL,
    VISIBILITY_WORK_ONLY,
    KbAccessContext,
    assertion_access_filter,
    create_owner_context,
    create_personal_agent_context,
    create_work_agent_context,
    document_access_filter,
    entity_access_filter,
    entity_alias_access_filter,
    from_agent_pool,
    from_legacy_scope,
    relation_access_filter,
)
from ttakkari.kb_models import KbDocument
from ttakkari.kb_ontology_models import KbAssertion, KbEntity, KbEntityAlias, KbRelation


def test_kb_access_context_roles_and_visibilities():
    # 1. owner context: tenant 내 global 및 work_only 모두 허용
    owner_ctx = create_owner_context(tenant_id="owner")
    assert owner_ctx.caller_role == "owner"
    assert owner_ctx.tenant_id == "owner"
    assert owner_ctx.allowed_visibilities == (VISIBILITY_GLOBAL, VISIBILITY_WORK_ONLY)
    assert owner_ctx.can_access_visibility(VISIBILITY_GLOBAL) is True
    assert owner_ctx.can_access_visibility(VISIBILITY_WORK_ONLY) is True
    assert owner_ctx.is_work_accessible() is True
    assert owner_ctx.include_hidden is False
    assert owner_ctx.strict_provenance is True

    # 2. work_agent context: global + work_only 허용
    work_agent_ctx = create_work_agent_context(tenant_id="owner")
    assert work_agent_ctx.caller_role == "work_agent"
    assert work_agent_ctx.allowed_visibilities == (VISIBILITY_GLOBAL, VISIBILITY_WORK_ONLY)
    assert work_agent_ctx.is_work_accessible() is True

    # 3. personal_agent context: global 만 허용 (work_only 차단)
    personal_agent_ctx = create_personal_agent_context(tenant_id="owner")
    assert personal_agent_ctx.caller_role == "personal_agent"
    assert personal_agent_ctx.allowed_visibilities == (VISIBILITY_GLOBAL,)
    assert personal_agent_ctx.can_access_visibility(VISIBILITY_GLOBAL) is True
    assert personal_agent_ctx.can_access_visibility(VISIBILITY_WORK_ONLY) is False
    assert personal_agent_ctx.is_work_accessible() is False


def test_from_agent_pool():
    work_ctx = from_agent_pool("work", tenant_id="t1")
    assert work_ctx.caller_role == "work_agent"
    assert work_ctx.tenant_id == "t1"
    assert work_ctx.is_work_accessible() is True

    personal_ctx = from_agent_pool("personal", tenant_id="t1")
    assert personal_ctx.caller_role == "personal_agent"
    assert personal_ctx.is_work_accessible() is False

    unknown_ctx = from_agent_pool(None, tenant_id="t1")
    assert unknown_ctx.caller_role == "personal_agent"
    assert unknown_ctx.is_work_accessible() is False


def test_from_legacy_scope_security_boundary():
    # legacy 'work' -> work_only 만 허용
    work_ctx = from_legacy_scope("work", tenant_id="owner")
    assert work_ctx.caller_role == "work_agent"
    assert work_ctx.allowed_visibilities == (VISIBILITY_WORK_ONLY,)
    assert work_ctx.can_access_visibility(VISIBILITY_GLOBAL) is False
    assert work_ctx.can_access_visibility(VISIBILITY_WORK_ONLY) is True

    # legacy 'personal' -> global 만 허용
    personal_ctx = from_legacy_scope("personal", tenant_id="owner")
    assert personal_ctx.caller_role == "personal_agent"
    assert personal_ctx.allowed_visibilities == (VISIBILITY_GLOBAL,)
    assert personal_ctx.can_access_visibility(VISIBILITY_WORK_ONLY) is False

    # caller 가 임의로 'all' 을 보내도 보안 경계를 우회하지 못하고 global only(fail-closed)로 제한
    all_ctx = from_legacy_scope("all", tenant_id="owner")
    assert all_ctx.caller_role == "personal_agent"
    assert all_ctx.allowed_visibilities == (VISIBILITY_GLOBAL,)
    assert all_ctx.is_work_accessible() is False


def test_document_access_filter_clause():
    owner_ctx = create_owner_context(tenant_id="owner")
    clause = document_access_filter(owner_ctx, KbDocument)
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "kb_document_access" in compiled
    assert "tenant_id = 'owner'" in compiled
    assert "visibility in ('global', 'work_only')" in compiled
    assert "hidden is false" in compiled

    personal_ctx = create_personal_agent_context(tenant_id="team_a")
    clause_personal = document_access_filter(personal_ctx, KbDocument)
    compiled_personal = str(clause_personal.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "tenant_id = 'team_a'" in compiled_personal
    assert "visibility in ('global')" in compiled_personal


def test_entity_access_filter_strict_provenance():
    # strict_provenance=True 일 때 문서 조인 검증이 결합되는지 확인
    owner_ctx = create_owner_context(tenant_id="owner")
    clause = entity_access_filter(owner_ctx, KbEntity)
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True}))
    assert "kb_entities" in compiled
    assert "kb_document_entities" in compiled
    assert "kb_document_access" in compiled
    assert "tenant_id = 'owner'" in compiled

    # strict_provenance=False 일 때는 기본 visibility 만 검사
    loose_ctx = KbAccessContext(
        tenant_id="owner",
        caller_role="owner",
        allowed_visibilities=ALL_VISIBILITIES,
        strict_provenance=False,
    )
    clause_loose = entity_access_filter(loose_ctx, KbEntity)
    compiled_loose = str(clause_loose.compile(compile_kwargs={"literal_binds": True}))
    assert "kb_document_entities" not in compiled_loose
    # 엔터티 행의 tenant 는 온톨로지 네임스페이스("personal")이지 문서 접근 tenant("owner")가 아니다.
    assert "kb_entities.tenant_id = 'personal'" in compiled_loose
    assert "kb_entities.tenant_id = 'owner'" not in compiled_loose


def test_alias_access_filter_requires_match_in_an_authorized_source_passage():
    ctx = create_personal_agent_context(tenant_id="team_a")
    clause = entity_alias_access_filter(ctx, KbEntity, KbEntityAlias)
    compiled = str(clause.compile(compile_kwargs={"literal_binds": True})).lower()
    assert "kb_document_entities" in compiled
    assert "kb_passages.body" in compiled
    assert "strpos" in compiled
    assert "tenant_id = 'team_a'" in compiled
    # seed/user_verified 별칭은 문서 출현 근거를 요구하지 않고, extracted 별칭만 요구한다
    assert "kb_entity_aliases.source in ('seed', 'user_verified')" in compiled


def test_relation_and_assertion_access_filters():
    ctx = create_owner_context(tenant_id="owner")

    # 관계 필터는 active 상태 및 evidence_count >= 1 검사
    rel_clause = relation_access_filter(ctx, KbRelation)
    compiled_rel = str(rel_clause.compile(compile_kwargs={"literal_binds": True}))
    assert "status = 'active'" in compiled_rel
    assert "evidence_count >= 1" in compiled_rel
    assert "kb_relation_evidence" in compiled_rel

    # 주장 필터는 증거-패시지-문서 접근 검사
    assert_clause = assertion_access_filter(ctx, KbAssertion)
    compiled_assert = str(assert_clause.compile(compile_kwargs={"literal_binds": True}))
    assert "kb_assertion_evidence" in compiled_assert
    assert "kb_passages" in compiled_assert


def test_from_caller_visibility_maps_ontology_callers_onto_the_single_access_rule():
    from ttakkari.kb_access import document_access_conditions, from_caller_visibility

    personal = from_caller_visibility("global")
    work = from_caller_visibility("work_only", ontology_tenant_id="personal")
    assert personal.allowed_visibilities == (VISIBILITY_GLOBAL,) and personal.caller_role == "personal_agent"
    assert work.allowed_visibilities == ALL_VISIBILITIES and work.caller_role == "work_agent"
    assert personal.tenant_id == "owner" and personal.ontology_tenant_id == "personal"
    for forged in ("all", "", "work", None):
        with pytest.raises(ValueError):
            from_caller_visibility(forged)  # type: ignore[arg-type]

    from ttakkari.collector.control_models import KbDocumentAccess

    compiled = " AND ".join(
        str(c.compile(compile_kwargs={"literal_binds": True})) for c in document_access_conditions(personal, KbDocumentAccess)
    ).lower()
    assert "tenant_id = 'owner'" in compiled and "visibility in ('global')" in compiled and "hidden is false" in compiled
