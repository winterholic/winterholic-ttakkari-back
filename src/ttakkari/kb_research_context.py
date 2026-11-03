"""연구 맥락 자료(research_text 중 재사용 지식이 아닌 것)를 지식도서관 접근 규칙에서 제외하는 단일 정의.

연구 맥락은 연구 모듈에서 필요할 때 직접 찾으면 되므로 도서관 목록·검색·온톨로지·충돌·정본·임베딩 어디에도 나오지 않는다.
kb_access.document_access_conditions 가 이 조건을 모든 접근 필터에 한 번에 붙인다(문서·패시지·엔터티·별칭·주장·관계).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import and_, func, literal, not_, or_, select
from sqlalchemy.orm import aliased


def source_is_research_context(source_model: Any):
    """원천이 연구 맥락 전용이면 참. library_purpose 가 knowledge_reference 이거나(구 자료는 외부 URL 이면) 재사용 지식이다."""
    purpose = source_model.origin_meta["library_purpose"].astext
    # 정규식(~*)은 Postgres 전용이라 시험(SQLite)에서 깨진다. http(s):// 로 시작하는 외부 주소는 LIKE 로 같은 뜻을 낸다.
    locator = func.lower(source_model.locator)
    legacy_external = func.coalesce(or_(locator.like("http://%"), locator.like("https://%")), False)
    reference = or_(purpose == "knowledge_reference", and_(purpose.is_(None), legacy_external))
    return and_(source_model.kind == "research_text", not_(func.coalesce(reference, False)))


def _current_revision_condition(rev: Any):
    """별칭 rev 가 문서의 현재 리비전이면 참(kb_ontology.currency.current_revision_filter 와 같은 정의, 별칭을 받는다)."""
    from ttakkari.collector.control_models import KbObservation
    from ttakkari.kb_models import KbRevision

    newer = aliased(KbRevision, name="rc_newer")
    obs = aliased(KbObservation, name="rc_obs")
    observed = (
        select(obs.revision_id)
        .where(obs.document_id == rev.document_id)
        .order_by(obs.event_time.desc(), obs.recorded_at.desc(), obs.id.desc())
        .limit(1)
        .correlate(rev)
        .scalar_subquery()
    )
    legacy = ~select(literal(1)).where(newer.document_id == rev.document_id, newer.rev_no > rev.rev_no).exists()
    return or_(and_(observed.is_not(None), rev.id == observed), and_(observed.is_(None), legacy))


def document_is_research_context(document_id_column: Any):
    """문서의 현재 리비전 원천이 연구 맥락이면 참(main 의 research_context_filter 와 같은 뜻).

    연구 맥락이던 문서가 나중에 참고자료로 바뀌면 도서관에 나타나고, 반대면 사라진다. 안쪽은 전부 별칭이라 바깥 쿼리에
    같은 테이블이 있어도 섞이지 않고, 바깥에서는 문서 id 컬럼 하나만 자동 상관으로 참조한다."""
    from ttakkari.kb_models import KbRevision, KbSource

    rev = aliased(KbRevision, name="rc_rev")
    src = aliased(KbSource, name="rc_src")
    return (
        select(rev.id)
        .join(src, src.id == rev.source_id)
        .where(rev.document_id == document_id_column, _current_revision_condition(rev), source_is_research_context(src))
        .exists()
    )


def revision_is_research_context(revision_id_column: Any):
    """리비전의 원천이 연구 맥락이면 참. 문서 현재 리비전이 참고자료여도 옛 연구 맥락 리비전의 패시지는 가린다."""
    from ttakkari.kb_models import KbRevision, KbSource

    rev = aliased(KbRevision, name="rc_rev2")
    src = aliased(KbSource, name="rc_src2")
    return (
        select(rev.id)
        .join(src, src.id == rev.source_id)
        .where(rev.id == revision_id_column, source_is_research_context(src))
        .exists()
    )


def document_is_covered_alias(document_id_column: Any):
    """본문이 canonical 문서에 모두 있는 병합 중복(covered)이면 참. 목록·검색에는 한 번만 보이게 뺀다."""
    from ttakkari.collector.control_models import KbDocumentAlias

    alias = aliased(KbDocumentAlias, name="rc_alias")
    return select(alias.document_id).where(alias.document_id == document_id_column, alias.relation == "covered").exists()


def revision_is_covered_alias(revision_id_column: Any):
    """리비전이 병합 중복(covered) 문서에 속하면 참."""
    from ttakkari.collector.control_models import KbDocumentAlias
    from ttakkari.kb_models import KbRevision

    alias = aliased(KbDocumentAlias, name="rc_alias2")
    rev = aliased(KbRevision, name="rc_rev3")
    return (
        select(alias.document_id)
        .join(rev, rev.document_id == alias.document_id)
        .where(rev.id == revision_id_column, alias.relation == "covered")
        .exists()
    )
