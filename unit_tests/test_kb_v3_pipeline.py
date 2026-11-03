"""KB v3 자동 지식화 파이프라인 단위 및 상태 머신 회귀 테스트 (DB-free in-memory SQLite).

테스트 대상:
1. 멱등 등록/수집 큐잉 (enqueue_pipeline_job)
2. 정상 6단계 파이프라인 (parsing -> chunking -> extracting -> linking -> indexing -> canonicalizing -> ready)
3. 미등록 프로세서의 안전한 보류(held)/저하(degraded) 및 capability_missing 에러 (절대 ready/succeeded 로 오인 금지)
4. attempt_token 펜싱 및 만료된 임차권의 stale writer 쓰기 차단
5. 중간 단계 재시도(retry) 시 이미 성공한 앞선 단계 재실행 방지 (from_stage 원칙)
6. 최대 재시도(max_attempts) 초과 시 영구 실패(failed) 전환
7. 워커의 조건부 claim, lease 만료 회수, 드레인 중 신규 claim 차단 (pause_claiming)
8. 한 잡의 held/failed 가 다른 잡을 가로막지 않음 (독립성)
9. 기존 패시지 원장 불변성 보호 (재청킹 시 UPDATE/DELETE 없이 기존 패시지 재활용)
10. origin_scope 및 visibility 계약 검증 (work 자료의 global 승격 금지)
11. background_consumers 및 세대 런타임 최소 등록/드레인 연동
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import MetaData, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

sqlite3.register_adapter(list, json.dumps)

from ttakkari.collector.control_models import (
    CollectorReview,
    CollectorSource,
    KbDocumentAccess,
    KbObservation,
)
from ttakkari.kb_library import register_text
from ttakkari.kb_models import KbDocument, KbPassage, KbRevision, KbSource


@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(type_, compiler, **kw):
    return "TEXT"


# 원장 불변 트리거 등 PostgreSQL 전용 after_create DDL 은 공유 Table 객체에 붙어 있다. 여기서 그 이벤트를 지우면 같은 프로세스의
# PostgreSQL 시험 스키마(create_all)에서도 트리거가 사라진다(원장 불변 시험이 조용히 실패). 그래서 SQLite 표는 이벤트 없는 사본으로 만든다.
from ttakkari.kb_pipeline import (
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
    STAGE_STATUS_HELD,
    STAGE_STATUS_PENDING,
    STAGE_STATUS_SUCCEEDED,
    DefaultChunkingProcessor,
    KbPipelineOrchestrator,
    KbPipelineWorker,
    MissingStageProcessor,
    StageContext,
    StageProcessorRegistry,
    StageResult,
    StaleWriterError,
    enqueue_pipeline_job,
    get_default_worker,
)
from ttakkari.kb_pipeline_models import KbPipelineJob, KbPipelineStage
from ttakkari.models import utcnow


@pytest.fixture
async def pipeline_db():
    """SQLite in-memory 비동기 DB 엔진 및 세션 팩토리."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _add_sqlite_custom_functions(dbapi_con, con_record):
        dbapi_con.create_function(
            "pg_advisory_xact_lock_shared", 1, lambda val: None
        )
        dbapi_con.create_function(
            "pg_advisory_xact_lock", 1, lambda val: None
        )
        dbapi_con.create_function(
            "hashtextextended", 2, lambda val, seed: 1
        )

    sqlite_meta = MetaData()
    pending = [
        _m.__table__
        for _m in (
            KbDocument,
            KbRevision,
            KbSource,
            KbPassage,
            KbDocumentAccess,
            KbPipelineJob,
            KbPipelineStage,
            CollectorSource,
            CollectorReview,
            KbObservation,
        )
    ]
    while pending:  # FK 대상 표까지 사본으로 만든다(to_metadata 사본에는 PG 전용 DDL 이벤트가 따라오지 않는다)
        _tbl = pending.pop()
        if _tbl.name in sqlite_meta.tables:
            continue
        _tbl.to_metadata(sqlite_meta)
        pending.extend(_fk.column.table for _fk in _tbl.foreign_keys)
    async with engine.begin() as conn:
        await conn.run_sync(sqlite_meta.create_all)

    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


class FakeSuccessProcessor:
    def __init__(self, stage_name: str) -> None:
        self.stage_name = stage_name
        self.called_count = 0

    async def __call__(self, ctx: StageContext) -> StageResult:
        self.called_count += 1
        return StageResult.success(
            detail={"stage": self.stage_name, "call_count": self.called_count},
            version="fake-v1",
        )


class FakeFailingProcessor:
    def __init__(self, fail_until_attempt: int = 1) -> None:
        self.fail_until_attempt = fail_until_attempt
        self.called_count = 0

    async def __call__(self, ctx: StageContext) -> StageResult:
        self.called_count += 1
        if self.called_count <= self.fail_until_attempt:
            return StageResult.failed(
                f"simulated failure at call {self.called_count}"
            )
        return StageResult.success(detail={"recovered": True})


@pytest.mark.asyncio
async def test_enqueue_pipeline_job_idempotent(pipeline_db):
    """동일 revision_id 에 대해 여러 번 enqueue 해도 멱등하게 1개의 작업만 유지된다."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        job1 = await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id
        )
        await db.commit()

    async with pipeline_db() as db:
        job2 = await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id
        )
        await db.commit()

    assert job1.id == job2.id

    async with pipeline_db() as db:
        jobs = list(await db.scalars(select(KbPipelineJob)))
        stages = list(await db.scalars(select(KbPipelineStage)))
        assert len(jobs) == 1
        assert len(stages) == 6  # 6개 단계가 모두 pending 으로 등록됨
        assert jobs[0].status == JOB_STATUS_REGISTERED
        assert jobs[0].current_stage == STAGE_PARSING


@pytest.mark.asyncio
async def test_normal_stage_pipeline_to_ready(pipeline_db):
    """6단계 프로세서가 모두 정상 처리되면 작업이 ready 로 전이되고 메트릭이 기록된다."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        job = await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id
        )
        token = uuid.uuid4()
        job.attempt_token = token
        job.status = STAGE_PARSING
        await db.commit()
        job_id = job.id

    # 모든 단계에 fake 성공 프로세서 주입
    registry = StageProcessorRegistry()
    for stage in PIPELINE_STAGES:
        registry.register(stage, FakeSuccessProcessor(stage))

    orchestrator = KbPipelineOrchestrator(
        session_factory=pipeline_db, registry=registry
    )
    final_status = await orchestrator.execute_job(job_id, token)

    assert final_status == JOB_STATUS_READY

    async with pipeline_db() as db:
        job_after = await db.get(KbPipelineJob, job_id)
        assert job_after.status == JOB_STATUS_READY
        assert job_after.completed_at is not None
        assert job_after.claimed_by is None
        assert job_after.lease_expires_at is None

        stages = list(
            await db.scalars(
                select(KbPipelineStage).where(
                    KbPipelineStage.job_id == job_id
                )
            )
        )
        assert len(stages) == 6
        for st in stages:
            assert st.status == STAGE_STATUS_SUCCEEDED
            assert st.duration_ms is not None
            assert st.finished_at is not None


def _registry_with(**procs):
    """기본 레지스트리 위에 단계별 가짜 처리기를 올린다. 키는 단계 이름."""
    registry = StageProcessorRegistry()
    for stage, proc in procs.items():
        registry.register(stage, proc)
    return registry


async def _new_job(pipeline_db, *, max_attempts=3, attempt_count=1):
    async with pipeline_db() as db:
        job = await enqueue_pipeline_job(
            db, document_id=uuid.uuid4(), revision_id=uuid.uuid4(), max_attempts=max_attempts
        )
        token = uuid.uuid4()
        job.attempt_token = token
        job.attempt_count = attempt_count
        await db.commit()
        return job.id, token


async def _stage_statuses(pipeline_db, job_id):
    async with pipeline_db() as db:
        return {
            s.stage: s.status
            for s in await db.scalars(select(KbPipelineStage).where(KbPipelineStage.job_id == job_id))
        }


@pytest.mark.asyncio
async def test_missing_processor_held_not_ready(pipeline_db):
    """미구현 단계(extracting)는 ready/no-op 으로 위장하지 않는다. 검색 색인(indexing)은 막히지 않고 잡은 degraded 로 끝난다."""
    job_id, token = await _new_job(pipeline_db)
    registry = _registry_with(
        **{
            STAGE_PARSING: FakeSuccessProcessor(STAGE_PARSING),
            STAGE_CHUNKING: FakeSuccessProcessor(STAGE_CHUNKING),
            STAGE_INDEXING: FakeSuccessProcessor(STAGE_INDEXING),
            STAGE_EXTRACTING: MissingStageProcessor(STAGE_EXTRACTING),
            STAGE_LINKING: MissingStageProcessor(STAGE_LINKING),
            STAGE_CANONICALIZING: MissingStageProcessor(STAGE_CANONICALIZING),
        }
    )
    orchestrator = KbPipelineOrchestrator(session_factory=pipeline_db, registry=registry)
    final_status = await orchestrator.execute_job(job_id, token)

    assert final_status == JOB_STATUS_DEGRADED

    async with pipeline_db() as db:
        job_after = await db.get(KbPipelineJob, job_id)
        assert job_after.status == JOB_STATUS_DEGRADED
        assert job_after.status != JOB_STATUS_READY
        assert job_after.hold_reason == "extracting"
        assert "capability_missing" in (job_after.error_detail or "")
        assert job_after.metrics["stage_statuses"][STAGE_INDEXING] == STAGE_STATUS_SUCCEEDED

    stages = await _stage_statuses(pipeline_db, job_id)
    assert stages[STAGE_PARSING] == STAGE_STATUS_SUCCEEDED
    assert stages[STAGE_CHUNKING] == STAGE_STATUS_SUCCEEDED
    assert stages[STAGE_EXTRACTING] == STAGE_STATUS_HELD
    assert stages[STAGE_INDEXING] == STAGE_STATUS_SUCCEEDED  # 추출 보류가 색인을 막지 않았다
    assert stages[STAGE_LINKING] == "skipped"  # 의존 단계가 충족되지 않아 건너뜀(성공으로 위장 안 함)
    assert stages[STAGE_CANONICALIZING] == "skipped"


@pytest.mark.asyncio
async def test_core_stage_held_holds_the_job(pipeline_db):
    """핵심 단계(chunking)가 보류되면 잡 전체가 held 이고 뒤 단계는 실행되지 않는다."""
    job_id, token = await _new_job(pipeline_db)
    indexing = FakeSuccessProcessor(STAGE_INDEXING)
    registry = _registry_with(
        **{
            STAGE_PARSING: FakeSuccessProcessor(STAGE_PARSING),
            STAGE_CHUNKING: MissingStageProcessor(STAGE_CHUNKING),
            STAGE_INDEXING: indexing,
        }
    )
    final = await KbPipelineOrchestrator(session_factory=pipeline_db, registry=registry).execute_job(job_id, token)
    assert final == JOB_STATUS_HELD
    assert indexing.called_count == 0
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        assert job.status == JOB_STATUS_HELD and job.hold_reason == "chunking"


@pytest.mark.asyncio
async def test_extraction_failure_does_not_block_indexing(pipeline_db):
    """추출이 계속 실패해도 같은 패스에서 indexing 은 끝나고, 시도를 다 쓰면 잡은 failed 가 아니라 degraded 다."""
    job_id, token = await _new_job(pipeline_db, max_attempts=2, attempt_count=2)
    indexing = FakeSuccessProcessor(STAGE_INDEXING)
    registry = _registry_with(
        **{
            STAGE_PARSING: FakeSuccessProcessor(STAGE_PARSING),
            STAGE_CHUNKING: FakeSuccessProcessor(STAGE_CHUNKING),
            STAGE_EXTRACTING: FakeFailingProcessor(fail_until_attempt=999),
            STAGE_LINKING: FakeSuccessProcessor(STAGE_LINKING),
            STAGE_INDEXING: indexing,
            STAGE_CANONICALIZING: FakeSuccessProcessor(STAGE_CANONICALIZING),
        }
    )
    final = await KbPipelineOrchestrator(session_factory=pipeline_db, registry=registry).execute_job(job_id, token)
    assert final == JOB_STATUS_DEGRADED
    assert indexing.called_count == 1
    stages = await _stage_statuses(pipeline_db, job_id)
    assert stages[STAGE_EXTRACTING] == "failed"
    assert stages[STAGE_LINKING] == "skipped"
    assert stages[STAGE_INDEXING] == STAGE_STATUS_SUCCEEDED
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        assert job.error_code == "max_attempts_exceeded" and job.hold_reason == "simulated failure at call 1"[:64]


@pytest.mark.asyncio
async def test_core_stage_failure_with_attempts_left_schedules_retry_and_runs_nothing_after(pipeline_db):
    job_id, token = await _new_job(pipeline_db, max_attempts=3, attempt_count=1)
    chunking_after = FakeSuccessProcessor(STAGE_INDEXING)
    registry = _registry_with(
        **{
            STAGE_PARSING: FakeFailingProcessor(fail_until_attempt=999),
            STAGE_INDEXING: chunking_after,
        }
    )
    final = await KbPipelineOrchestrator(
        session_factory=pipeline_db, registry=registry, base_backoff_seconds=0.1
    ).execute_job(job_id, token)
    assert final == "retry_scheduled"
    assert chunking_after.called_count == 0
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        assert job.status == JOB_STATUS_REGISTERED and job.current_stage == STAGE_PARSING


@pytest.mark.asyncio
async def test_degraded_stage_provides_partial_availability(pipeline_db):
    """degraded 모드 설정 시 작업이 degraded 로 완료되어 부분 검색 가용성을 제공한다."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        job = await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id
        )
        token = uuid.uuid4()
        job.attempt_token = token
        await db.commit()
        job_id = job.id

    registry = StageProcessorRegistry()
    registry.register(STAGE_PARSING, FakeSuccessProcessor(STAGE_PARSING))
    registry.register(STAGE_CHUNKING, FakeSuccessProcessor(STAGE_CHUNKING))
    # extracting 단계를 degraded(부분 결과)로 반환하도록 설정. 부분 결과가 있으므로 linking/indexing 은 계속 진행한다.
    registry.register(
        STAGE_EXTRACTING,
        MissingStageProcessor(STAGE_EXTRACTING, as_degraded=True),
    )
    registry.register(STAGE_LINKING, FakeSuccessProcessor(STAGE_LINKING))
    registry.register(STAGE_INDEXING, FakeSuccessProcessor(STAGE_INDEXING))
    registry.register(STAGE_CANONICALIZING, FakeSuccessProcessor(STAGE_CANONICALIZING))

    orchestrator = KbPipelineOrchestrator(
        session_factory=pipeline_db, registry=registry
    )
    final_status = await orchestrator.execute_job(job_id, token)

    assert final_status == JOB_STATUS_DEGRADED

    async with pipeline_db() as db:
        job_after = await db.get(KbPipelineJob, job_id)
        assert job_after.status == JOB_STATUS_DEGRADED
        assert job_after.completed_at is not None


@pytest.mark.asyncio
async def test_attempt_token_fencing_rejects_stale_writer(pipeline_db):
    """다른 워커가 작업을 재임차하여 attempt_token 이 바뀌면 오래된 워커의 쓰기는 StaleWriterError 로 거부된다."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        job = await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id
        )
        old_token = uuid.uuid4()
        new_token = uuid.uuid4()
        job.attempt_token = new_token  # DB 에는 새 토큰이 기록됨
        await db.commit()
        job_id = job.id

    registry = StageProcessorRegistry()
    orchestrator = KbPipelineOrchestrator(
        session_factory=pipeline_db, registry=registry
    )

    # 이전 토큰(old_token)으로 실행 시도 시 거부되어야 함
    with pytest.raises(StaleWriterError):
        await orchestrator.execute_job(job_id, old_token)


@pytest.mark.asyncio
async def test_retry_from_stage_does_not_repeat_succeeded_stages(pipeline_db):
    """중간 단계에서 실패 후 재시도할 때, 앞서 이미 성공한 단계는 다시 실행하지 않는다 (from_stage 원칙)."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        job = await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id, max_attempts=3
        )
        token = uuid.uuid4()
        job.attempt_token = token
        job.attempt_count = 1
        await db.commit()
        job_id = job.id

    parsing_proc = FakeSuccessProcessor(STAGE_PARSING)
    chunking_proc = FakeSuccessProcessor(STAGE_CHUNKING)
    # extracting 단계에서 1회차 실패 후 2회차에 성공
    extracting_proc = FakeFailingProcessor(fail_until_attempt=1)
    linking_proc = FakeSuccessProcessor(STAGE_LINKING)
    indexing_proc = FakeSuccessProcessor(STAGE_INDEXING)
    canonicalizing_proc = FakeSuccessProcessor(STAGE_CANONICALIZING)

    registry = StageProcessorRegistry()
    registry.register(STAGE_PARSING, parsing_proc)
    registry.register(STAGE_CHUNKING, chunking_proc)
    registry.register(STAGE_EXTRACTING, extracting_proc)
    registry.register(STAGE_LINKING, linking_proc)
    registry.register(STAGE_INDEXING, indexing_proc)
    registry.register(STAGE_CANONICALIZING, canonicalizing_proc)

    orchestrator = KbPipelineOrchestrator(
        session_factory=pipeline_db,
        registry=registry,
        base_backoff_seconds=0.1,
    )

    # 1회차 실행: extracting 에서 실패 -> 재시도 예약
    status1 = await orchestrator.execute_job(job_id, token)
    assert status1 == "retry_scheduled"
    assert parsing_proc.called_count == 1
    assert chunking_proc.called_count == 1
    assert extracting_proc.called_count == 1

    async with pipeline_db() as db:
        job_mid = await db.get(KbPipelineJob, job_id)
        assert job_mid.status == JOB_STATUS_REGISTERED
        assert job_mid.current_stage == STAGE_EXTRACTING
        # 2회차 실행을 위한 새 토큰 발급 모사
        token2 = uuid.uuid4()
        job_mid.attempt_token = token2
        job_mid.attempt_count = 2
        await db.commit()

    # 2회차 실행: parsing 과 chunking 은 이미 succeeded 이므로 건너뛰고 extracting 부터 재개!
    status2 = await orchestrator.execute_job(job_id, token2)
    assert status2 == JOB_STATUS_READY

    # parsing 과 chunking 은 다시 호출되지 않았어야 함 (called_count 가 여전히 1)
    assert parsing_proc.called_count == 1
    assert chunking_proc.called_count == 1
    # extracting 은 2번째 호출되어 성공
    assert extracting_proc.called_count == 2
    assert linking_proc.called_count == 1


@pytest.mark.asyncio
async def test_max_attempts_exceeded_fails_permanently(pipeline_db):
    """max_attempts 를 초과하여 실패하면 잡이 failed 상태로 영구 종료된다."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        job = await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id, max_attempts=2
        )
        token = uuid.uuid4()
        job.attempt_token = token
        job.attempt_count = 2  # 이미 최대 시도 횟수 도달
        await db.commit()
        job_id = job.id

    registry = StageProcessorRegistry()
    registry.register(
        STAGE_PARSING, FakeFailingProcessor(fail_until_attempt=999)
    )

    orchestrator = KbPipelineOrchestrator(
        session_factory=pipeline_db, registry=registry
    )
    final_status = await orchestrator.execute_job(job_id, token)

    assert final_status == JOB_STATUS_FAILED

    async with pipeline_db() as db:
        job_after = await db.get(KbPipelineJob, job_id)
        assert job_after.status == JOB_STATUS_FAILED
        assert job_after.error_code == "max_attempts_exceeded"
        assert job_after.completed_at is not None


@pytest.mark.asyncio
async def test_worker_claim_and_lease_expiration_recovery(pipeline_db):
    """워커가 작업을 선점하고, lease 가 만료되면 다른 워커가 회수하여 claim 할 수 있다."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id
        )
        await db.commit()

    worker1 = KbPipelineWorker(
        session_factory=pipeline_db,
        worker_id="worker-1",
        lease_seconds=1.0,
    )

    # 1. worker1 이 정상 선점
    async with pipeline_db() as db:
        claimed1 = await worker1.claim_job(db)
        await db.commit()

    assert claimed1 is not None
    job_id, token1 = claimed1

    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        assert job.claimed_by == "worker-1"
        assert job.attempt_count == 1
        assert job.attempt_token == token1

    # 아직 lease 가 살아있을 때 worker2 는 claim 할 수 없음
    worker2 = KbPipelineWorker(
        session_factory=pipeline_db, worker_id="worker-2"
    )
    async with pipeline_db() as db:
        claimed2 = await worker2.claim_job(db)
        await db.commit()
    assert claimed2 is None

    # lease 를 과거로 강제 만료시켜 회수 가능하게 만듦
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        job.lease_expires_at = utcnow() - timedelta(seconds=10)
        await db.commit()

    # 2. worker2 가 만료된 임차권을 회수하여 선점
    async with pipeline_db() as db:
        claimed2 = await worker2.claim_job(db)
        await db.commit()

    assert claimed2 is not None
    job_id2, token2 = claimed2
    assert job_id2 == job_id
    assert token2 != token1  # 새로운 펜싱 토큰 부여

    async with pipeline_db() as db:
        job_recovered = await db.get(KbPipelineJob, job_id)
        assert job_recovered.claimed_by == "worker-2"
        assert job_recovered.attempt_count == 2
        assert job_recovered.attempt_token == token2


@pytest.mark.asyncio
async def test_worker_pause_claiming_stops_new_claims(pipeline_db):
    """pause_claiming() 상태에서는 pump() 가 새 작업을 선점하지 않는다."""
    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    async with pipeline_db() as db:
        await enqueue_pipeline_job(
            db, document_id=doc_id, revision_id=rev_id
        )
        await db.commit()

    worker = KbPipelineWorker(session_factory=pipeline_db)
    worker.pause_claiming()

    assert await worker.pump() == 0

    worker.resume_claiming()
    # 일감을 가져갈 수 있게 됨
    async with pipeline_db() as db:
        claimed = await worker.claim_job(db)
        assert claimed is not None


@pytest.mark.asyncio
async def test_failed_or_held_job_does_not_block_subsequent_jobs(pipeline_db):
    """한 작업이 held 상태가 되어도 다음 pump 에서 다른 작업이 정상 처리된다 (잡 간 독립성)."""
    doc1 = uuid.uuid4()
    rev1 = uuid.uuid4()
    doc2 = uuid.uuid4()
    rev2 = uuid.uuid4()

    async with pipeline_db() as db:
        await enqueue_pipeline_job(db, document_id=doc1, revision_id=rev1)
        await enqueue_pipeline_job(db, document_id=doc2, revision_id=rev2)
        await db.commit()

    registry = StageProcessorRegistry()
    registry.register(STAGE_PARSING, FakeSuccessProcessor(STAGE_PARSING))
    registry.register(STAGE_CHUNKING, FakeSuccessProcessor(STAGE_CHUNKING))
    registry.register(STAGE_INDEXING, FakeSuccessProcessor(STAGE_INDEXING))
    registry.register(STAGE_EXTRACTING, MissingStageProcessor(STAGE_EXTRACTING))
    # 첫 번째 잡은 extracting 보류(degraded)가 되고, 두 번째 잡도 독립적으로 pump 가능
    worker = KbPipelineWorker(
        session_factory=pipeline_db,
        orchestrator=KbPipelineOrchestrator(
            session_factory=pipeline_db, registry=registry
        ),
    )

    # 1번째 잡 pump
    assert await worker.pump() == 1
    # 2번째 잡 pump
    assert await worker.pump() == 1
    # 더 이상 대기 잡 없음
    assert await worker.pump() == 0

    # 백그라운드 태스크 완료 대기
    if worker._tasks:
        await asyncio.gather(*worker._tasks)

    async with pipeline_db() as db:
        jobs = list(
            await db.scalars(
                select(KbPipelineJob).order_by(KbPipelineJob.created_at)
            )
        )
        assert len(jobs) == 2
        # 두 작업 모두 missing extracting 단계로 degraded(색인은 끝남)가 되었으며 서로 방해하지 않음
        assert jobs[0].status == JOB_STATUS_DEGRADED
        assert jobs[1].status == JOB_STATUS_DEGRADED


@pytest.mark.asyncio
async def test_chunking_processor_preserves_immutable_ledger(pipeline_db):
    """DefaultChunkingProcessor 는 이미 패시지가 존재하는 경우 원장 UPDATE/DELETE 없이 기존 행을 재활용한다."""
    rev_id = uuid.uuid4()
    p_id = uuid.uuid4()

    async with pipeline_db() as db:
        # 기존 패시지가 이미 1개 적재되어 있는 상황 모사
        passage = KbPassage(
            id=p_id,
            revision_id=rev_id,
            ord=0,
            body="Existing immutable passage text",
            char_start=0,
            char_end=31,
            sha256="dummy_sha256",
        )
        db.add(passage)
        await db.commit()

    processor = DefaultChunkingProcessor()
    ctx = StageContext(
        job_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        revision_id=rev_id,
        stage=STAGE_CHUNKING,
        attempt_token=uuid.uuid4(),
        session_factory=pipeline_db,
    )

    result = await processor(ctx)
    assert result.status == STAGE_STATUS_SUCCEEDED
    assert result.detail.get("reused_existing") is True
    assert result.detail.get("passage_count") == 1
    assert result.detail.get("immutable_preserved") is True

    async with pipeline_db() as db:
        passages = list(
            await db.scalars(
                select(KbPassage).where(KbPassage.revision_id == rev_id)
            )
        )
        assert len(passages) == 1
        assert passages[0].body == "Existing immutable passage text"


@pytest.mark.asyncio
async def test_access_scope_unification_and_work_boundary():
    """work 스코프 자료는 origin_scope='work', visibility='work_only' 가 되며 global 로 승격되지 않는다.

    또한 derive_scope_and_visibility 헬퍼를 통해 fail-closed 규칙을 엄격히 준수한다.
    """
    from ttakkari.kb_pipeline import derive_scope_and_visibility

    # 1. access 행이 전혀 없는 경우 -> fail-closed ('work', 'work_only')
    assert derive_scope_and_visibility(None) == ("work", "work_only")

    # 2. scope == 'work' 인 경우 -> 무조건 ('work', 'work_only') 강제 (global 승격 불가)
    doc_work = uuid.uuid4()
    access_work = KbDocumentAccess(
        document_id=doc_work,
        tenant_id="owner",
        scope="work",
        origin_scope="work",
        visibility="work_only",
    )
    assert access_work.origin_scope == "work"
    assert access_work.visibility == "work_only"
    assert derive_scope_and_visibility(access_work) == ("work", "work_only")

    # 만약 불법적으로 visibility='global'로 설정하려 하면 모델 계층에서 fail-closed 차단 (ValueError)
    with pytest.raises(ValueError, match="Work scope document access cannot have global visibility"):
        KbDocumentAccess(
            document_id=uuid.uuid4(),
            tenant_id="owner",
            scope="work",
            origin_scope="work",
            visibility="global",
        )

    # 또한 임의의 우회 객체가 전달되더라도 derive_scope_and_visibility 헬퍼는 work_only 강제
    from types import SimpleNamespace

    tampered_access = SimpleNamespace(
        scope="work",
        origin_scope="work",
        visibility="global",
    )
    assert derive_scope_and_visibility(tampered_access) == ("work", "work_only")

    # 3. personal 스코프이면서 명시적으로 global 설정된 경우 -> ('personal', 'global')
    doc_personal = uuid.uuid4()
    access_personal = KbDocumentAccess(
        document_id=doc_personal,
        tenant_id="owner",
        scope="personal",
        origin_scope="personal",
        visibility="global",
    )
    assert access_personal.origin_scope == "personal"
    assert access_personal.visibility == "global"
    assert derive_scope_and_visibility(access_personal) == ("personal", "global")

    # 4. personal 스코프이지만 visibility 가 비어있거나 누락/None 인 경우 -> 통합 공간을 위해 ('personal', 'global') 로 파생
    access_personal_empty = KbDocumentAccess(
        document_id=uuid.uuid4(),
        tenant_id="owner",
        scope="personal",
        origin_scope="personal",
        visibility="",
    )
    assert derive_scope_and_visibility(access_personal_empty) == ("personal", "global")

    access_personal_none = KbDocumentAccess(
        document_id=uuid.uuid4(),
        tenant_id="owner",
        scope="personal",
        origin_scope="personal",
        visibility=None,
    )
    assert derive_scope_and_visibility(access_personal_none) == ("personal", "global")

    # 5. personal 스코프이지만 명시적으로 visibility='work_only' 로 설정된 경우 -> ('personal', 'work_only')
    access_personal_explicit_work = KbDocumentAccess(
        document_id=uuid.uuid4(),
        tenant_id="owner",
        scope="personal",
        origin_scope="personal",
        visibility="work_only",
    )
    assert derive_scope_and_visibility(access_personal_explicit_work) == ("personal", "work_only")


@pytest.mark.asyncio
async def test_background_consumers_and_generation_runtime_integration():
    """background_consumers 의 PUMPS 에 kb-pipeline 이 등록되어 있고, pause 및 inflight 가 연동된다."""
    from ttakkari import background_consumers

    pump_names = [name for name, _ in background_consumers.PUMPS]
    assert "kb-pipeline" in pump_names

    worker = get_default_worker()
    worker.resume_claiming()

    background_consumers.consumers.pause_claiming()
    assert worker.claiming_paused is True

    background_consumers.consumers.resume_claiming()
    assert worker.claiming_paused is False


@pytest.mark.asyncio
async def test_register_text_queues_pipeline_job_and_sets_scope(pipeline_db):
    """register_text 실행 시 같은 트랜잭션에서 KbPipelineJob 이 큐잉되고 access 스코프가 보존된다."""
    async with pipeline_db() as db:
        res = await register_text(
            db,
            title="Kubernetes Architecture Spec",
            text="# Kubernetes Architecture\nDeployment manages replica sets and pods.",
            source_key=None,
            lang="en",
            locator=None,
            scope="work",
        )
        await db.commit()

        # 1. access 행의 origin_scope 및 visibility 확인 (work_only 유지, global 승격 금지)
        access = await db.get(KbDocumentAccess, res.document_id)
        assert access is not None
        assert access.scope == "work"
        assert access.origin_scope == "work"
        assert access.visibility == "work_only"

        # 2. 동일 트랜잭션 내 큐잉된 KbPipelineJob 확인
        job = await db.scalar(
            select(KbPipelineJob).where(
                KbPipelineJob.revision_id == res.revision_id
            )
        )
        assert job is not None
        assert job.document_id == res.document_id
        assert job.status == JOB_STATUS_REGISTERED
        assert job.current_stage == STAGE_PARSING

        # 3. 6개 단계가 모두 pending 으로 초기화되었는지 확인
        stages = list(
            await db.scalars(
                select(KbPipelineStage).where(
                    KbPipelineStage.job_id == job.id
                )
            )
        )
        assert len(stages) == 6
        assert all(s.status == STAGE_STATUS_PENDING for s in stages)


@pytest.mark.asyncio
async def test_collector_delivery_queues_pipeline_job(pipeline_db, monkeypatch):
    """kb_delivery.deliver_item 호출 성공 시 동일 트랜잭션에서 KbPipelineJob 이 큐잉된다."""
    from unittest.mock import AsyncMock, MagicMock

    from ttakkari import kb_delivery

    doc_id = uuid.uuid4()
    rev_id = uuid.uuid4()

    # validate_envelope, compute_payload_hash, active_policy 등 collector 선행 조건 mock
    monkeypatch.setattr(
        kb_delivery, "validate_envelope", lambda env: env
    )
    monkeypatch.setattr(
        kb_delivery, "compute_payload_hash", lambda env: "dummy_hash"
    )
    monkeypatch.setattr(
        kb_delivery.store, "_lock", AsyncMock()
    )
    mock_policy = MagicMock()
    mock_policy.allows = MagicMock(return_value=True)
    monkeypatch.setattr(
        kb_delivery.store, "active_policy", AsyncMock(return_value=(mock_policy,))
    )
    monkeypatch.setattr(
        kb_delivery, "default_blob_store", lambda: MagicMock(get=lambda handle: b"blob content")
    )
    monkeypatch.setattr(
        kb_delivery, "hashlib", MagicMock(sha256=lambda b: MagicMock(hexdigest=lambda: "dummy_sha"))
    )

    ingest_res = MagicMock(document_id=doc_id, revision_id=rev_id, created=True)
    monkeypatch.setattr(
        kb_delivery, "ingest_source", AsyncMock(return_value=ingest_res)
    )
    monkeypatch.setattr(
        kb_delivery, "segment_revision", AsyncMock(return_value=[])
    )

    item = MagicMock(
        id=uuid.uuid4(),
        job_id=uuid.uuid4(),
        tenant_id="owner",
        scope="work",
        source_key="src_key",
        url="https://example.com/spec",
        payload_hash="dummy_hash",
        text_sha256="dummy_sha",
        envelope={
            "access": {"tenant_id": "owner", "scope": "work", "principal_ids": ["owner"]},
            "rights": {"permission": "allowed", "retain_until": None, "purposes": ["knowledge"]},
            "decision": {"state": "accept"},
            "normalized": {"truncated": False, "language": "en", "text": "body", "parser_version": "v1", "normalizer_version": "v1", "chunker_version": "chunk-v1", "sha256": "norm_sha", "raw_alignment_handle": "align"},
            "raw": {"blob_handle": "handle", "byte_size": len(b"blob content"), "sha256": "dummy_sha", "media_type": "text/html"},
            "source": {"url": "https://example.com/spec", "collected_at": "2026-10-25T00:00:00Z"},
        },
    )
    outbox = MagicMock(
        id=uuid.uuid4(),
        envelope_id=uuid.uuid4(),
        tenant_id="owner",
        scope="work",
        payload_hash="dummy_hash",
        sink="knowledge",
    )

    async with pipeline_db() as db:
        src = CollectorSource(
            id=uuid.uuid4(),
            tenant_id="owner",
            scope="work",
            source_key="src_key",
            title="Test Source",
            auto_import=True,
            request={"url": "https://example.com/spec"},
        )
        db.add(src)
        await db.commit()

        res_rev_id, outcome, _detail = await kb_delivery.deliver_item(db, outbox, item)
        await db.commit()

        assert res_rev_id == rev_id
        assert outcome == "imported"

        # access 행 검증
        access = await db.get(KbDocumentAccess, doc_id)
        assert access is not None
        assert access.scope == "personal"  # C2 single storage namespace
        assert access.pool == "work"
        assert access.origin_scope == "work"
        assert access.visibility == "work_only"

        # 큐잉된 파이프라인 잡 검증
        job = await db.scalar(
            select(KbPipelineJob).where(
                KbPipelineJob.revision_id == rev_id
            )
        )
        assert job is not None
        assert job.document_id == doc_id
        assert job.status == JOB_STATUS_REGISTERED
        assert job.current_stage == STAGE_PARSING


# ---------------------------------------------------------------------------
# retry_pipeline_job / fenced writes / default registry wiring
# ---------------------------------------------------------------------------
from ttakkari.kb_pipeline import PipelineRetryError, retry_pipeline_job


async def _run_to_degraded(pipeline_db):
    job_id, token = await _new_job(pipeline_db)
    procs = {
        STAGE_PARSING: FakeSuccessProcessor(STAGE_PARSING),
        STAGE_CHUNKING: FakeSuccessProcessor(STAGE_CHUNKING),
        STAGE_EXTRACTING: FakeFailingProcessor(fail_until_attempt=1),
        STAGE_LINKING: FakeSuccessProcessor(STAGE_LINKING),
        STAGE_INDEXING: FakeSuccessProcessor(STAGE_INDEXING),
        STAGE_CANONICALIZING: FakeSuccessProcessor(STAGE_CANONICALIZING),
    }
    # max_attempts=1: 첫 실패가 곧 최종이라 degraded 로 끝난다.
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        job.max_attempts = 1
        await db.commit()
    orch = KbPipelineOrchestrator(session_factory=pipeline_db, registry=_registry_with(**procs))
    assert await orch.execute_job(job_id, token) == JOB_STATUS_DEGRADED
    return job_id, token, procs, orch


@pytest.mark.asyncio
async def test_retry_pipeline_job_from_stage_reruns_only_from_that_stage(pipeline_db):
    job_id, old_token, procs, orch = await _run_to_degraded(pipeline_db)
    assert procs[STAGE_PARSING].called_count == 1

    outcome = await retry_pipeline_job(pipeline_db, job_id, from_stage=STAGE_EXTRACTING)
    assert outcome.requeued and outcome.from_stage == STAGE_EXTRACTING
    assert outcome.reset_stages == (STAGE_EXTRACTING, STAGE_LINKING, STAGE_INDEXING, STAGE_CANONICALIZING)

    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        assert job.status == JOB_STATUS_REGISTERED and job.current_stage == STAGE_EXTRACTING
        assert job.attempt_token not in (None, old_token)
        assert job.attempt_count == 0 and job.claimed_by is None and job.error_code is None
        new_token = job.attempt_token
        job.max_attempts = 3
        await db.commit()
    stages = await _stage_statuses(pipeline_db, job_id)
    assert stages[STAGE_PARSING] == STAGE_STATUS_SUCCEEDED and stages[STAGE_CHUNKING] == STAGE_STATUS_SUCCEEDED
    assert stages[STAGE_EXTRACTING] == STAGE_STATUS_PENDING and stages[STAGE_INDEXING] == STAGE_STATUS_PENDING

    # 옛 토큰의 실행자는 막힌다
    with pytest.raises(StaleWriterError):
        await orch.execute_job(job_id, old_token)

    assert await orch.execute_job(job_id, new_token) == JOB_STATUS_READY
    assert procs[STAGE_PARSING].called_count == 1  # 앞선 성공 단계는 건너뛴다
    assert procs[STAGE_CHUNKING].called_count == 1
    assert procs[STAGE_EXTRACTING].called_count == 2  # failed 1회 + 재시도 성공
    assert procs[STAGE_INDEXING].called_count == 2  # from_stage 이후는 다시 실행


@pytest.mark.asyncio
async def test_retry_pipeline_job_is_idempotent_and_default_resumes_at_first_unsettled(pipeline_db):
    job_id, _tok, _procs, _orch = await _run_to_degraded(pipeline_db)
    first = await retry_pipeline_job(pipeline_db, job_id)
    assert first.requeued and first.from_stage == STAGE_EXTRACTING
    async with pipeline_db() as db:
        token_after_first = (await db.get(KbPipelineJob, job_id)).attempt_token
    second = await retry_pipeline_job(pipeline_db, job_id)
    assert not second.requeued and second.reason == "already_queued"
    async with pipeline_db() as db:
        assert (await db.get(KbPipelineJob, job_id)).attempt_token == token_after_first


@pytest.mark.asyncio
async def test_retry_pipeline_job_requeues_held_and_failed_and_worker_claims_them(pipeline_db):
    # held(핵심 단계 보류)
    job_id, token = await _new_job(pipeline_db)
    registry = _registry_with(**{STAGE_PARSING: MissingStageProcessor(STAGE_PARSING)})
    assert await KbPipelineOrchestrator(session_factory=pipeline_db, registry=registry).execute_job(job_id, token) == JOB_STATUS_HELD
    # 워커는 held 를 claim 하지 않는다
    worker = KbPipelineWorker(session_factory=pipeline_db, orchestrator=KbPipelineOrchestrator(
        session_factory=pipeline_db, registry=_registry_with(**{STAGE_PARSING: FakeSuccessProcessor(STAGE_PARSING)})))
    assert await worker.pump() == 0

    out = await retry_pipeline_job(pipeline_db, job_id, from_stage=STAGE_PARSING)
    assert out.requeued
    assert await worker.pump() == 1
    if worker._tasks:
        await asyncio.gather(*worker._tasks)

    # 최종 failed(핵심 단계 실패, 시도 소진)
    job2, tok2 = await _new_job(pipeline_db, max_attempts=1, attempt_count=1)
    reg2 = _registry_with(**{STAGE_PARSING: FakeFailingProcessor(fail_until_attempt=999)})
    assert await KbPipelineOrchestrator(session_factory=pipeline_db, registry=reg2).execute_job(job2, tok2) == JOB_STATUS_FAILED
    async with pipeline_db() as db:
        assert (await db.get(KbPipelineJob, job2)).status == JOB_STATUS_FAILED
    out2 = await retry_pipeline_job(pipeline_db, job2)
    assert out2.requeued and out2.from_stage == STAGE_PARSING
    async with pipeline_db() as db:
        j = await db.get(KbPipelineJob, job2)
        assert j.status == JOB_STATUS_REGISTERED and j.attempt_count == 0 and j.error_code is None


@pytest.mark.asyncio
async def test_retry_pipeline_job_rejects_running_unknown_stage_and_missing_job(pipeline_db):
    job_id, _token = await _new_job(pipeline_db)
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        job.claimed_by = "worker-x"
        job.lease_expires_at = utcnow() + timedelta(minutes=5)
        await db.commit()
    with pytest.raises(PipelineRetryError, match="job_running"):
        await retry_pipeline_job(pipeline_db, job_id)
    with pytest.raises(PipelineRetryError, match="unknown_stage"):
        await retry_pipeline_job(pipeline_db, job_id, from_stage="nope")
    with pytest.raises(PipelineRetryError, match="job_not_found"):
        await retry_pipeline_job(pipeline_db, uuid.uuid4())
    # 임차권이 만료되면 회수 가능
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        job.lease_expires_at = utcnow() - timedelta(minutes=1)
        await db.commit()
    assert (await retry_pipeline_job(pipeline_db, job_id, from_stage=STAGE_CHUNKING)).requeued


@pytest.mark.asyncio
async def test_fenced_session_blocks_side_writes_after_token_change(pipeline_db):
    """처리기의 부수 DB 쓰기는 토큰이 바뀐 뒤에는 한 행도 남기지 못한다."""
    job_id, token = await _new_job(pipeline_db)
    rev_id = uuid.uuid4()

    def ctx_for(tok):
        return StageContext(
            job_id=job_id, document_id=uuid.uuid4(), revision_id=rev_id, stage=STAGE_CHUNKING,
            attempt_token=tok, session_factory=pipeline_db,
        )

    def passage():
        return KbPassage(id=uuid.uuid4(), revision_id=rev_id, ord=0, body="x", char_start=0, char_end=1, sha256="s")

    async with ctx_for(token).fenced_session() as db:
        db.add(passage())
    async with pipeline_db() as db:
        assert len(list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == rev_id)))) == 1

    # 다른 실행자가 재임차해 토큰이 바뀌었다
    async with pipeline_db() as db:
        job = await db.get(KbPipelineJob, job_id)
        job.attempt_token = uuid.uuid4()
        await db.commit()
    with pytest.raises(StaleWriterError):
        async with ctx_for(token).fenced_session() as db:
            db.add(KbPassage(id=uuid.uuid4(), revision_id=rev_id, ord=1, body="y", char_start=0, char_end=1, sha256="t"))
    async with pipeline_db() as db:
        assert len(list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == rev_id)))) == 1

    # 처리기 내부 예외로 빠져나가면 커밋하지 않는다
    async with pipeline_db() as db:
        live = (await db.get(KbPipelineJob, job_id)).attempt_token
    with pytest.raises(RuntimeError):
        async with ctx_for(live).fenced_session() as db:
            db.add(KbPassage(id=uuid.uuid4(), revision_id=rev_id, ord=2, body="z", char_start=0, char_end=1, sha256="u"))
            raise RuntimeError("boom")
    async with pipeline_db() as db:
        assert len(list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == rev_id)))) == 1


@pytest.mark.asyncio
async def test_stale_processor_cannot_commit_even_if_it_finishes_late(pipeline_db):
    """실행 중 토큰이 바뀐 잡의 처리기가 늦게 쓰기를 시도하면 StaleWriterError 로 잡 전체가 펜싱된다."""
    job_id, token = await _new_job(pipeline_db)
    rev_id = uuid.uuid4()

    class LateWriter:
        async def __call__(self, ctx):
            # 모델 호출이 길어지는 사이 다른 실행자가 토큰을 가져갔다
            async with pipeline_db() as db:
                job = await db.get(KbPipelineJob, ctx.job_id)
                job.attempt_token = uuid.uuid4()
                await db.commit()
            async with ctx.fenced_session() as db:
                db.add(KbPassage(id=uuid.uuid4(), revision_id=rev_id, ord=0, body="late", char_start=0, char_end=4, sha256="l"))
            return StageResult.success()

    orch = KbPipelineOrchestrator(session_factory=pipeline_db, registry=_registry_with(**{STAGE_PARSING: LateWriter()}))
    with pytest.raises(StaleWriterError):
        await orch.execute_job(job_id, token)
    async with pipeline_db() as db:
        assert list(await db.scalars(select(KbPassage).where(KbPassage.revision_id == rev_id))) == []


def test_default_registry_registers_real_processors():
    from ttakkari.kb_pipeline import ExtractingProcessor, LinkingProcessor
    from ttakkari.kb_pipeline.canonicalizing_processor import CanonicalizingProcessor

    reg = StageProcessorRegistry()
    assert isinstance(reg.get(STAGE_EXTRACTING), ExtractingProcessor)
    assert isinstance(reg.get(STAGE_LINKING), LinkingProcessor)
    assert not isinstance(reg.get(STAGE_INDEXING), MissingStageProcessor)
    # canonicals 합류 후 정본 통합도 실제 처리기다(held 위장 아님)
    assert isinstance(reg.get(STAGE_CANONICALIZING), CanonicalizingProcessor)
    from ttakkari.kb_pipeline.types import STAGE_REQUIRES

    assert STAGE_LINKING not in STAGE_REQUIRES[STAGE_INDEXING]
    assert STAGE_EXTRACTING not in STAGE_REQUIRES[STAGE_INDEXING]
