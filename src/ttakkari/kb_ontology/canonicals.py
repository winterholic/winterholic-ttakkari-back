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

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_access import (
    create_owner_context,
    create_personal_agent_context,
    document_access_conditions,
)
from ttakkari.kb_models import KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology.currency import (
    current_revision_id,
    current_revision_ids,
    older_revision_ids,
)
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


SUPERSESSION_RESOLVER = "system:revision_supersession"


async def update_superseded_assertions(
    db: AsyncSession, revision_id: uuid.UUID
) -> int:
    """새 리비전 또는 재관측된 리비전으로 인해 대체되는 과거 주장을 superseded 로 전환하고,
    현재 리비전의 주장은 active 로 유지/복원한다.

    원장(kb_revisions, kb_passages, kb_evidence)은 절대 UPDATE/DELETE 하지 않으며,
    파생 프로젝션인 kb_assertions.status 와 kb_conflicts 만 변경한다.
    충돌 당사자라 disputed 로 남아 있던 구버전 주장도 대체한다. 그 주장이 당사자인 열린 충돌은 닫는다(아래).
    """
    rev = await db.get(KbRevision, revision_id)
    if rev is None:
        return 0

    curr_id = await current_revision_id(db, rev.document_id)
    if curr_id is None:
        return 0

    # 1. 만약 rev 가 문서의 현재 리비전인 경우:
    if curr_id == rev.id:
        # A->B->A 복귀 시 이전에 superseded 처리되었을 수 있는 현재 리비전의 주장을 active 로 복원
        restored = list(
            await db.scalars(
                select(KbAssertion).where(
                    KbAssertion.revision_id == rev.id,
                    KbAssertion.status == "superseded",
                )
            )
        )
        for assertion in restored:
            assertion.status = "active"

        # 동일 문서의 다른 모든 과거 리비전의 주장을 superseded 로 전환
        older_rev_ids = await older_revision_ids(db, rev)
        if not older_rev_ids:
            return len(restored)

        older_assertions = list(
            await db.scalars(
                select(KbAssertion).where(
                    KbAssertion.revision_id.in_(list(older_rev_ids)),
                    KbAssertion.status.in_(("active", "disputed")),
                )
            )
        )
        for assertion in older_assertions:
            assertion.status = "superseded"
            assertion.is_canonical = False

        if older_assertions:
            await _close_conflicts_of_superseded(db, {a.id for a in older_assertions})
            log.info(
                "revision %s superseded %d assertions from older revisions %s",
                revision_id,
                len(older_assertions),
                older_rev_ids,
            )
        return len(older_assertions) + len(restored)

    # 2. 만약 rev 가 현재 리비전이 아닌 경우 (오래된 스냅샷 늦은 수집 등):
    # rev 자신의 active/disputed 주장은 비현재 리비전이므로 superseded 로 전환한다.
    # 현재 리비전(curr_id)의 주장은 절대 건드리지 않는다.
    rev_assertions = list(
        await db.scalars(
            select(KbAssertion).where(
                KbAssertion.revision_id == rev.id,
                KbAssertion.status.in_(("active", "disputed")),
            )
        )
    )
    for assertion in rev_assertions:
        assertion.status = "superseded"
        assertion.is_canonical = False

    if rev_assertions:
        await _close_conflicts_of_superseded(db, {a.id for a in rev_assertions})
        log.info(
            "non-current revision %s assertions (%d) set to superseded",
            revision_id,
            len(rev_assertions),
        )
    return len(rev_assertions)


async def _close_conflicts_of_superseded(
    db: AsyncSession, superseded_ids: set[uuid.UUID]
) -> list[KbConflict]:
    """당사자 주장이 대체된 열린 충돌을 닫는다.

    - 한쪽 당사자만 대체된 경우: 살아남은 당사자 쪽으로 resolved_a / resolved_b 로 전환.
      살아남은 당사자가 다른 열린 충돌에 걸려 있지 않으면 disputed 에서 active 로 되돌린다.
    - 양쪽 당사자가 모두 대체된 경우: resolved_both_valid 는 둘 다 유효하다는 오해를 주므로
      사용하지 않고, 기존 상태값 중 특정 주장을 채택하지 않고 종결함을 나타내는 kept_separate 를 쓴다.
    - 사유는 resolution_note ('both_parties_superseded_by_newer_revision' 등)에 남기고,
      resolved_by 에 SUPERSESSION_RESOLVER 를 남긴다.
    """
    conflicts = list(
        await db.scalars(
            select(KbConflict).where(
                KbConflict.status == "open",
                or_(
                    KbConflict.assertion_a_id.in_(superseded_ids),
                    KbConflict.assertion_b_id.in_(superseded_ids),
                ),
            )
        )
    )
    now = utcnow()
    survivors: set[uuid.UUID] = set()
    for conflict in conflicts:
        a_gone = conflict.assertion_a_id in superseded_ids
        b_gone = conflict.assertion_b_id in superseded_ids
        if a_gone and b_gone:
            conflict.status = "kept_separate"
            note = "both_parties_superseded_by_newer_revision"
        elif a_gone:
            conflict.status = "resolved_b"
            note = "party_a_superseded_by_newer_revision"
            if conflict.assertion_b_id is not None:
                survivors.add(conflict.assertion_b_id)
        else:
            conflict.status = "resolved_a"
            note = "party_b_superseded_by_newer_revision"
            if conflict.assertion_a_id is not None:
                survivors.add(conflict.assertion_a_id)
        conflict.resolution_note = note
        conflict.resolved_by = SUPERSESSION_RESOLVER
        conflict.resolved_at = now
    if not conflicts:
        return []
    await db.flush()

    for survivor_id in survivors:
        survivor = await db.get(KbAssertion, survivor_id)
        if survivor is None or survivor.status != "disputed":
            continue
        still_open = await db.scalar(
            select(func.count(KbConflict.id)).where(
                KbConflict.status == "open",
                or_(
                    KbConflict.assertion_a_id == survivor_id,
                    KbConflict.assertion_b_id == survivor_id,
                ),
            )
        )
        if not still_open:
            survivor.status = "active"
    log.info("closed %d conflicts whose party assertion was superseded", len(conflicts))
    return conflicts


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
        )
    )

    # global 엔터티의 정본은 global 문서·주장만, work_only 엔터티는 global + work_only 를 kb_access 의 같은 규칙으로 고른다.
    ctx = create_personal_agent_context() if entity.visibility == "global" else create_owner_context()
    stmt = stmt.where(
        *document_access_conditions(ctx, KbDocumentAccess),
        KbAssertion.visibility.in_(ctx.allowed_visibilities),
    )

    stmt = stmt.order_by(KbAssertion.id, KbEvidence.id)
    rows = list((await db.execute(stmt)).all())
    if not rows:
        return []

    # 문서별 현재 리비전은 rev_no 최대(kb_ontology.currency). 현재 리비전이 아닌 리비전의 주장은 정본에서 뺀다.
    current_rev_map = await current_revision_ids(db, {a.document_id for a, _ in rows})

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
    # Two jobs canonicalizing the same entity both saw "no card yet" and both inserted it (kb_entity_canonicals_pkey,
    # 12 failed stages in one collection run). Identity writes of a tenant are serialized for the rest of this transaction.
    from ttakkari.kb_ontology.write_lock import lock_identity_writes

    await lock_identity_writes(db, getattr(entity, "tenant_id", None) or "owner")
    await db.refresh(entity)
    if entity.status != "active":
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

    # 3. 구버전이 대체되었다면 구버전(supersedes_id 와 더 낮은 rev_no 전부)의 주장 주어 엔터티도 갱신 대상에 포함
    older_ids = await older_revision_ids(db, rev)
    if older_ids:
        older_subjects = list(
            await db.scalars(
                select(KbAssertion.subject_entity_id).where(
                    KbAssertion.revision_id.in_(older_ids)
                )
            )
        )
        for eid in older_subjects:
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
