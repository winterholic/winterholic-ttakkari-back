"""KB v3 자동 지식화 파이프라인 canonicalizing 단계 처리기.

원칙:
1. 단계 계약 준수:
   fenced_stage_write 정책(ctx.fenced_session())을 준수하여 분산/재시도 환경에서 쓰기 임차권(attempt_token)을 보장한다.
2. 결정론적 베이스라인:
   외부 LLM 이나 MCP 수동 호출 없이 코드로 정밀하게 동작하는 결정론적 알고리즘으로 수행된다.
3. 멱등성 및 원장 보호:
   원장(kb_revisions, kb_passages, kb_evidence)은 절대 수정하지 않으며,
   리비전 갱신 시 과거 주장을 superseded 로 안전하게 전환하고,
   동일 조건 충돌 탐지 및 정본 카드(kb_entity_canonicals)를 합성한다.
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select

from ttakkari.kb_models import KbRevision
from ttakkari.kb_ontology.canonicals import (
    synthesize_canonical_for_entity,
    update_superseded_assertions,
)
from ttakkari.kb_ontology.conflicts import detect_conflicts_for_entity
from ttakkari.kb_ontology_models import KbAssertion, KbDocumentEntity
from ttakkari.kb_pipeline.types import (
    STAGE_CANONICALIZING,
    StageContext,
    StageResult,
)

log = logging.getLogger(__name__)

__all__ = ["CanonicalizingProcessor"]


class CanonicalizingProcessor:
    """다문서 정본 통합 및 상충 감지를 수행하는 실제 파이프라인 처리기."""

    async def __call__(self, ctx: StageContext) -> StageResult:
        if ctx.session_factory is None:
            return StageResult.failed("session_factory_missing")

        # 1. 대상 리비전 및 영향받는 엔터티 목록 사전 조회
        async with ctx.session_factory() as db:
            rev = await db.get(KbRevision, ctx.revision_id)
            if rev is None:
                return StageResult.failed(f"revision_not_found: {ctx.revision_id}")

            affected_entity_ids: set[uuid.UUID] = set()

            # 문서-엔터티 연결에서 엔터티 ID 추출
            doc_entities = await db.scalars(
                select(KbDocumentEntity.entity_id).where(
                    KbDocumentEntity.revision_id == ctx.revision_id
                )
            )
            for eid in doc_entities:
                affected_entity_ids.add(eid)

            # 주장 테이블에서 주어 및 목적어 엔터티 추출
            assertions = list(
                await db.scalars(
                    select(KbAssertion).where(
                        KbAssertion.revision_id == ctx.revision_id
                    )
                )
            )
            for a in assertions:
                affected_entity_ids.add(a.subject_entity_id)
                if a.object_entity_id is not None:
                    affected_entity_ids.add(a.object_entity_id)

            # 구버전 대체(supersedes_id) 대상 주어 엔터티도 포함
            if rev.supersedes_id is not None:
                older_eids = await db.scalars(
                    select(KbAssertion.subject_entity_id).where(
                        KbAssertion.revision_id == rev.supersedes_id
                    )
                )
                for eid in older_eids:
                    affected_entity_ids.add(eid)

            db.expunge_all()

        conflicts_count = 0
        canonicals_count = 0
        superseded_count = 0

        # 2. Fenced 트랜잭션 내에서 영속화 수행 (attempt_token 잠금 및 검증)
        async with ctx.fenced_session() as db:
            # 과거 리비전 주장 superseded 전환
            superseded_count = await update_superseded_assertions(
                db, ctx.revision_id
            )

            # 영향 엔터티별 충돌 탐지 및 정본 요약 카드 갱신
            for eid in affected_entity_ids:
                new_conflicts = await detect_conflicts_for_entity(db, eid)
                conflicts_count += len(new_conflicts)

                canonical = await synthesize_canonical_for_entity(db, eid)
                if canonical is not None:
                    canonicals_count += 1

        detail = {
            "entities_processed": len(affected_entity_ids),
            "canonicals_updated": canonicals_count,
            "conflicts_detected": conflicts_count,
            "assertions_superseded": superseded_count,
        }
        log.info(
            "kb pipeline job %s stage %s succeeded: %s",
            ctx.job_id,
            STAGE_CANONICALIZING,
            detail,
        )
        return StageResult.success(detail=detail, version="canonicalizing-v1")
