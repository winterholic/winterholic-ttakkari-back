"""KB v3 자동 지식화 파이프라인 백그라운드 워커.

SKIP LOCKED 기반 분산 임차권(lease) 획득, 만료된 임차권 자동 회수, 펜싱 토큰 부여,
세대 런타임 수명주기(pause_claiming, drain, remote API deferred)를 준수한다.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ttakkari.db import sessionmaker
from ttakkari.kb_pipeline.orchestrator import KbPipelineOrchestrator
from ttakkari.kb_pipeline.types import (
    JOB_STATUS_REGISTERED,
    PIPELINE_STAGES,
    StaleWriterError,
)
from ttakkari.kb_pipeline_models import KbPipelineJob
from ttakkari.models import utcnow

log = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 300.0


class KbPipelineWorker:
    """자동 지식화 작업을 클레임하고 오케스트레이터로 실행하는 워커."""

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession] | None = None,
        orchestrator: KbPipelineOrchestrator | None = None,
        worker_id: str | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self.session_factory = session_factory or sessionmaker()
        self.orchestrator = orchestrator or KbPipelineOrchestrator(
            session_factory=self.session_factory
        )
        self.worker_id = (
            worker_id or f"kb-worker:{uuid.uuid4().hex[:12]}"
        )
        self.lease_seconds = lease_seconds
        self._tasks: set[asyncio.Task[Any]] = set()
        self._paused = False
        self._stopping = False

    @property
    def claiming_paused(self) -> bool:
        return self._paused

    def pause_claiming(self) -> None:
        """드레인 또는 롤백 준비 시 신규 작업 획득을 일시 중단한다."""
        self._paused = True

    def resume_claiming(self) -> None:
        """작업 획득을 재개한다."""
        self._paused = False

    def stop_claiming(self) -> None:
        """종료 신호 시 루프를 정지한다."""
        self._stopping = True
        self._paused = True

    def inflight(self) -> int:
        """현재 실행 중인 백그라운드 태스크 수를 반환한다."""
        return sum(not t.done() for t in self._tasks)

    async def cancel_inflight(self) -> None:
        """드레인 상한 초과 또는 강제 종료 시 남은 실행 중인 태스크를 취소한다."""
        tasks = [t for t in self._tasks if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def claim_job(
        self, db: AsyncSession
    ) -> tuple[uuid.UUID, uuid.UUID] | None:
        """SKIP LOCKED 로 실행 가능한 1개 작업을 선점(claim)하고 attempt_token 을 갱신한다.

        선점 조건:
        - status 가 registered 이거나 진행 중인 stage 중 하나
        - next_run_at <= now
        - claimed_by 가 없거나 lease_expires_at 이 지난 경우(만료 임차권 회수)
        """
        now = utcnow()
        active_statuses = (JOB_STATUS_REGISTERED, *PIPELINE_STAGES)

        stmt = (
            select(KbPipelineJob)
            .where(
                and_(
                    KbPipelineJob.status.in_(active_statuses),
                    KbPipelineJob.next_run_at <= now,
                    or_(
                        KbPipelineJob.claimed_by.is_(None),
                        KbPipelineJob.lease_expires_at <= now,
                    ),
                )
            )
            .order_by(
                KbPipelineJob.next_run_at.asc(),
                KbPipelineJob.created_at.asc(),
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )

        job = await db.scalar(stmt)
        if job is None:
            return None

        new_token = uuid.uuid4()
        job.attempt_token = new_token
        job.claimed_by = self.worker_id
        job.claimed_at = now
        job.lease_expires_at = now + timedelta(seconds=self.lease_seconds)
        job.attempt_count += 1
        job.status = job.current_stage

        return job.id, new_token

    async def _run_job_wrapper(
        self, job_id: uuid.UUID, token: uuid.UUID
    ) -> None:
        try:
            await self.orchestrator.execute_job(job_id, token)
        except StaleWriterError as exc:
            log.warning("kb pipeline job %s fenced: %s", job_id, exc)
        except Exception:
            log.exception("unhandled exception running kb pipeline job %s", job_id)

    async def pump(self) -> int:
        """소비자 루프의 1회 claim 및 실행. 작업이 시작되면 1, 없으면 0을 반환한다."""
        if self._paused or self._stopping:
            return 0

        # remote API 모드인 경우 워커는 일감을 가져가지 않음
        with contextlib.suppress(Exception):
            from ttakkari.background_consumers import deferred

            if deferred():
                return 0

        claimed: tuple[uuid.UUID, uuid.UUID] | None = None
        async with self.session_factory() as db:
            claimed = await self.claim_job(db)
            if claimed is not None:
                await db.commit()

        if claimed is None:
            return 0

        job_id, token = claimed
        task = asyncio.create_task(
            self._run_job_wrapper(job_id, token),
            name=f"kb-pipeline-job:{job_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return 1


_default_worker: KbPipelineWorker | None = None


def get_default_worker() -> KbPipelineWorker:
    global _default_worker
    if _default_worker is None:
        _default_worker = KbPipelineWorker()
    return _default_worker


def set_default_worker(worker: KbPipelineWorker | None) -> None:
    global _default_worker
    _default_worker = worker
