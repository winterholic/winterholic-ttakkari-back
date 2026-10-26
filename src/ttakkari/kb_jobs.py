"""KB v3 파이프라인 작업(job) 조회·재시도 뷰(KB3-402). 모든 조회는 kb_access 단일 규칙으로 문서 접근을 거른다."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ttakkari.kb_access import (
    KbAccessContext,
    assertion_access_filter,
    entity_access_filter,
)
from ttakkari.kb_library import scope_filter
from ttakkari.kb_models import KbDocument, KbEvidence, KbPassage, KbRevision
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_pipeline import PipelineRetryError, retry_pipeline_job
from ttakkari.kb_pipeline.types import (
    JOB_STATUS_DEGRADED,
    JOB_STATUS_FAILED,
    JOB_STATUS_HELD,
    JOB_STATUS_READY,
    JOB_STATUS_REGISTERED,
    PIPELINE_STAGES,
    STAGE_STATUS_PENDING,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage

# 진행 중으로 보는 상태(워커가 아직 일을 하고 있거나 할 예정인 작업)
ACTIVE_STATUSES: tuple[str, ...] = (JOB_STATUS_REGISTERED, "parsing", "chunking", "extracting", "linking", "indexing", "canonicalizing")
# 사람이 봐야 하는 상태
ATTENTION_STATUSES: tuple[str, ...] = (JOB_STATUS_HELD, JOB_STATUS_FAILED, JOB_STATUS_DEGRADED)
ALL_STATUSES: frozenset[str] = frozenset({*ACTIVE_STATUSES, JOB_STATUS_READY, *ATTENTION_STATUSES})
STATUS_GROUPS: dict[str, tuple[str, ...]] = {"active": ACTIVE_STATUSES, "attention": ATTENTION_STATUSES}
MAX_JOB_PAGE = 100


class JobNotFound(LookupError):
    pass


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def stage_view(row: KbPipelineStage | None, stage: str) -> dict[str, Any]:
    if row is None:
        return {"stage": stage, "status": STAGE_STATUS_PENDING, "duration_ms": None, "started_at": None,
                "finished_at": None, "error_message": None, "attempt_count": 0, "processor_version": None, "detail": {}}
    return {
        "stage": row.stage,
        "status": row.status,
        "duration_ms": row.duration_ms,
        "started_at": _iso(row.started_at),
        "finished_at": _iso(row.finished_at),
        "error_message": row.error_message,
        "attempt_count": row.attempt_count,
        "processor_version": row.processor_version,
        "detail": row.detail or {},
    }


async def revision_metrics(db: AsyncSession, access: KbAccessContext, rev_ids: list[uuid.UUID]) -> dict[uuid.UUID, dict[str, int]]:
    """리비전별 처리 결과 수치(설계 §2.3 metrics: passage/entity/assertion/conflict 수, L2 호출 수 제외). 접근 규칙으로 센다."""
    out: dict[uuid.UUID, dict[str, int]] = {
        r: {"passage_count": 0, "entity_count": 0, "assertion_count": 0, "relation_count": 0,
            "relation_candidate_count": 0, "conflict_count": 0}
        for r in rev_ids
    }
    if not rev_ids:
        return out
    for rev_id, n in (await db.execute(
        select(KbPassage.revision_id, func.count()).where(KbPassage.revision_id.in_(rev_ids)).group_by(KbPassage.revision_id)
    )).all():
        out[rev_id]["passage_count"] = n
    link, ent = aliased(KbDocumentEntity), aliased(KbEntity)
    for rev_id, n in (await db.execute(
        select(link.revision_id, func.count(func.distinct(ent.id))).join(ent, ent.id == link.entity_id)
        .where(link.revision_id.in_(rev_ids), entity_access_filter(access, ent)).group_by(link.revision_id)
    )).all():
        out[rev_id]["entity_count"] = n
    assertions = (await db.execute(
        select(KbAssertion.id, KbAssertion.revision_id).where(
            KbAssertion.revision_id.in_(rev_ids), KbAssertion.status.in_(("active", "disputed")),
            assertion_access_filter(access, KbAssertion),
        )
    )).all()
    by_assertion: dict[uuid.UUID, uuid.UUID] = {}
    for a_id, rev_id in assertions:
        by_assertion[a_id] = rev_id
        out[rev_id]["assertion_count"] += 1
    for rev_id, status, n in (await db.execute(
        select(KbPassage.revision_id, KbRelation.status, func.count(func.distinct(KbRelation.id)))
        .join(KbEvidence, KbEvidence.passage_id == KbPassage.id)
        .join(KbRelationEvidence, KbRelationEvidence.evidence_id == KbEvidence.id)
        .join(KbRelation, KbRelation.id == KbRelationEvidence.relation_id)
        .where(KbPassage.revision_id.in_(rev_ids), KbRelation.visibility.in_(access.allowed_visibilities))
        .group_by(KbPassage.revision_id, KbRelation.status)
    )).all():
        key = "relation_count" if status == "active" else "relation_candidate_count" if status == "candidate" else None
        if key:
            out[rev_id][key] = n
    if by_assertion:
        for a_id, b_id in (await db.execute(
            select(KbConflict.assertion_a_id, KbConflict.assertion_b_id).where(
                KbConflict.status == "open", KbConflict.visibility.in_(access.allowed_visibilities),
                or_(KbConflict.assertion_a_id.in_(by_assertion), KbConflict.assertion_b_id.in_(by_assertion)),
            )
        )).all():
            for assertion_id in {a_id, b_id}:
                if assertion_id in by_assertion:
                    out[by_assertion[assertion_id]]["conflict_count"] += 1
    return out


def job_view(
    job: KbPipelineJob, stages: list[KbPipelineStage], *, title: str | None = None, rev_no: int | None = None,
    computed: dict[str, int] | None = None,
) -> dict[str, Any]:
    by_stage = {s.stage: s for s in stages}
    extracting = by_stage.get("extracting")
    l2_calls = int(((extracting.detail or {}).get("l2") or {}).get("calls_made", 0)) if extracting else 0
    return {
        "id": str(job.id),
        "document_id": str(job.document_id),
        "revision_id": str(job.revision_id),
        "document_title": title,
        "rev_no": rev_no,
        "status": job.status,
        "current_stage": job.current_stage,
        "hold_reason": job.hold_reason,
        "error_code": job.error_code,
        "error_detail": job.error_detail,
        "attempt_count": job.attempt_count,
        "max_attempts": job.max_attempts,
        "pipeline_version": job.pipeline_version,
        # 저장된 메트릭(단계 상태 등)에 DB 에서 센 값을 덮어쓴다: passage/entity/assertion/relation/conflict 수와 L2 호출 수
        "metrics": {**(job.metrics or {}), **(computed or {}), "l2_calls": l2_calls},
        "stages": [stage_view(by_stage.get(name), name) for name in PIPELINE_STAGES],
        "created_at": _iso(job.created_at),
        "updated_at": _iso(job.updated_at),
        "completed_at": _iso(job.completed_at),
    }


def _job_access(access: KbAccessContext):
    """작업이 가리키는 문서가 이 컨텍스트에 보이는가."""
    return select(KbDocument.id).where(KbDocument.id == KbPipelineJob.document_id, scope_filter("personal", access)).exists()


async def _views(db: AsyncSession, access: KbAccessContext, jobs: list[KbPipelineJob]) -> list[dict[str, Any]]:
    if not jobs:
        return []
    computed = await revision_metrics(db, access, [j.revision_id for j in jobs])
    stages: dict[uuid.UUID, list[KbPipelineStage]] = {}
    for row in await db.scalars(select(KbPipelineStage).where(KbPipelineStage.job_id.in_([j.id for j in jobs]))):
        stages.setdefault(row.job_id, []).append(row)
    titles = {d.id: d.title for d in await db.scalars(select(KbDocument).where(KbDocument.id.in_({j.document_id for j in jobs})))}
    rev_nos = dict((await db.execute(select(KbRevision.id, KbRevision.rev_no).where(KbRevision.id.in_({j.revision_id for j in jobs})))).all())
    return [
        job_view(j, stages.get(j.id, []), title=titles.get(j.document_id), rev_no=rev_nos.get(j.revision_id),
                 computed=computed.get(j.revision_id))
        for j in jobs
    ]


async def list_jobs(
    db: AsyncSession, access: KbAccessContext, *, status: str | None = None, document_id: uuid.UUID | None = None,
    limit: int = 50, offset: int = 0,
) -> dict[str, Any]:
    """최신순 작업 목록. status 는 작업 상태 하나 또는 그룹(active=진행 중, attention=보류·실패·저하)이다."""
    limit = max(1, min(limit, MAX_JOB_PAGE))
    stmt = select(KbPipelineJob).where(_job_access(access))
    if status:
        if status in STATUS_GROUPS:
            stmt = stmt.where(KbPipelineJob.status.in_(STATUS_GROUPS[status]))
        elif status in ALL_STATUSES:
            stmt = stmt.where(KbPipelineJob.status == status)
        else:
            raise ValueError("kb_job_status_invalid")
    if document_id is not None:
        stmt = stmt.where(KbPipelineJob.document_id == document_id)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    jobs = list(await db.scalars(
        stmt.order_by(KbPipelineJob.created_at.desc(), KbPipelineJob.id).offset(offset).limit(limit)
    ))
    return {"items": await _views(db, access, jobs), "total": total, "limit": limit, "offset": offset}


async def get_job(db: AsyncSession, access: KbAccessContext, job_id: uuid.UUID) -> dict[str, Any]:
    job = await db.scalar(select(KbPipelineJob).where(KbPipelineJob.id == job_id, _job_access(access)))
    if job is None:
        raise JobNotFound(str(job_id))
    return (await _views(db, access, [job]))[0]


async def retry_job(session_factory, db: AsyncSession, access: KbAccessContext, job_id: uuid.UUID, from_stage: str | None) -> dict[str, Any]:
    """접근 가능한 작업만 다시 큐에 넣는다. from_stage 가 없으면 성공하지 않은 첫 단계부터다.

    돌려주는 값은 갱신된 작업 뷰와 retry 결과다(requeued=False 면 이유 포함). 실행 중(임차권 유효)은 PipelineRetryError 다.
    """
    job = await db.scalar(select(KbPipelineJob.id).where(KbPipelineJob.id == job_id, _job_access(access)))
    if job is None:
        raise JobNotFound(str(job_id))
    await db.rollback()  # 읽기 트랜잭션을 닫고 재시도 쓰기는 자기 세션에서 한다(FOR UPDATE 경합 방지)
    outcome = await retry_pipeline_job(session_factory, job_id, from_stage)
    view = await get_job(db, access, job_id)
    view["retry"] = {
        "requeued": outcome.requeued,
        "from_stage": outcome.from_stage,
        "reason": outcome.reason,
        "reset_stages": list(outcome.reset_stages),
    }
    return view


__all__ = ["JobNotFound", "PipelineRetryError", "get_job", "job_view", "list_jobs", "retry_job", "stage_view"]
