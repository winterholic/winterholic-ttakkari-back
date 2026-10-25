"""지식 도서관(KB v3) 지식 상충 탐지기 및 해결 서비스 (kb_conflicts).

원칙:
1. 조건부 비상충 원칙:
   같은 subject + predicate 라도 적용 조건(applies_condition), 버전, 유효 기간,
   양상(modality: fact vs recommendation 등)이 다르면 단순 값 차이로 충돌 확정하지 않는다.
   동일 조건·양상에서의 상반된 값(value_contradiction) 또는 극성(polarity_contradiction)만 충돌로 탐지한다.
2. 근거·시간 양측 보존:
   충돌이 발생해도 어느 한쪽 주장을 임의로 덮어쓰거나 원장을 훼손하지 않는다.
   양쪽 주장의 status 를 'disputed' 로 전환하고 kb_conflicts 행을 생성하여 검토 상태로 보존한다.
3. 가시성 격리 및 누출 방지(Fail-closed):
   회사(work_only) 주장이 개인(global) 주장과 상충할 때, global 주장의 상태를 'disputed' 로 변경하여
   개인 세션에 비인가된 사내 지식의 존재를 누출해서는 안 된다.
   혼합 상충 시 충돌 행의 visibility 는 'work_only' 이며, global 주장은 개인 시점 'active' 를 유지한다.
4. 해결 서비스 계약(Resolution Service Contract):
   'resolved_a' | 'resolved_b' | 'resolved_both_valid' | 'merged' | 'kept_separate' 계약을 엄격히 준수한다.
   모호 엔터티(ambiguous_entity)는 자동 병합하지 않고 기존 후보를 유지한다.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_access import (
    KbAccessContext,
    create_owner_context,
    document_access_conditions,
    from_caller_visibility,
)
from ttakkari.kb_models import KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology.canonicals import synthesize_canonical_for_entity
from ttakkari.kb_ontology.currency import current_revision_id
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbEntity,
)
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

__all__ = [
    "detect_conflicts_for_entity",
    "detect_conflicts_for_revision",
    "list_conflicts",
    "list_conflicts_with_total",
    "resolve_conflict",
]

VALID_RESOLUTIONS = {
    "resolved_a",
    "resolved_b",
    "resolved_both_valid",
    "merged",
    "kept_separate",
}
MAX_CONFLICT_PAGE_SIZE = 100


def _validate_query(caller_visibility: str, offset: int | None = None, limit: int | None = None) -> None:
    if caller_visibility not in {"global", "work_only"}:
        raise ValueError("caller_visibility must be 'global' or 'work_only'")
    if offset is not None and offset < 0:
        raise ValueError("offset must be non-negative")
    if limit is not None and not 1 <= limit <= MAX_CONFLICT_PAGE_SIZE:
        raise ValueError(f"limit must be between 1 and {MAX_CONFLICT_PAGE_SIZE}")


def _are_conditions_compatible(cond_a: dict[str, Any] | None, cond_b: dict[str, Any] | None) -> bool:
    """두 주장의 적용 조건(applies_condition)이 실질적으로 동일한지 비교한다.

    버전, 운영체제 등 명시된 키의 값이 서로 다르면 상충이 아닌 버전별/환경별 분기이므로 False 를 반환한다.
    """
    a = cond_a or {}
    b = cond_b or {}
    return a == b


def _are_modalities_compatible(mod_a: str, mod_b: str) -> bool:
    """두 주장의 modality 가 상충 비교 가능한지 확인한다.

    fact vs recommendation 처럼 양상이 다르면 상충으로 확정하지 않는다.
    """
    return mod_a == mod_b


def _periods_overlap(rev_a: KbRevision, rev_b: KbRevision) -> bool:
    """Revision 유효시간이 서로 분리되어 있으면 서로 다른 시점의 사실로 취급한다."""
    def as_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    a_start, a_end = as_utc(rev_a.valid_from), as_utc(rev_a.valid_to)
    b_start, b_end = as_utc(rev_b.valid_from), as_utc(rev_b.valid_to)
    return not (
        (a_end is not None and b_start is not None and a_end <= b_start)
        or (b_end is not None and a_start is not None and b_end <= a_start)
    )


async def _current_revision_id(
    db: AsyncSession, document_id: uuid.UUID
) -> uuid.UUID | None:
    """관측 원장 기준 현재 리비전. 주장 생애주기와 같은 kb_ontology.currency 정의를 쓴다."""
    return await current_revision_id(db, document_id)


async def detect_conflicts_for_entity(
    db: AsyncSession,
    entity_id: uuid.UUID,
) -> list[KbConflict]:
    """단일 엔터티에 대해 활성 또는 disputed 상태인 주장들 간의 상충을 탐지하고 kb_conflicts 를 생성한다.

    1. 동일 주어 + 동일 술어
    2. 동일 적용 조건(applies_condition)
    3. 동일 modality
    4. 극성 반대(polarity_contradiction) 또는 동일 극성 내 상이한 목적어 값(value_contradiction)
    """
    entity = await db.get(KbEntity, entity_id)
    if entity is None or entity.status != "active":
        return []

    # 실제 근거와 원문 리비전이 연결되고, 숨겨지지 않은 owner 접근 문서의 주장만 조회.
    stmt = (
        select(KbAssertion, KbRevision)
        .join(KbDocumentAccess, KbDocumentAccess.document_id == KbAssertion.document_id)
        .join(
            KbAssertionEvidence,
            KbAssertionEvidence.assertion_id == KbAssertion.id,
        )
        .join(KbEvidence, KbEvidence.id == KbAssertionEvidence.evidence_id)
        .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
        .join(KbRevision, KbRevision.id == KbPassage.revision_id)
        .where(
            KbAssertion.subject_entity_id == entity.id,
            KbAssertion.status.in_(("active", "disputed")),
            KbAssertionEvidence.relation == "supports",
            KbRevision.id == KbAssertion.revision_id,
            KbRevision.document_id == KbAssertion.document_id,
            # 상충 탐지는 쓰기 쪽이라 owner tenant 의 숨기지 않은 모든 문서를 본다(조회 가시성은 list/resolve 가 kb_access 로 건다).
            *document_access_conditions(create_owner_context(), KbDocumentAccess),
        )
        .distinct()
    )
    rows = list((await db.execute(stmt)).all())
    current_by_document: dict[uuid.UUID, uuid.UUID | None] = {}
    assertions: list[KbAssertion] = []
    revision_by_assertion: dict[uuid.UUID, KbRevision] = {}
    for assertion, revision in rows:
        current = current_by_document.get(assertion.document_id)
        if assertion.document_id not in current_by_document:
            current = await _current_revision_id(db, assertion.document_id)
            current_by_document[assertion.document_id] = current
        if current is not None and revision.id != current:
            continue
        assertions.append(assertion)
        revision_by_assertion[assertion.id] = revision
    if len(assertions) < 2:
        return []

    # 열린 쌍과 사람이 양쪽 유효로 판단한 동일 주장 쌍은 다시 열지 않는다.
    # 새 주장 ID는 별도 쌍이며, 리비전 대체로 자동 종결된 쌍은 복귀 시 재탐지할 수 있다.
    existing_conflicts = list(
        await db.scalars(
            select(KbConflict).where(
                KbConflict.entity_id == entity.id,
                or_(
                    KbConflict.status == "open",
                    and_(
                        KbConflict.status == "resolved_both_valid",
                        or_(
                            KbConflict.resolved_by.is_(None),
                            KbConflict.resolved_by != "system:revision_supersession",
                        ),
                    ),
                ),
            )
        )
    )
    existing_pairs: set[frozenset[uuid.UUID]] = set()
    for c in existing_conflicts:
        if c.assertion_a_id and c.assertion_b_id:
            existing_pairs.add(frozenset({c.assertion_a_id, c.assertion_b_id}))

    new_conflicts: list[KbConflict] = []

    # 술어별 그룹핑
    by_predicate: dict[str, list[KbAssertion]] = {}
    for a in assertions:
        by_predicate.setdefault(a.predicate, []).append(a)

    for pred, group in by_predicate.items():
        if len(group) < 2:
            continue

        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a1 = group[i]
                a2 = group[j]

                # 1. 동일 문서 내 리비전이 다르면 (구버전이 아직 superseded 안 된 경우 등) 비교 제외
                if a1.document_id == a2.document_id and a1.revision_id != a2.revision_id:
                    continue

                # 2. 이미 존재하는 충돌 쌍인지 검사
                pair_key = frozenset({a1.id, a2.id})
                if pair_key in existing_pairs:
                    continue

                # 3. 조건 호환성 (버전·환경 차이 시 상충 금지)
                if not _are_conditions_compatible(a1.applies_condition, a2.applies_condition):
                    continue

                # 4. 양상 호환성 (권고 vs 사실 등 차이 시 상충 금지)
                if not _are_modalities_compatible(a1.modality, a2.modality):
                    continue

                if not _periods_overlap(
                    revision_by_assertion[a1.id], revision_by_assertion[a2.id]
                ):
                    continue

                conflict_kind: str | None = None

                # 5. 극성 반대: polarity_contradiction
                if a1.polarity != a2.polarity:
                    conflict_kind = "polarity_contradiction"
                # 6. 동일 극성 상반 값: value_contradiction
                elif a1.norm_object_value != a2.norm_object_value:
                    conflict_kind = "value_contradiction"

                if conflict_kind is None:
                    continue

                # 7. 가시성 결정 및 개인 세션 누출 방지 경계
                is_work = a1.visibility == "work_only" or a2.visibility == "work_only"
                conflict_visibility = "work_only" if is_work else "global"

                # 주장 상태 disputed 전환
                if conflict_visibility == "global":
                    a1.status = "disputed"
                    a2.status = "disputed"
                    a1.is_canonical = False
                    a2.is_canonical = False
                else:
                    # work_only 충돌: work 주장만 disputed 로 변경
                    # global 주장은 개인 세션에 영향을 주지 않도록 active 유지 (누출 방지)
                    if a1.visibility == "work_only":
                        a1.status = "disputed"
                        a1.is_canonical = False
                    if a2.visibility == "work_only":
                        a2.status = "disputed"
                        a2.is_canonical = False

                conflict = KbConflict(
                    conflict_kind=conflict_kind,
                    entity_id=entity.id,
                    predicate=pred,
                    assertion_a_id=a1.id,
                    assertion_b_id=a2.id,
                    visibility=conflict_visibility,
                    status="open",
                    created_at=utcnow(),
                )
                db.add(conflict)
                existing_pairs.add(pair_key)
                new_conflicts.append(conflict)
                log.info(
                    "detected %s on entity %s predicate %s (assertions %s vs %s, vis=%s)",
                    conflict_kind,
                    entity.id,
                    pred,
                    a1.id,
                    a2.id,
                    conflict_visibility,
                )

    return new_conflicts


async def detect_conflicts_for_revision(
    db: AsyncSession,
    revision_id: uuid.UUID,
) -> list[KbConflict]:
    """리비전에서 다룬 엔터티들에 대해 상충을 탐지한다."""
    subject_ids = list(
        await db.scalars(
            select(KbAssertion.subject_entity_id)
            .where(KbAssertion.revision_id == revision_id)
            .distinct()
        )
    )

    all_conflicts: list[KbConflict] = []
    for eid in subject_ids:
        conflicts = await detect_conflicts_for_entity(db, eid)
        all_conflicts.extend(conflicts)
    return all_conflicts


async def resolve_conflict(
    db: AsyncSession,
    conflict_id: uuid.UUID,
    resolution: str,
    *,
    resolution_note: str | None = None,
    resolved_by: str | None = None,
    tenant_id: str = "personal",
    caller_visibility: str = "global",
    access: KbAccessContext | None = None,
) -> KbConflict:
    """충돌 해결 서비스 API 단일 진입점.

    계약:
    - 'resolved_a': A 주장 채택(active, is_canonical=True), B 주장 기각(rejected, is_canonical=False)
    - 'resolved_b': B 주장 채택(active, is_canonical=True), A 주장 기각(rejected, is_canonical=False)
    - 'resolved_both_valid': 양쪽 주장 모두 유효(active), 정본에 맥락 병기
    - 'merged': 모호 엔터티 병합 승인 (candidate_entity -> entity merged_into_id 설정)
    - 'kept_separate': 모호 엔터티 분리 유지 승인 (candidate_entity active 유지)
    """
    _validate_query(caller_visibility)
    if resolution not in VALID_RESOLUTIONS:
        raise ValueError(
            f"Invalid resolution '{resolution}'. Must be one of {sorted(VALID_RESOLUTIONS)}"
        )

    ctx = access or from_caller_visibility(caller_visibility, ontology_tenant_id=tenant_id)
    conflict = await db.get(KbConflict, conflict_id)
    if conflict is None or not await _conflict_is_accessible(db, conflict, ctx=ctx):
        raise ValueError(f"Conflict {conflict_id} not found")

    assertion_a: KbAssertion | None = None
    assertion_b: KbAssertion | None = None
    if conflict.assertion_a_id:
        assertion_a = await db.get(KbAssertion, conflict.assertion_a_id)
    if conflict.assertion_b_id:
        assertion_b = await db.get(KbAssertion, conflict.assertion_b_id)

    if resolution == "resolved_a":
        if assertion_a is not None:
            assertion_a.status = "active"
            assertion_a.is_canonical = True
        if assertion_b is not None:
            assertion_b.status = "rejected"
            assertion_b.is_canonical = False
        conflict.status = "resolved_a"

    elif resolution == "resolved_b":
        if assertion_b is not None:
            assertion_b.status = "active"
            assertion_b.is_canonical = True
        if assertion_a is not None:
            assertion_a.status = "rejected"
            assertion_a.is_canonical = False
        conflict.status = "resolved_b"

    elif resolution == "resolved_both_valid":
        if assertion_a is not None and assertion_a.status == "disputed":
            assertion_a.status = "active"
        if assertion_b is not None and assertion_b.status == "disputed":
            assertion_b.status = "active"
        conflict.status = "resolved_both_valid"

    elif resolution == "merged":
        if conflict.candidate_entity_id:
            cand = await db.get(KbEntity, conflict.candidate_entity_id)
            if cand is not None:
                cand.status = "merged"
                cand.merged_into_id = conflict.entity_id
        conflict.status = "merged"

    elif resolution == "kept_separate":
        if conflict.candidate_entity_id:
            cand = await db.get(KbEntity, conflict.candidate_entity_id)
            if cand is not None and cand.status == "merged":
                cand.status = "active"
                cand.merged_into_id = None
        conflict.status = "kept_separate"

    conflict.resolution_note = resolution_note
    conflict.resolved_by = resolved_by
    conflict.resolved_at = utcnow()

    # 충돌 해결 후 해당 엔터티의 정본 카드 재합성
    await synthesize_canonical_for_entity(db, conflict.entity_id)

    log.info(
        "conflict %s resolved as %s (by=%s, note=%s)",
        conflict.id,
        conflict.status,
        resolved_by,
        resolution_note,
    )
    return conflict


async def list_conflicts_with_total(
    db: AsyncSession,
    *,
    entity_id: uuid.UUID | None = None,
    status: str | None = "open",
    kind: str | None = None,
    caller_visibility: str = "global",
    tenant_id: str = "personal",
    access: KbAccessContext | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[KbConflict], int]:
    """접근 가능한 충돌 목록과 (페이지 전) 전체 건수. status 가 None 또는 'all' 이면 상태로 거르지 않는다."""
    _validate_query(caller_visibility, offset, limit)
    ctx = access or from_caller_visibility(caller_visibility, ontology_tenant_id=tenant_id)
    stmt = (
        select(KbConflict)
        .join(KbEntity, KbEntity.id == KbConflict.entity_id)
        .where(
            KbEntity.tenant_id == ctx.ontology_tenant_id,
            KbConflict.visibility.in_(ctx.allowed_visibilities),
        )
    )
    if status not in (None, "all"):
        stmt = stmt.where(KbConflict.status == status)
    if kind is not None:
        stmt = stmt.where(KbConflict.conflict_kind == kind)
    if entity_id is not None:
        stmt = stmt.where(KbConflict.entity_id == entity_id)

    candidates = list(await db.scalars(stmt.order_by(KbConflict.created_at.desc(), KbConflict.id)))
    accessible: list[KbConflict] = []
    for conflict in candidates:
        if await _conflict_is_accessible(db, conflict, ctx=ctx):
            accessible.append(conflict)
    return accessible[offset : offset + limit], len(accessible)


async def list_conflicts(
    db: AsyncSession,
    *,
    entity_id: uuid.UUID | None = None,
    status: str = "open",
    caller_visibility: str = "global",
    tenant_id: str = "personal",
    access: KbAccessContext | None = None,
    offset: int = 0,
    limit: int = 50,
) -> list[KbConflict]:
    """충돌 목록을 조회한다 (가시성 격리 준수)."""
    items, _ = await list_conflicts_with_total(
        db, entity_id=entity_id, status=status, caller_visibility=caller_visibility, tenant_id=tenant_id,
        access=access, offset=offset, limit=limit,
    )
    return items


async def _conflict_is_accessible(
    db: AsyncSession,
    conflict: KbConflict,
    *,
    ctx: KbAccessContext,
) -> bool:
    entity = await db.get(KbEntity, conflict.entity_id)
    if entity is None or entity.tenant_id != ctx.ontology_tenant_id:
        return False
    allowed = set(ctx.allowed_visibilities)
    if entity.visibility not in allowed or conflict.visibility not in allowed:
        return False

    for assertion_id in (conflict.assertion_a_id, conflict.assertion_b_id):
        if assertion_id is None:
            continue
        assertion = await db.get(KbAssertion, assertion_id)
        if assertion is None or assertion.visibility not in allowed:
            return False
        doc_access = await db.get(KbDocumentAccess, assertion.document_id)
        if doc_access is None or not await db.scalar(
            select(KbDocumentAccess.document_id).where(
                KbDocumentAccess.document_id == doc_access.document_id,
                *document_access_conditions(ctx, KbDocumentAccess),
            )
        ):
            return False
        current_revision_id = await _current_revision_id(db, assertion.document_id)
        if current_revision_id != assertion.revision_id:
            return False
        has_grounded_evidence = await db.scalar(
            select(KbAssertionEvidence.assertion_id)
            .join(KbEvidence, KbEvidence.id == KbAssertionEvidence.evidence_id)
            .join(KbPassage, KbPassage.id == KbEvidence.passage_id)
            .join(KbRevision, KbRevision.id == KbPassage.revision_id)
            .where(
                KbAssertionEvidence.assertion_id == assertion.id,
                KbAssertionEvidence.relation == "supports",
                KbRevision.id == assertion.revision_id,
                KbRevision.document_id == assertion.document_id,
            )
            .limit(1)
        )
        if has_grounded_evidence is None:
            return False
    if conflict.candidate_entity_id is not None:
        candidate = await db.get(KbEntity, conflict.candidate_entity_id)
        if (
            candidate is None
            or candidate.tenant_id != ctx.ontology_tenant_id
            or candidate.visibility not in allowed
        ):
            return False
    return True
