"""KB v3 자동 지식화 파이프라인 오케스트레이터.

6단계(parsing -> chunking -> extracting -> linking -> indexing -> canonicalizing) 상태 머신을 구동하며,
단계별 상태 전이, 펜싱(attempt_token), 지수 백오프 기반 재시도(from_stage), 단계별 보류(held)/저하(degraded)를 제어한다.
오래된 임차권 소유자(stale writer)의 side effect는 매 커밋 전 attempt_token 검증으로 원천 차단된다.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_pipeline.adapters import StageProcessorRegistry
from ttakkari.kb_pipeline.types import (
    JOB_STATUS_DEGRADED,
    JOB_STATUS_FAILED,
    JOB_STATUS_HELD,
    JOB_STATUS_READY,
    JOB_STATUS_REGISTERED,
    PIPELINE_STAGES,
    STAGE_CANONICALIZING,
    STAGE_STATUS_DEGRADED,
    STAGE_STATUS_HELD,
    STAGE_STATUS_RUNNING,
    STAGE_STATUS_SUCCEEDED,
    StageContext,
    StageResult,
    StaleWriterError,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

DEFAULT_BASE_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_BACKOFF_SECONDS = 3600.0


def derive_scope_and_visibility(
    access: KbDocumentAccess | None,
) -> tuple[str, str]:
    """원천 문서의 접근 권한(KbDocumentAccess)으로부터 origin_scope와 visibility를 fail-closed로 유도한다.

    규칙:
    1. access 행이 없으면 fail-closed 원칙에 따라 ('work', 'work_only') 반환.
    2. access.scope == 'work' 이거나 origin_scope == 'work' 인 경우 visibility='work_only' 강제 (global 승격 금지).
    3. personal 스코프이면서 visibility=='global'로 명시 설정된 경우에만 ('personal', 'global') 허용.
       그 외 불확실하거나 미설정된 경우 fail-closed로 ('personal', 'work_only') 반환.
    """
    if access is None:
        return "work", "work_only"

    origin_scope = getattr(access, "origin_scope", None) or access.scope or "work"
    if access.scope == "work" or origin_scope == "work":
        return "work", "work_only"

    visibility = getattr(access, "visibility", None)
    if visibility == "global":
        return "personal", "global"
    return "personal", "work_only"


class KbPipelineOrchestrator:
    """6단계 지식화 파이프라인 작업 실행기."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        registry: StageProcessorRegistry | None = None,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry or StageProcessorRegistry()
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds

    def _compute_backoff(self, attempt_count: int) -> float:
        factor = 2 ** max(0, attempt_count - 1)
        return min(self.max_backoff_seconds, self.base_backoff_seconds * factor)

    async def _verify_and_get_job(
        self, db: AsyncSession, job_id: Any, attempt_token: Any
    ) -> KbPipelineJob:
        job = await db.get(KbPipelineJob, job_id)
        if job is None:
            raise StaleWriterError(f"job_not_found: {job_id}")
        if job.attempt_token != attempt_token:
            raise StaleWriterError(
                f"stale_writer_fenced: token mismatch for job {job_id} "
                f"(expected {job.attempt_token}, got {attempt_token})"
            )
        return job

    async def execute_job(self, job_id: Any, attempt_token: Any) -> str:
        """한 파이프라인 작업을 현재 단계부터 6단계 완료 또는 중단 지점까지 실행한다."""
        # 1. 초기 펜싱 검증 및 메타데이터 확보
        async with self.session_factory() as db:
            job = await self._verify_and_get_job(db, job_id, attempt_token)
            document_id = job.document_id
            revision_id = job.revision_id

            access = await db.get(KbDocumentAccess, document_id)
            origin_scope, visibility = derive_scope_and_visibility(access)

            # 기존 단계별 상태 맵 조회
            existing_stages = list(
                await db.scalars(
                    select(KbPipelineStage).where(
                        KbPipelineStage.job_id == job.id
                    )
                )
            )
            stage_map = {s.stage: s for s in existing_stages}

        # 2. 6단계 순차 실행
        for stage in PIPELINE_STAGES:
            # (from_stage 원칙) 이미 이전 시도에서 성공한 단계는 건너뜀
            if (
                stage in stage_map
                and stage_map[stage].status == STAGE_STATUS_SUCCEEDED
            ):
                log.debug(
                    "job %s stage %s already succeeded; skipping", job_id, stage
                )
                continue

            # 단계 진입 트랜잭션
            async with self.session_factory() as db:
                job = await self._verify_and_get_job(db, job_id, attempt_token)
                job.current_stage = stage
                job.status = stage

                st = stage_map.get(stage)
                if st is None:
                    st = KbPipelineStage(
                        job_id=job.id,
                        stage=stage,
                        status=STAGE_STATUS_RUNNING,
                        attempt_count=1,
                        started_at=utcnow(),
                    )
                    db.add(st)
                    stage_map[stage] = st
                else:
                    st_db = await db.get(KbPipelineStage, st.id)
                    if st_db is not None:
                        st_db.status = STAGE_STATUS_RUNNING
                        st_db.attempt_count += 1
                        st_db.started_at = utcnow()
                        st = st_db

                await db.commit()

            # 프로세서 실행 (I/O 및 모델 추론은 DB 트랜잭션 밖에서 수행)
            processor = self.registry.get(stage)
            ctx = StageContext(
                job_id=job_id,
                document_id=document_id,
                revision_id=revision_id,
                stage=stage,
                attempt_token=attempt_token,
                origin_scope=origin_scope,
                visibility=visibility,
                session_factory=self.session_factory,
            )

            start_t = monotonic()
            try:
                result = await processor(ctx)
            except Exception as exc:
                log.exception(
                    "kb pipeline stage %s execution error on job %s",
                    stage,
                    job_id,
                )
                result = StageResult.failed(str(exc))
            duration_ms = int((monotonic() - start_t) * 1000)

            # 단계 결과 및 상태 전이 저장 (트랜잭션 내 펜싱 재검증)
            async with self.session_factory() as db:
                job = await self._verify_and_get_job(db, job_id, attempt_token)
                st_db = await db.scalar(
                    select(KbPipelineStage).where(
                        KbPipelineStage.job_id == job_id,
                        KbPipelineStage.stage == stage,
                    )
                )
                if st_db is None:
                    st_db = KbPipelineStage(
                        job_id=job.id,
                        stage=stage,
                    )
                    db.add(st_db)

                st_db.status = result.status
                st_db.detail = result.detail
                st_db.error_message = result.error_message
                st_db.finished_at = utcnow()
                st_db.duration_ms = duration_ms
                st_db.processor_version = result.processor_version

                if result.status == STAGE_STATUS_SUCCEEDED:
                    stage_map[stage] = st_db
                    # 6단계 전체가 성공한 경우 (마지막 canonicalizing 단계 완료)
                    if stage == STAGE_CANONICALIZING:
                        job.status = JOB_STATUS_READY
                        job.completed_at = utcnow()
                        job.claimed_by = None
                        job.lease_expires_at = None
                        await db.commit()
                        log.info(
                            "kb pipeline job %s reached READY successfully",
                            job_id,
                        )
                        return JOB_STATUS_READY

                    # 다음 단계로 진행 계속
                    await db.commit()
                    continue

                if result.status == STAGE_STATUS_HELD:
                    # 미구현 capability 또는 리소스 보류로 잡 일시 정지
                    job.status = JOB_STATUS_HELD
                    raw_reason = (
                        result.detail.get("missing_capability")
                        or result.detail.get("reason")
                        or "capability_missing"
                    )
                    job.hold_reason = str(raw_reason)[:64]
                    job.error_detail = result.error_message
                    job.claimed_by = None
                    job.lease_expires_at = None
                    await db.commit()
                    log.info(
                        "kb pipeline job %s HELD at stage %s: %s",
                        job_id,
                        stage,
                        job.hold_reason,
                    )
                    return JOB_STATUS_HELD

                if result.status == STAGE_STATUS_DEGRADED:
                    # 부분 검색 가용성을 제공하는 성능 저하 완료
                    job.status = JOB_STATUS_DEGRADED
                    job.completed_at = utcnow()
                    job.claimed_by = None
                    job.lease_expires_at = None
                    job.error_detail = result.error_message
                    await db.commit()
                    log.info(
                        "kb pipeline job %s completed DEGRADED at stage %s",
                        job_id,
                        stage,
                    )
                    return JOB_STATUS_DEGRADED

                # 실패(failed) 시 재시도 또는 영구 실패 처리
                if job.attempt_count < job.max_attempts:
                    backoff = self._compute_backoff(job.attempt_count)
                    job.status = JOB_STATUS_REGISTERED
                    job.current_stage = stage  # 실패한 단계부터 다시 재개
                    job.next_run_at = utcnow() + timedelta(seconds=backoff)
                    job.claimed_by = None
                    job.lease_expires_at = None
                    job.error_code = "stage_failed"
                    job.error_detail = result.error_message
                    await db.commit()
                    log.warning(
                        "kb pipeline job %s stage %s failed (attempt %d/%d). Retry in %.1fs: %s",
                        job_id,
                        stage,
                        job.attempt_count,
                        job.max_attempts,
                        backoff,
                        result.error_message,
                    )
                    return "retry_scheduled"

                job.status = JOB_STATUS_FAILED
                job.error_code = "max_attempts_exceeded"
                job.error_detail = result.error_message
                job.completed_at = utcnow()
                job.claimed_by = None
                job.lease_expires_at = None
                await db.commit()
                log.error(
                    "kb pipeline job %s FAILED permanently at stage %s: %s",
                    job_id,
                    stage,
                    result.error_message,
                )
                return JOB_STATUS_FAILED

        return JOB_STATUS_READY
