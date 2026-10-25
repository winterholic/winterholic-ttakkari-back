"""KB v3 자동 지식화 파이프라인 패키지.

문서 리비전별 6단계(parsing -> chunking -> extracting -> linking -> indexing -> canonicalizing)
무인 파이프라인 큐잉, 분산 락/임차권 및 펜싱 제어, 상태 전이 오케스트레이터를 제공한다.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.kb_pipeline.adapters import (
    DefaultChunkingProcessor,
    DefaultIndexingProcessor,
    DefaultParsingProcessor,
    MissingStageProcessor,
    StageProcessorRegistry,
)
from ttakkari.kb_pipeline.orchestrator import (
    KbPipelineOrchestrator,
    derive_scope_and_visibility,
)
from ttakkari.kb_pipeline.types import (
    JOB_STATUS_DEGRADED,
    JOB_STATUS_FAILED,
    JOB_STATUS_HELD,
    JOB_STATUS_READY,
    JOB_STATUS_REGISTERED,
    PIPELINE_STAGES,
    STAGE_CANONICALIZING,
    STAGE_CHUNKING,
    STAGE_EXTRACTING,
    STAGE_INDEXING,
    STAGE_LINKING,
    STAGE_PARSING,
    STAGE_STATUS_DEGRADED,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_HELD,
    STAGE_STATUS_PENDING,
    STAGE_STATUS_RUNNING,
    STAGE_STATUS_SKIPPED,
    STAGE_STATUS_SUCCEEDED,
    StageContext,
    StageProcessor,
    StageResult,
    StaleWriterError,
)
from ttakkari.kb_pipeline.worker import (
    KbPipelineWorker,
    get_default_worker,
    set_default_worker,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage


async def enqueue_pipeline_job(
    db: AsyncSession,
    *,
    document_id: uuid.UUID,
    revision_id: uuid.UUID,
    max_attempts: int = 3,
) -> KbPipelineJob:
    """등록 및 수집 배달 트랜잭션 내에서 리비전별 멱등 파이프라인 작업을 큐에 넣는다."""
    existing = await db.scalar(
        select(KbPipelineJob).where(KbPipelineJob.revision_id == revision_id)
    )
    if existing is not None:
        return existing

    job = KbPipelineJob(
        document_id=document_id,
        revision_id=revision_id,
        status=JOB_STATUS_REGISTERED,
        current_stage=STAGE_PARSING,
        max_attempts=max_attempts,
    )
    db.add(job)
    await db.flush()

    for stage in PIPELINE_STAGES:
        st = KbPipelineStage(
            job_id=job.id,
            stage=stage,
            status=STAGE_STATUS_PENDING,
            attempt_count=0,
        )
        db.add(st)

    return job


__all__ = [
    "JOB_STATUS_DEGRADED",
    "JOB_STATUS_FAILED",
    "JOB_STATUS_HELD",
    "JOB_STATUS_READY",
    "JOB_STATUS_REGISTERED",
    "PIPELINE_STAGES",
    "STAGE_CANONICALIZING",
    "STAGE_CHUNKING",
    "STAGE_EXTRACTING",
    "STAGE_INDEXING",
    "STAGE_LINKING",
    "STAGE_PARSING",
    "STAGE_STATUS_DEGRADED",
    "STAGE_STATUS_FAILED",
    "STAGE_STATUS_HELD",
    "STAGE_STATUS_PENDING",
    "STAGE_STATUS_RUNNING",
    "STAGE_STATUS_SKIPPED",
    "STAGE_STATUS_SUCCEEDED",
    "DefaultChunkingProcessor",
    "DefaultIndexingProcessor",
    "DefaultParsingProcessor",
    "KbPipelineOrchestrator",
    "KbPipelineWorker",
    "MissingStageProcessor",
    "StageContext",
    "StageProcessor",
    "StageProcessorRegistry",
    "StageResult",
    "StaleWriterError",
    "derive_scope_and_visibility",
    "enqueue_pipeline_job",
    "get_default_worker",
    "set_default_worker",
]
