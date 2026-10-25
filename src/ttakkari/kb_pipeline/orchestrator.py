"""KB v3 자동 지식화 파이프라인 오케스트레이터.

6단계(parsing -> chunking -> extracting -> linking -> indexing -> canonicalizing)를 단계 의존성(STAGE_REQUIRES)에 따라
구동한다. 추출/연결/정본 단계의 실패나 보류는 검색 색인(indexing)을 막지 않는다(부분 실패 격리).

- 단계별 상태 전이와 펜싱(attempt_token): 상태 저장은 매번 토큰을 확인하고, 처리기의 DB 쓰기는 ctx.fenced_session() 으로
  같은 트랜잭션에서 토큰을 확인한 뒤 커밋한다.
- 재시도: 실패한 단계가 있으면 지수 백오프로 같은 잡을 다시 큐에 넣는다. 성공/저하/보류 단계는 건너뛴다.
  보류(held)·최종 실패·저하 잡은 retry_pipeline_job() 으로 사람이/상위 서비스가 다시 큐에 넣는다.
- 잡 상태: 모든 단계 성공 -> ready. 핵심 단계(parsing/chunking/indexing)가 실패하면 failed, 보류면 held.
  핵심 단계는 끝났으나 보강 단계가 저하·보류·실패·건너뜀이면 degraded(검색은 가능하나 지식화가 미완).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.collector.control_models import KbDocumentAccess
from ttakkari.kb_pipeline.adapters import StageProcessorRegistry
from ttakkari.kb_pipeline.types import (
    CORE_STAGES,
    JOB_STATUS_DEGRADED,
    JOB_STATUS_FAILED,
    JOB_STATUS_HELD,
    JOB_STATUS_READY,
    JOB_STATUS_REGISTERED,
    PIPELINE_STAGES,
    STAGE_REQUIRES,
    STAGE_STATUS_DEGRADED,
    STAGE_STATUS_FAILED,
    STAGE_STATUS_HELD,
    STAGE_STATUS_PENDING,
    STAGE_STATUS_RUNNING,
    STAGE_STATUS_SKIPPED,
    STAGE_STATUS_SUCCEEDED,
    StageContext,
    StageResult,
    StaleWriterError,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.kb_visibility import derive_scope_and_visibility
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

DEFAULT_BASE_BACKOFF_SECONDS = 30.0
DEFAULT_MAX_BACKOFF_SECONDS = 3600.0
DEFAULT_LEASE_EXTENSION_SECONDS = 300.0

# 이전 시도에서 이미 결론이 난 단계(재실행하지 않는다). failed/pending/running/skipped 만 다시 돈다.
_SETTLED_STATUSES = (
    STAGE_STATUS_SUCCEEDED,
    STAGE_STATUS_DEGRADED,
    STAGE_STATUS_HELD,
)
# 의존 단계로서 충족되는 상태(저하는 부분 결과가 있으므로 다음 단계를 허용한다).
_SATISFYING_STATUSES = (STAGE_STATUS_SUCCEEDED, STAGE_STATUS_DEGRADED)


@dataclass
class RetryOutcome:
    """retry_pipeline_job 결과."""

    job_id: uuid.UUID
    requeued: bool
    from_stage: str
    reason: str | None = None
    reset_stages: tuple[str, ...] = ()


class PipelineRetryError(Exception):
    """재시도를 받아들일 수 없는 요청(알 수 없는 잡/단계, 실행 중인 잡)."""


class KbPipelineOrchestrator:
    """6단계 지식화 파이프라인 작업 실행기."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        registry: StageProcessorRegistry | None = None,
        base_backoff_seconds: float = DEFAULT_BASE_BACKOFF_SECONDS,
        max_backoff_seconds: float = DEFAULT_MAX_BACKOFF_SECONDS,
        lease_extension_seconds: float = DEFAULT_LEASE_EXTENSION_SECONDS,
    ) -> None:
        self.session_factory = session_factory
        self.registry = registry or StageProcessorRegistry()
        self.base_backoff_seconds = base_backoff_seconds
        self.max_backoff_seconds = max_backoff_seconds
        self.lease_extension_seconds = lease_extension_seconds

    def _compute_backoff(self, attempt_count: int) -> float:
        factor = 2 ** max(0, attempt_count - 1)
        return min(self.max_backoff_seconds, self.base_backoff_seconds * factor)

    async def _verify_and_get_job(
        self, db: AsyncSession, job_id: Any, attempt_token: Any, *, lock: bool = False
    ) -> KbPipelineJob:
        if lock:
            job = await db.scalar(
                select(KbPipelineJob)
                .where(KbPipelineJob.id == job_id)
                .with_for_update()
            )
        else:
            job = await db.get(KbPipelineJob, job_id)
        if job is None:
            raise StaleWriterError(f"job_not_found: {job_id}")
        if job.attempt_token != attempt_token:
            raise StaleWriterError(
                f"stale_writer_fenced: token mismatch for job {job_id} "
                f"(expected {job.attempt_token}, got {attempt_token})"
            )
        return job

    def _extend_lease(self, job: KbPipelineJob) -> None:
        if job.claimed_by is not None:
            job.lease_expires_at = utcnow() + timedelta(
                seconds=self.lease_extension_seconds
            )

    async def execute_job(self, job_id: Any, attempt_token: Any) -> str:
        """한 파이프라인 작업을 의존성 순서로 실행하고 최종(또는 재시도 예약) 상태를 돌려준다."""
        async with self.session_factory() as db:
            job = await self._verify_and_get_job(db, job_id, attempt_token)
            document_id = job.document_id
            revision_id = job.revision_id
            access = await db.get(KbDocumentAccess, document_id)
            origin_scope, visibility = derive_scope_and_visibility(access)
            existing_stages = list(
                await db.scalars(
                    select(KbPipelineStage).where(KbPipelineStage.job_id == job.id)
                )
            )
            statuses: dict[str, str] = {s.stage: s.status for s in existing_stages}
            stage_ids: dict[str, uuid.UUID] = {s.stage: s.id for s in existing_stages}

        results: dict[str, StageResult] = {}

        for stage in PIPELINE_STAGES:
            current = statuses.get(stage, STAGE_STATUS_PENDING)
            if current in _SETTLED_STATUSES:
                continue

            blocked_by = [
                req
                for req in STAGE_REQUIRES[stage]
                if statuses.get(req, STAGE_STATUS_PENDING) not in _SATISFYING_STATUSES
            ]
            if blocked_by:
                await self._record_skipped(
                    job_id, attempt_token, stage, blocked_by, statuses
                )
                statuses[stage] = STAGE_STATUS_SKIPPED
                continue

            await self._enter_stage(job_id, attempt_token, stage, stage_ids, statuses)

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
            except StaleWriterError:
                raise
            except Exception as exc:
                log.exception(
                    "kb pipeline stage %s execution error on job %s", stage, job_id
                )
                result = StageResult.failed(f"{type(exc).__name__}: {exc}")
            duration_ms = int((monotonic() - start_t) * 1000)

            await self._record_result(
                job_id, attempt_token, stage, result, duration_ms
            )
            statuses[stage] = result.status
            results[stage] = result

        return await self._finalize(job_id, attempt_token, statuses, results)

    async def _enter_stage(
        self,
        job_id: Any,
        attempt_token: Any,
        stage: str,
        stage_ids: dict[str, uuid.UUID],
        statuses: dict[str, str],
    ) -> None:
        async with self.session_factory() as db:
            job = await self._verify_and_get_job(db, job_id, attempt_token, lock=True)
            job.current_stage = stage
            job.status = stage
            self._extend_lease(job)

            st = await db.scalar(
                select(KbPipelineStage).where(
                    KbPipelineStage.job_id == job.id,
                    KbPipelineStage.stage == stage,
                )
            )
            if st is None:
                st = KbPipelineStage(
                    job_id=job.id,
                    stage=stage,
                    status=STAGE_STATUS_RUNNING,
                    attempt_count=1,
                    started_at=utcnow(),
                )
                db.add(st)
            else:
                st.status = STAGE_STATUS_RUNNING
                st.attempt_count = (st.attempt_count or 0) + 1
                st.started_at = utcnow()
                st.finished_at = None
                st.error_message = None
            await db.flush()
            stage_ids[stage] = st.id
            statuses[stage] = STAGE_STATUS_RUNNING
            await db.commit()

    async def _record_skipped(
        self,
        job_id: Any,
        attempt_token: Any,
        stage: str,
        blocked_by: list[str],
        statuses: dict[str, str],
    ) -> None:
        reason = "dependency_unmet: " + ", ".join(
            f"{req}={statuses.get(req, STAGE_STATUS_PENDING)}" for req in blocked_by
        )
        async with self.session_factory() as db:
            job = await self._verify_and_get_job(db, job_id, attempt_token, lock=True)
            st = await db.scalar(
                select(KbPipelineStage).where(
                    KbPipelineStage.job_id == job.id,
                    KbPipelineStage.stage == stage,
                )
            )
            if st is None:
                st = KbPipelineStage(job_id=job.id, stage=stage)
                db.add(st)
            st.status = STAGE_STATUS_SKIPPED
            st.detail = {"reason": reason, "blocked_by": blocked_by}
            st.error_message = reason
            st.finished_at = utcnow()
            await db.commit()

    async def _record_result(
        self,
        job_id: Any,
        attempt_token: Any,
        stage: str,
        result: StageResult,
        duration_ms: int,
    ) -> None:
        async with self.session_factory() as db:
            job = await self._verify_and_get_job(db, job_id, attempt_token, lock=True)
            st = await db.scalar(
                select(KbPipelineStage).where(
                    KbPipelineStage.job_id == job.id,
                    KbPipelineStage.stage == stage,
                )
            )
            if st is None:
                st = KbPipelineStage(job_id=job.id, stage=stage)
                db.add(st)
            st.status = result.status
            st.detail = result.detail
            st.error_message = result.error_message
            st.finished_at = utcnow()
            st.duration_ms = duration_ms
            st.processor_version = result.processor_version[:32]
            self._extend_lease(job)
            await db.commit()

    async def _finalize(
        self,
        job_id: Any,
        attempt_token: Any,
        statuses: dict[str, str],
        results: dict[str, StageResult],
    ) -> str:
        """한 패스가 끝난 뒤 잡 상태를 집계한다. 실패 단계가 있고 시도가 남았으면 재시도를 예약한다."""
        async with self.session_factory() as db:
            job = await self._verify_and_get_job(db, job_id, attempt_token, lock=True)
            stage_rows = {
                s.stage: s
                for s in await db.scalars(
                    select(KbPipelineStage).where(KbPipelineStage.job_id == job.id)
                )
            }

            def status_of(stage: str) -> str:
                return statuses.get(
                    stage,
                    stage_rows[stage].status if stage in stage_rows else STAGE_STATUS_PENDING,
                )

            def first_problem(stage_names: tuple[str, ...], status: str) -> str | None:
                for name in stage_names:
                    if status_of(name) == status:
                        return name
                return None

            failed_stage = first_problem(PIPELINE_STAGES, STAGE_STATUS_FAILED)
            held_stage = first_problem(PIPELINE_STAGES, STAGE_STATUS_HELD)
            job.claimed_by = None
            job.lease_expires_at = None
            job.metrics = {
                **(job.metrics or {}),
                "stage_statuses": {s: status_of(s) for s in PIPELINE_STAGES},
            }

            if failed_stage is not None and job.attempt_count < job.max_attempts:
                backoff = self._compute_backoff(job.attempt_count)
                job.status = JOB_STATUS_REGISTERED
                job.current_stage = failed_stage
                job.next_run_at = utcnow() + timedelta(seconds=backoff)
                job.error_code = "stage_failed"
                job.error_detail = (
                    results[failed_stage].error_message
                    if failed_stage in results
                    else stage_rows[failed_stage].error_message
                )
                await db.commit()
                log.warning(
                    "kb pipeline job %s stage %s failed (attempt %d/%d). Retry in %.1fs",
                    job_id, failed_stage, job.attempt_count, job.max_attempts, backoff,
                )
                return "retry_scheduled"

            all_succeeded = all(
                status_of(s) == STAGE_STATUS_SUCCEEDED for s in PIPELINE_STAGES
            )
            core_failed = first_problem(CORE_STAGES, STAGE_STATUS_FAILED)
            core_held = first_problem(CORE_STAGES, STAGE_STATUS_HELD)
            core_blocked = [
                s for s in CORE_STAGES if status_of(s) == STAGE_STATUS_SKIPPED
            ]

            if all_succeeded:
                job.status = JOB_STATUS_READY
                job.error_code = None
                job.error_detail = None
                job.hold_reason = None
                outcome = JOB_STATUS_READY
            elif core_failed is not None or (core_blocked and failed_stage is not None):
                job.status = JOB_STATUS_FAILED
                job.error_code = "max_attempts_exceeded"
                job.error_detail = (
                    results[failed_stage].error_message
                    if failed_stage in results
                    else stage_rows[failed_stage].error_message
                ) if failed_stage else None
                outcome = JOB_STATUS_FAILED
            elif core_held is not None or core_blocked:
                job.status = JOB_STATUS_HELD
                culprit = core_held or core_blocked[0]
                raw_reason = _stage_reason(results.get(culprit), stage_rows.get(culprit))
                job.hold_reason = raw_reason[:64]
                job.error_detail = (
                    results[culprit].error_message
                    if culprit in results
                    else stage_rows[culprit].error_message
                    if culprit in stage_rows
                    else None
                )
                outcome = JOB_STATUS_HELD
            else:
                # 핵심 단계는 끝났고 보강 단계가 미완이다: 검색은 되지만 지식화는 덜 됐다. ready 로 위장하지 않는다.
                job.status = JOB_STATUS_DEGRADED
                culprit = (
                    failed_stage
                    or held_stage
                    or first_problem(PIPELINE_STAGES, STAGE_STATUS_DEGRADED)
                    or first_problem(PIPELINE_STAGES, STAGE_STATUS_SKIPPED)
                )
                if failed_stage is not None:
                    job.error_code = "max_attempts_exceeded"
                if culprit is not None:
                    job.hold_reason = _stage_reason(
                        results.get(culprit), stage_rows.get(culprit)
                    )[:64]
                    job.error_detail = (
                        results[culprit].error_message
                        if culprit in results
                        else stage_rows[culprit].error_message
                        if culprit in stage_rows
                        else None
                    )
                outcome = JOB_STATUS_DEGRADED

            job.completed_at = utcnow()
            await db.commit()
            log.info("kb pipeline job %s finished as %s", job_id, outcome)
            return outcome


def _stage_reason(result: StageResult | None, row: KbPipelineStage | None) -> str:
    detail: dict[str, Any] = {}
    if result is not None:
        detail = result.detail or {}
    elif row is not None:
        detail = row.detail or {}
    raw = detail.get("missing_capability") or detail.get("reason")
    if raw:
        return str(raw)
    if result is not None and result.error_message:
        return result.error_message
    if row is not None and row.error_message:
        return row.error_message
    return "unspecified"


async def retry_pipeline_job(
    session_factory: Callable[[], AsyncSession],
    job_id: uuid.UUID,
    from_stage: str | None = None,
) -> RetryOutcome:
    """held / failed / degraded / ready 잡을 다시 큐에 넣는다(멱등).

    - from_stage 가 있으면 그 단계와 이후 단계를 pending 으로 되돌려 다시 실행한다. 앞선 단계는 건너뛴다.
    - from_stage 가 없으면 성공하지 않은(또는 저하·보류·건너뜀·실패) 첫 단계부터 다시 한다.
    - attempt_token 을 새로 발급해 이전 실행자의 쓰기를 차단하고, 시도 횟수는 0 으로 되돌린다.
    - 이미 같은 단계에서 대기 중인 registered 잡에 다시 부르면 아무것도 바꾸지 않는다.
    - 지금 임차권이 살아 있는 잡(실행 중)은 거절한다.
    """
    if from_stage is not None and from_stage not in PIPELINE_STAGES:
        raise PipelineRetryError(f"unknown_stage: {from_stage}")

    async with session_factory() as db:
        job = await db.scalar(
            select(KbPipelineJob)
            .where(KbPipelineJob.id == job_id)
            .with_for_update()
        )
        if job is None:
            raise PipelineRetryError(f"job_not_found: {job_id}")

        now = utcnow()
        lease = job.lease_expires_at
        if lease is not None and lease.tzinfo is None:
            from datetime import UTC

            lease = lease.replace(tzinfo=UTC)
        if job.claimed_by is not None and lease is not None and lease > now:
            raise PipelineRetryError("job_running: lease still active")

        stages = {
            s.stage: s
            for s in await db.scalars(
                select(KbPipelineStage).where(KbPipelineStage.job_id == job.id)
            )
        }

        if from_stage is None:
            target = next(
                (
                    s
                    for s in PIPELINE_STAGES
                    if s not in stages or stages[s].status != STAGE_STATUS_SUCCEEDED
                ),
                None,
            )
            if target is None:
                return RetryOutcome(
                    job_id=job_id,
                    requeued=False,
                    from_stage=PIPELINE_STAGES[0],
                    reason="nothing_to_retry: all stages succeeded",
                )
        else:
            target = from_stage

        target_idx = PIPELINE_STAGES.index(target)
        to_reset = PIPELINE_STAGES[target_idx:]

        already_queued = (
            job.status == JOB_STATUS_REGISTERED
            and job.claimed_by is None
            and job.current_stage == target
            and all(
                (stages[s].status == STAGE_STATUS_PENDING) if s in stages else True
                for s in to_reset
            )
        )
        if already_queued:
            return RetryOutcome(
                job_id=job_id,
                requeued=False,
                from_stage=target,
                reason="already_queued",
                reset_stages=(),
            )

        for name in to_reset:
            st = stages.get(name)
            if st is None:
                db.add(
                    KbPipelineStage(
                        job_id=job.id, stage=name, status=STAGE_STATUS_PENDING
                    )
                )
                continue
            st.status = STAGE_STATUS_PENDING
            st.detail = {}
            st.error_message = None
            st.started_at = None
            st.finished_at = None
            st.duration_ms = None

        job.status = JOB_STATUS_REGISTERED
        job.current_stage = target
        job.attempt_token = uuid.uuid4()
        job.attempt_count = 0
        job.claimed_by = None
        job.claimed_at = None
        job.lease_expires_at = None
        job.next_run_at = now
        job.hold_reason = None
        job.error_code = None
        job.error_detail = None
        job.completed_at = None
        await db.commit()
        log.info(
            "kb pipeline job %s requeued from stage %s (reset %s)",
            job_id, target, ",".join(to_reset),
        )
        return RetryOutcome(
            job_id=job_id,
            requeued=True,
            from_stage=target,
            reset_stages=tuple(to_reset),
        )
