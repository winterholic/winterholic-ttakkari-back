"""지식 도서관(KB v3) 다중 문서 통합 정본 요약 합성기 (kb_entity_canonicals).

원칙:
1. 불변 원장 보호: 원장(kb_sources, kb_documents, kb_revisions, kb_passages, kb_evidence)은 절대 수정·삭제하지 않는다.
   리비전 갱신(supersedes_id) 시 구버전 주장의 superseded 전환은 파생 온톨로지(kb_assertions)의 status 만 변경한다.
2. 사실 근거 결속: 실제 KbAssertion, KbAssertionEvidence, KbEvidence, KbPassage, KbRevision 로부터만 정본을 합성하며,
   인용할 수 없는 무근거 진실을 임의로 생성하지 않는다. 모든 핵심 주장은 실제 assertion_id, evidence_id, document_id 를 보존한다.
3. 멱등성 및 버전 보존: 정본 카드의 내용(정의, 핵심 사실, 출처 문서, 지지 근거, 충돌 유무)이 실제로 변경될 때만 version_no 가 증가하며,
   동일 입력에 대한 재실행은 version_no 와 내용을 그대로 보존한다.
4. 가시성 격리(fail-closed): global 엔터티의 정본 카드는 global 원천 문서 및 global 주장으로만 합성한다.
   work_only 주장은 global 정본 카드에 혼입되거나 global 엔터티의 메타데이터를 변경하여 개인 세션에 민감 정보를 누출하지 않는다.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess, KbObservation
from ttakkari.kb_models import KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityCanonical,
)
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

__all__ = [
    "get_active_assertions_for_entity",
    "rebuild_all_canonicals",
    "rebuild_canonical",
    "synthesize_canonical_for_entity",
    "update_canonicals_for_revision",
    "update_superseded_assertions",
]


async def update_superseded_assertions(
    db: AsyncSession, revision_id: uuid.UUID
) -> int:
    """새 리비전이 구버전 리비전을 대체(supersedes_id 또는 rev_no 갱신)할 때 과거 주장을 superseded 로 전환.

    원장(kb_revisions, kb_passages, kb_evidence)은 절대 UPDATE/DELETE 하지 않으며,
    파생 프로젝션인 kb_assertions.status 만 변경한다.
    """
    rev = await db.get(KbRevision, revision_id)
    if rev is None:
        return 0

    older_rev_ids: set[uuid.UUID] = set()

    # 1. 명시적 supersedes_id 가 있는 경우
    if rev.supersedes_id is not None:
        older_rev_ids.add(rev.supersedes_id)

    # 2. 동일 문서의 더 낮은 rev_no 리비전들
    older_rev_rows = await db.scalars(
        select(KbRevision.id).where(
            KbRevision.document_id == rev.document_id,
            KbRevision.rev_no < rev.rev_no,
        )
    )
    for oid in older_rev_rows:
        older_rev_ids.add(oid)

    if not older_rev_ids:
        return 0

    # 과거 리비전에 속한 active 주장들을 superseded 로 전환
    older_assertions = list(
        await db.scalars(
            select(KbAssertion).where(
                KbAssertion.revision_id.in_(list(older_rev_ids)),
                KbAssertion.status == "active",
            )
        )
    )
    for assertion in older_assertions:
        assertion.status = "superseded"
        assertion.is_canonical = False

    if older_assertions:
        log.info(
            "revision %s superseded %d assertions from older revisions %s",
            revision_id,
            len(older_assertions),
            older_rev_ids,
        )
    return len(older_assertions)


def _current_revision_subquery(document_id: Any) -> Any:
    """kb_library._current_revision_filter 와 동등한 최신 리비전 ID 결정 서브쿼리."""
    obs = KbObservation.__table__.alias("obs_curr")
    newer = KbRevision.__table__.alias("newer_rev")

    observed = (
        select(obs.c.revision_id)
        .where(obs.c.document_id == document_id)
        .order_by(
            obs.c.event_time.desc(),
            obs.c.recorded_at.desc(),
            obs.c.id.desc(),
        )
        .limit(1)
        .scalar_subquery()
    )

    latest_legacy = (
        select(newer.c.id)
        .where(
            newer.c.document_id == document_id,
        )
        .order_by(newer.c.rev_no.desc())
        .limit(1)
        .scalar_subquery()
    )

    return func.coalesce(observed, latest_legacy)


async def get_active_assertions_for_entity(
    db: AsyncSession,
    entity: KbEntity,
) -> list[tuple[KbAssertion, uuid.UUID]]:
    """엔터티의 정본 합성에 적격한 활성 주장과 그 지지 근거 ID 목록을 반환한다.

    적격 조건:
    1. subject_entity_id == entity.id
    2. status == 'active' (disputed, superseded, rejected 제외)
    3. trust_score >= 0.5
    4. 실제 검증된 근거(kb_assertion_evidence -> kb_evidence)가 최소 1개 이상 결속되어 있을 것
    5. 소속 문서가 숨김(hidden=True)이 아니며, 현재 리비전일 것
    6. 가시성 격리:
       - entity.visibility == 'global' 이면, 문서 접근 및 주장 모두 'global' 이어야 함.
       - entity.visibility == 'work_only' 이면, 'work_only' 및 'global' 모두 허용.
    """
    stmt = (
        select(KbAssertion, KbAssertionEvidence.evidence_id)
        .join(
            KbAssertionEvidence,
            KbAssertionEvidence.assertion_id == KbAssertion.id,
        )
        .join(KbEvidence, KbEvidence.id == KbAssertionEvidence.evidence_id)
        .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .join(KbDocumentAccess, KbDocumentAccess.document_id == KbAssertion.document_id)
        .where(
            KbAssertion.subject_entity_id == entity.id,
            KbAssertion.status == "active",
            KbAssertion.trust_score >= 0.5,
            KbAssertionEvidence.relation == "supports",
            KbRevision.id == KbAssertion.revision_id,
            KbRevision.document_id == KbAssertion.document_id,
            KbDocumentAccess.tenant_id == "owner",
            KbDocumentAccess.hidden.is_(False),
        )
    )

    if entity.visibility == "global":
        stmt = stmt.where(
            KbAssertion.visibility == "global",
            KbDocumentAccess.visibility == "global",
            KbDocumentAccess.origin_scope != "work",
        )
    else:
        stmt = stmt.where(
            KbAssertion.visibility.in_(("global", "work_only")),
        )

    stmt = stmt.order_by(KbAssertion.id, KbEvidence.id)
    rows = list((await db.execute(stmt)).all())
    if not rows:
        return []

    # 문서별 현재 리비전 검증 (메모리에서 캐싱하여 최신 리비전 아닌 주장 필터링)
    doc_ids = {a.document_id for a, _ in rows}
    current_rev_map: dict[uuid.UUID, uuid.UUID | None] = {}
    for did in doc_ids:
        # observation 확인
        latest_obs = await db.scalar(
            select(KbObservation.revision_id)
            .where(KbObservation.document_id == did)
            .order_by(
                KbObservation.event_time.desc(),
                KbObservation.recorded_at.desc(),
                KbObservation.id.desc(),
            )
            .limit(1)
        )
        if latest_obs is not None:
            current_rev_map[did] = latest_obs
        else:
            # rev_no 최대치
            latest_rev = await db.scalar(
                select(KbRevision.id)
                .where(KbRevision.document_id == did)
                .order_by(KbRevision.rev_no.desc())
                .limit(1)
            )
            current_rev_map[did] = latest_rev

    # 현재 리비전에 해당하는 주장들만 선별
    valid_pairs: list[tuple[KbAssertion, uuid.UUID]] = []
    seen_assertions: set[uuid.UUID] = set()

    for assertion, evidence_id in rows:
        expected_curr = current_rev_map.get(assertion.document_id)
        if expected_curr is not None and assertion.revision_id != expected_curr:
            continue
        if assertion.id not in seen_assertions:
            seen_assertions.add(assertion.id)
            valid_pairs.append((assertion, evidence_id))

    # 신뢰도 및 최신성 정렬
    valid_pairs.sort(
        key=lambda pair: (
            pair[0].trust_score,
            pair[0].freshness_ts,
            pair[0].created_at,
            str(pair[0].id),
            str(pair[1]),
        ),
        reverse=True,
    )
    return valid_pairs


async def synthesize_canonical_for_entity(
    db: AsyncSession,
    entity_id: uuid.UUID,
) -> KbEntityCanonical | None:
    """단일 엔터티에 대해 다중 문서 통합 정본 카드(KbEntityCanonical)를 합성 또는 갱신한다.

    반환: 갱신되거나 생성된 KbEntityCanonical, 엔터티가 비활성이면 None.
    멱등성: 내용 변화가 없으면 version_no 를 증가시키지 않고 기존 행을 유지한다.
    """
    entity = await db.get(KbEntity, entity_id)
    if entity is None or entity.status != "active":
        return None

    # 1. 미해결 충돌(open conflict) 유무 확인
    conflict_stmt = select(func.count(KbConflict.id)).where(
        KbConflict.entity_id == entity.id,
        KbConflict.status == "open",
    )
    if entity.visibility == "global":
        conflict_stmt = conflict_stmt.where(KbConflict.visibility == "global")

    open_conflicts_count = await db.scalar(conflict_stmt) or 0
    has_open_conflict = open_conflicts_count > 0

    # 2. 적격 활성 주장 조회
    pairs = await get_active_assertions_for_entity(db, entity)

    # 3. 정본 정의(canonical_definition) 선정
    # 우선순위: predicate == 'definition' -> 'purpose' -> entity.summary -> 빈 문자열
    definition_assertion: KbAssertion | None = None
    for a, _ in pairs:
        if a.predicate == "definition":
            definition_assertion = a
            break
    if definition_assertion is None:
        for a, _ in pairs:
            if a.predicate == "purpose":
                definition_assertion = a
                break

    if definition_assertion is not None:
        canonical_definition = definition_assertion.object_value.strip()
    else:
        # 엔터티 요약은 원문 인용이 보장되지 않으므로 정본 사실로 승격하지 않는다.
        canonical_definition = ""

    # 4. 핵심 사실(key_facts) 구성 (술어별 대표 주장 및 실제 인용 ID 보존)
    # 술어별 최고 신뢰도 주장 1개씩 선별
    selected_by_predicate: dict[str, tuple[KbAssertion, uuid.UUID]] = {}
    for a, ev_id in pairs:
        if a.predicate not in selected_by_predicate:
            selected_by_predicate[a.predicate] = (a, ev_id)

    key_facts: list[dict[str, Any]] = []
    canonical_assertion_ids: set[uuid.UUID] = set()
    if definition_assertion is not None:
        canonical_assertion_ids.add(definition_assertion.id)

    for a, ev_id in selected_by_predicate.values():
        key_facts.append(
            {
                "predicate": a.predicate,
                "value": a.object_value,
                "norm_value": a.norm_object_value,
                "modality": a.modality,
                "assertion_id": str(a.id),
                "evidence_id": str(ev_id),
                "document_id": str(a.document_id),
            }
        )
        canonical_assertion_ids.add(a.id)

    # 대표 주장 플래그(is_canonical) 동기화
    for a, _ in pairs:
        a.is_canonical = a.id in canonical_assertion_ids

    # 술어 알파벳순 정렬로 결정론적 JSON 순서 보장
    key_facts.sort(key=lambda item: item["predicate"])

    # 5. 출처 문서 및 지지 근거 ID 집합 (문자열 리스트, 정렬 보장)
    source_document_ids = sorted(
        {str(a.document_id) for a, _ in pairs}
    )
    supporting_evidence_ids = sorted(
        {str(ev_id) for _, ev_id in pairs}
    )

    # 6. 기존 정본 카드 조회 및 내용 변화 검사
    existing = await db.get(KbEntityCanonical, entity.id)
    if existing is not None:
        content_changed = (
            existing.canonical_definition != canonical_definition
            or existing.key_facts != key_facts
            or existing.source_document_ids != source_document_ids
            or existing.supporting_evidence_ids != supporting_evidence_ids
            or existing.has_open_conflict != has_open_conflict
            or existing.visibility != entity.visibility
        )

        if content_changed:
            existing.canonical_definition = canonical_definition
            existing.key_facts = key_facts
            existing.source_document_ids = source_document_ids
            existing.supporting_evidence_ids = supporting_evidence_ids
            existing.has_open_conflict = has_open_conflict
            existing.visibility = entity.visibility
            existing.version_no = existing.version_no + 1
            existing.updated_at = utcnow()
            log.info(
                "entity %s canonical updated to version %d",
                entity.id,
                existing.version_no,
            )
        return existing

    # 신규 정본 카드 생성
    canonical = KbEntityCanonical(
        entity_id=entity.id,
        visibility=entity.visibility,
        canonical_definition=canonical_definition,
        key_facts=key_facts,
        source_document_ids=source_document_ids,
        supporting_evidence_ids=supporting_evidence_ids,
        has_open_conflict=has_open_conflict,
        version_no=1,
        updated_at=utcnow(),
    )
    db.add(canonical)
    log.info("entity %s canonical created version 1", entity.id)
    return canonical


async def update_canonicals_for_revision(
    db: AsyncSession, revision_id: uuid.UUID
) -> list[KbEntityCanonical]:
    """주어진 리비전으로 인해 영향을 받은 모든 엔터티들의 정본 카드를 증분 갱신한다.

    1. 리비전에서 언급된 엔터티(kb_document_entities)
    2. 리비전에서 추출된 주장의 주어/목적어 엔터티(kb_assertions)
    """
    rev = await db.get(KbRevision, revision_id)
    if rev is None:
        return []

    # 과거 리비전 대체 처리
    await update_superseded_assertions(db, revision_id)

    affected_entity_ids: set[uuid.UUID] = set()

    # 1. 문서 엔터티 언급
    doc_entity_rows = await db.scalars(
        select(KbDocumentEntity.entity_id).where(
            KbDocumentEntity.revision_id == revision_id
        )
    )
    for eid in doc_entity_rows:
        affected_entity_ids.add(eid)

    # 2. 주장 주어 및 목적어 엔터티
    assertion_rows = list(
        await db.scalars(
            select(KbAssertion).where(KbAssertion.revision_id == revision_id)
        )
    )
    for a in assertion_rows:
        affected_entity_ids.add(a.subject_entity_id)
        if a.object_entity_id is not None:
            affected_entity_ids.add(a.object_entity_id)

    # 3. 만약 구버전이 대체되었다면 구버전의 주장 주어 엔터티도 갱신 대상에 포함
    if rev.supersedes_id is not None:
        older_assertions = list(
            await db.scalars(
                select(KbAssertion.subject_entity_id).where(
                    KbAssertion.revision_id == rev.supersedes_id
                )
            )
        )
        for eid in older_assertions:
            affected_entity_ids.add(eid)

    updated: list[KbEntityCanonical] = []
    for eid in affected_entity_ids:
        card = await synthesize_canonical_for_entity(db, eid)
        if card is not None:
            updated.append(card)

    return updated


async def rebuild_canonical(
    db: AsyncSession, entity_id: uuid.UUID
) -> KbEntityCanonical | None:
    """단일 엔터티의 정본 요약 카드를 강제 재합성한다."""
    return await synthesize_canonical_for_entity(db, entity_id)


async def rebuild_all_canonicals(
    db: AsyncSession, *, tenant_id: str = "personal"
) -> int:
    """전체 활성 엔터티의 정본 요약 카드를 일괄 재합성한다."""
    entity_ids = list(
        await db.scalars(
            select(KbEntity.id).where(
                KbEntity.tenant_id == tenant_id,
                KbEntity.status == "active",
            )
        )
    )
    count = 0
    for eid in entity_ids:
        card = await synthesize_canonical_for_entity(db, eid)
        if card is not None:
            count += 1
    return count
