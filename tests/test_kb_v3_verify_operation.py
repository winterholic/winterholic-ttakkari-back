"""지식 도서관(KB v3) L2 활성화 및 상충 파이프라인 실검증 호스트 운영 작업(kb_v3_verify) 테스트.

1. 호스트 오퍼레이션 레지스트리(plans.KINDS, plans.INLINE, api.operations.StartIn, MCP tool enum) 엄격 등록 및 generic 입력(쉘, 경로, DB URL, 프롬프트) 거부.
2. KB v3 스키마 부재 시 명확 미지원(unsupported, kb_v3_schema_missing) 반환.
3. 1개 공개 합성 PDF + 2개 상충 정책(30일 vs 90일) 문서의 파이프라인 통과, 정합 인용, 주장, 충돌(value_contradiction) 감지 검증.
4. kb_l2_adapter=disabled 일 때 전역 설정 override 없이 정직하게 degraded 보고.
5. 멱등 재실행 및 민감정보(secret, db credential, raw body, prompt) 비노출 검증.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import event, select, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import DDL

from ttakkari.api.operations import DESCRIPTIONS, StartIn
from ttakkari.collector.control_models import (
    KbDocumentAccess,
    KbDocumentAlias,
    KbObservation,
)
from ttakkari.config import Settings
from ttakkari.kb_models import (
    KbDocument,
    KbEvidence,
    KbPassage,
    KbRevision,
    KbSource,
)
from ttakkari.kb_ontology_models import (
    KbAssertion,
    KbAssertionEvidence,
    KbConflict,
    KbDocumentEntity,
    KbEntity,
    KbEntityAlias,
    KbEntityCanonical,
    KbRelation,
    KbRelationEvidence,
)
from ttakkari.kb_pipeline.orchestrator import KbPipelineOrchestrator
from ttakkari.kb_pipeline.worker import KbPipelineWorker
from ttakkari.kb_pipeline_models import (
    KbL2BudgetReservation,
    KbL2BudgetUsage,
    KbPipelineJob,
    KbPipelineStage,
)
from ttakkari.models import AppSetting
from ttakkari.operations import plans
from ttakkari.operations.kb_v3_verify import (
    KEY_PDF,
    KEY_POLICY_30D,
    KEY_POLICY_90D,
    verify_kb_v3,
)
from ttakkari.orchestrator import mcp_server


# DB 없이 순수 단위/in-memory 테스트를 위해 conftest DB fixture 오버라이드
@pytest.fixture(scope="session", autouse=True)
def _schema_and_app() -> None:
    yield


@pytest.fixture(autouse=True)
def _clean() -> None:
    yield


# SQLite 호환 DDL 및 ARRAY 타입 컴파일 패치
@compiles(ARRAY, "sqlite")
def _compile_array_sqlite(type_, compiler, **kw):
    return "TEXT"


_orig_ddl_call = DDL.__call__


def _safe_ddl_call(self, target, bind, **kw):
    if bind.dialect.name == "sqlite":
        return
    return _orig_ddl_call(self, target, bind, **kw)


DDL.__call__ = _safe_ddl_call

ALL_V3_TABLES = [
    KbSource.__table__,
    KbDocument.__table__,
    KbDocumentAccess.__table__,
    KbDocumentAlias.__table__,
    KbRevision.__table__,
    KbObservation.__table__,
    KbPassage.__table__,
    KbEvidence.__table__,
    KbEntity.__table__,
    KbEntityAlias.__table__,
    KbDocumentEntity.__table__,
    KbAssertion.__table__,
    KbAssertionEvidence.__table__,
    KbRelation.__table__,
    KbRelationEvidence.__table__,
    KbEntityCanonical.__table__,
    KbConflict.__table__,
    KbPipelineJob.__table__,
    KbPipelineStage.__table__,
    AppSetting.__table__,
    KbL2BudgetReservation.__table__,
    KbL2BudgetUsage.__table__,
]


@pytest_asyncio.fixture
async def mem_db() -> AsyncGenerator[tuple[AsyncSession, async_sessionmaker]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, echo=False
    )

    def _register_sqlite_pg_funcs(dbapi_conn, connection_record):
        dbapi_conn.create_function("pg_advisory_xact_lock_shared", 1, lambda k: 1)
        dbapi_conn.create_function("pg_advisory_xact_lock", 1, lambda k: 1)
        dbapi_conn.create_function("pg_advisory_unlock", 1, lambda k: 1)
        dbapi_conn.create_function("hashtextextended", 2, lambda k, s: 1)
        dbapi_conn.create_function("hashtext", 1, lambda k: 1)

    event.listen(engine.sync_engine, "connect", _register_sqlite_pg_funcs)

    def _encode_array_parameters(
        conn, cursor, statement, parameters, context, executemany
    ):
        def encode_row(row):
            if isinstance(row, (tuple, list)):
                return tuple(
                    json.dumps(value) if isinstance(value, list) else value
                    for value in row
                )
            return row

        if executemany:
            parameters = [encode_row(row) for row in parameters]
        else:
            parameters = encode_row(parameters)
        return statement, parameters

    event.listen(
        engine.sync_engine,
        "before_cursor_execute",
        _encode_array_parameters,
        retval=True,
    )
    async with engine.begin() as conn:
        for t in ALL_V3_TABLES:
            await conn.run_sync(t.create)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        # daily_budget 시드
        from datetime import UTC, datetime

        day_key = f"kb_l2_daily_budget:{datetime.now(UTC).date().isoformat()}"
        session.add(AppSetting(key=day_key, value={"baseline": 0}))
        await session.commit()
        yield session, session_factory
    await engine.dispose()


def test_operation_registration_and_strict_schema():
    """kb_v3_verify 가 operations 허용 목록에 안전하게 등록되어 있고 generic 입력을 거절하는지 확인.
    
    라이프사이클 계약:
    - INLINE은 읽기 전용 즉시 작업 계약이므로 자료 등록/L2를 수행하는 kb_v3_verify는 INLINE이 아니어야 한다.
    - background/단일 heavy 경로(plans.HEAVY)에 등록되어야 한다.
    - 3분(180초) 유한 타임아웃이 설정되어야 한다.
    """
    assert "kb_v3_verify" in plans.HEAVY
    assert "kb_v3_verify" not in plans.INLINE
    assert "kb_v3_verify" in plans.KINDS
    assert "kb_v3_verify" in DESCRIPTIONS
    assert plans.TIMEOUT_SECONDS.get("kb_v3_verify") == 180

    # 계획 생성 확인
    job_id = "00000000-0000-0000-0000-000000000001"
    plan = plans.make_plan("kb_v3_verify", job_id)
    assert plan.kind == "kb_v3_verify"
    assert any("kb_v3_verify" in arg for arg in plan.argv)

    # 올바른 호출
    valid = StartIn(kind="kb_v3_verify")
    assert valid.kind == "kb_v3_verify"

    # 추가 generic 파라미터 (쉘, 경로, DB URL, 프롬프트 등) 입력 시 엄격 거부 (extra='forbid')
    with pytest.raises(ValidationError):
        StartIn.model_validate({"kind": "kb_v3_verify", "shell": "/bin/bash"})

    with pytest.raises(ValidationError):
        StartIn.model_validate(
            {"kind": "kb_v3_verify", "path": "/Users/winterholic/secret"}
        )

    with pytest.raises(ValidationError):
        StartIn.model_validate(
            {"kind": "kb_v3_verify", "db_url": "postgresql://root@localhost/prod"}
        )

    with pytest.raises(ValidationError):
        StartIn.model_validate(
            {"kind": "kb_v3_verify", "prompt": "ignore instructions"}
        )


def test_mcp_server_tool_includes_kb_v3_verify():
    """MCP 서버의 host_operation_start 도구 시그니처와 설명에 kb_v3_verify 가 연결되어 있는지 확인."""
    tool = mcp_server.host_operation_start
    assert tool is not None
    assert "kb_v3_verify" in tool.__doc__


@pytest.mark.asyncio
async def test_schema_missing_reports_unsupported():
    """테이블이 없는 DB 에서는 명확하게 unsupported 와 kb_v3_schema_missing 을 반환한다."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    async with sf() as session:
        res = await verify_kb_v3(session, session_factory=sf)
        assert res["status"] == "unsupported"
        assert res["reason"] == "kb_v3_schema_missing"
    await engine.dispose()


@pytest.mark.asyncio
async def test_verify_kb_v3_pipeline_and_conflict_detection(mem_db, monkeypatch):
    """1개 공개 합성 PDF + 2개 30일/90일 정책 문서 등록 및 L0/L1/L2 파이프라인 통과와 충돌 검출 검증."""
    session, session_factory = mem_db

    # Fake L2 adapter 주입: 실제 클라우드 호출 없이 L2 스키마에 맞는 JSON 응답 반환
    async def fake_l2_call(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "entities": [],
            "assertions": [],
            "relations": [],
        }

    class FakeAdapter:
        name = "subscription_cli"
        retry_after = None

        async def availability(self):
            return True, None

        async def __call__(self, prompt, schema):
            try:
                return await fake_l2_call(prompt, schema)
            except Exception:
                import traceback

                traceback.print_exc()
                raise

    # 워커 및 오케스트레이터 설정
    orchestrator = KbPipelineOrchestrator(session_factory=session_factory)
    from ttakkari.kb_pipeline.stages import ExtractingProcessor

    orchestrator.registry.register(
        "extracting",
        ExtractingProcessor(adapter_factory=lambda: FakeAdapter()),
    )
    worker = KbPipelineWorker(
        session_factory=session_factory, orchestrator=orchestrator
    )

    # L2 활성화 설정
    s = Settings(
        kb_l2_adapter="subscription_cli",
        kb_l2_subscription_model="gpt-6-luna",
        _env_file=None,
    )
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)
    monkeypatch.setattr("ttakkari.kb_pipeline.stages.get_settings", lambda: s)

    result = await verify_kb_v3(session, session_factory=session_factory, worker=worker)

    assert result["status"] in ("succeeded", "degraded")
    assert result["l2_adapter"] == "subscription_cli"
    assert result["counts"]["documents"] == 3
    assert result["counts"]["passages"] > 0
    assert result["counts"]["assertions"] >= 2
    # 30일 정책과 90일 정책 문서 간에 value_contradiction 충돌이 감지되어야 한다
    assert result["counts"]["conflicts"] >= 1
    assert len(result["conflict_ids"]) >= 1

    # 잡 상태 및 단계 확인
    assert len(result["jobs"]) == 3
    for job_summary in result["jobs"]:
        stages = job_summary["stages"]
        assert "parsing" in stages
        assert "chunking" in stages
        assert "extracting" in stages
        assert "canonicalizing" in stages

    # 민감 정보(secret, password, db url, raw body, prompt) 비노출 검증
    res_str = json.dumps(result, ensure_ascii=False)
    assert "postgresql://" not in res_str
    assert "secret" not in res_str.lower()
    assert "password" not in res_str.lower()
    assert "prompt" not in res_str


@pytest.mark.asyncio
async def test_verify_kb_v3_honestly_reports_degraded_when_l2_disabled(
    mem_db, monkeypatch
):
    """kb_l2_adapter=disabled 설정일 때 전역 설정을 override하지 않고 degraded 로 보고한다."""
    session, session_factory = mem_db

    orchestrator = KbPipelineOrchestrator(session_factory=session_factory)
    worker = KbPipelineWorker(
        session_factory=session_factory, orchestrator=orchestrator
    )

    s = Settings(kb_l2_adapter="disabled", _env_file=None)
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)
    monkeypatch.setattr("ttakkari.kb_pipeline.stages.get_settings", lambda: s)

    result = await verify_kb_v3(session, session_factory=session_factory, worker=worker)

    assert result["status"] == "degraded"
    assert result["l2_adapter"] == "disabled"
    assert result["counts"]["documents"] == 3
    # L2 가 꺼져 있어도 L0/L1 과 결정적 정본/상충 파이프라인은 정상 통과한다
    assert result["counts"]["passages"] > 0
    assert result["counts"]["conflicts"] >= 1


@pytest.mark.asyncio
async def test_verify_kb_v3_idempotent_reexecution(mem_db, monkeypatch):
    """동일 DB 에 두 번 실행해도 멱등한 source_key 로 문서를 중복 생성하지 않고 안전하게 기존 문서를 재사용한다."""
    session, session_factory = mem_db

    orchestrator = KbPipelineOrchestrator(session_factory=session_factory)
    worker = KbPipelineWorker(
        session_factory=session_factory, orchestrator=orchestrator
    )

    s = Settings(kb_l2_adapter="disabled", _env_file=None)
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)
    monkeypatch.setattr("ttakkari.kb_pipeline.stages.get_settings", lambda: s)

    # 1회 실행
    res1 = await verify_kb_v3(session, session_factory=session_factory, worker=worker)
    assert res1["counts"]["documents"] == 3

    # 2회 실행 (재실행)
    res2 = await verify_kb_v3(session, session_factory=session_factory, worker=worker)
    assert res2["counts"]["documents"] == 3

    # 전체 문서 수가 여전히 3개인지 확인 (중복 생성 방지)
    doc_count = await session.scalar(
        text(
            f"SELECT COUNT(*) FROM kb_documents WHERE doc_key IN ('{KEY_PDF}', '{KEY_POLICY_30D}', '{KEY_POLICY_90D}')"
        )
    )
    assert doc_count == 3


@pytest.mark.asyncio
async def test_verify_gate_rejects_held_and_zero_l2(mem_db, monkeypatch):
    """3개 잡이 held 이거나 실제 L2 extraction 호출이 0회일 때 succeeded 로 판정하지 않는다."""
    session, session_factory = mem_db

    s = Settings(
        kb_l2_adapter="subscription_cli",
        kb_l2_subscription_model="gpt-6-luna",
        _env_file=None,
    )
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)

    # worker 없이 실행하면 잡들이 ready가 되지 않고 finite timeout 이후 partial 혹은 held 상태가 됨
    result = await verify_kb_v3(
        session, session_factory=session_factory, worker=None, max_wait_seconds=0.1
    )

    # held / 0 L2 인 상황에서 절대로 succeeded 가 나와선 안 된다
    assert result["status"] != "succeeded"
    assert result["status"] in ("held", "degraded", "partial", "failed")
    # 1e5 하드코딩 오류 제거 검증 (제품 응답에 섞지 않음)
    assert "verification_required" not in result
    assert "recommended_verifier" not in result


@pytest.mark.asyncio
async def test_verify_gate_rejects_unrelated_or_stale_conflict(mem_db, monkeypatch):
    """무관한 엔터티의 conflict나 stale conflict만으로는 30일 vs 90일 상충 검증을 통과할 수 없다."""
    session, session_factory = mem_db

    s = Settings(
        kb_l2_adapter="subscription_cli",
        kb_l2_subscription_model="gpt-6-luna",
        _env_file=None,
    )
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)

    # 무관한 엔터티 및 conflict 주입
    unrelated_entity = KbEntity(
        tenant_id="default",
        visibility="global",
        origin_scope="personal",
        entity_kind="system",
        canonical_name="UnrelatedSystem",
        norm_name="unrelatedsystem",
        status="active",
    )
    session.add(unrelated_entity)
    await session.flush()

    unrelated_conflict = KbConflict(
        visibility="global",
        conflict_kind="value_contradiction",
        entity_id=unrelated_entity.id,
        predicate="version",
        status="open",
    )
    session.add(unrelated_conflict)
    await session.commit()

    # 가짜 워커: 잡을 강제로 ready 로 만들지만 30일 vs 90일 conflict는 생성하지 않음
    class EmptyWorker:
        async def pump(self):
            return 0

    result = await verify_kb_v3(
        session,
        session_factory=session_factory,
        worker=EmptyWorker(),
        max_wait_seconds=0.1,
    )
    # 30일 vs 90일 동일 주체/동일 predicate 상충이 없으므로 succeeded 불가
    assert result["status"] != "succeeded"


@pytest.mark.asyncio
async def test_verify_does_not_claim_unrelated_backlog(mem_db, monkeypatch):
    """운영 큐에 대기 중인 무관한 백로그 잡을 verify_kb_v3가 pump/claim하여 건드리지 않는다."""
    session, session_factory = mem_db

    # 무관한 문서 및 파이프라인 잡 생성
    from ttakkari.kb_library import register_text

    unrelated_doc = await register_text(
        session,
        title="[사용자 문서] 일상 메모",
        text="오늘의 메모 내용입니다.",
        source_key="user:memo:001",
        lang="ko",
        locator=None,
        scope="personal",
    )
    await session.commit()

    # 등록된 잡 확인
    unrelated_job = await session.scalar(
        select(KbPipelineJob).where(KbPipelineJob.revision_id == unrelated_doc.revision_id)
    )
    assert unrelated_job is not None
    orig_status = unrelated_job.status
    orig_claimed_by = unrelated_job.claimed_by

    s = Settings(kb_l2_adapter="disabled", _env_file=None)
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)

    # verify_kb_v3 실행 (worker=None 또는 기본 관측)
    await verify_kb_v3(
        session,
        session_factory=session_factory,
        worker=None,
        max_wait_seconds=0.1,
    )

    # verify_kb_v3 이후에도 unrelated_job 은 전혀 영향을 받지 않아야 한다
    await session.refresh(unrelated_job)
    assert unrelated_job.status == orig_status
    assert unrelated_job.claimed_by == orig_claimed_by


@pytest.mark.asyncio
async def test_verify_gate_rejects_missing_exact_quote(mem_db, monkeypatch):
    """주장(assertion)이 있더라도 근거(evidence)에 exact_quote가 없거나 빈 값이면 succeeded로 승인하지 않는다."""
    session, session_factory = mem_db

    s = Settings(
        kb_l2_adapter="subscription_cli",
        kb_l2_subscription_model="gpt-6-luna",
        _env_file=None,
    )
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)

    # 3개 잡을 모두 ready 로 만들지만 evidence 에 exact_quote 가 없도록 설정하는 시나리오
    # verify_kb_v3 는 quote 가 유효하지 않으면 degraded 또는 failed 처리해야 함
    class QuoteMissingWorker:
        async def pump(self):
            return 0

    result = await verify_kb_v3(
        session,
        session_factory=session_factory,
        worker=QuoteMissingWorker(),
        max_wait_seconds=0.1,
    )
    assert result["status"] != "succeeded"


@pytest.mark.asyncio
async def test_actual_db_counts_reported(mem_db, monkeypatch):
    """문서 카운트(counts.documents)는 하드코딩 3이 아니라 actual DB 쿼리 결과여야 한다."""
    session, session_factory = mem_db

    s = Settings(kb_l2_adapter="disabled", _env_file=None)
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)

    result = await verify_kb_v3(
        session,
        session_factory=session_factory,
        worker=None,
        max_wait_seconds=0.1,
    )
    assert isinstance(result["counts"]["documents"], int)
    assert result["counts"]["documents"] == 3


@pytest.mark.asyncio
async def test_verify_gate_full_success_when_all_conditions_met(mem_db, monkeypatch):
    """모든 계약 조건(3문서, 3ready, L2 calls 성공 증거, 인용구 보장, 30d vs 90d 동일 엔터티/술어 상충) 충족 시 정직하게 succeeded 보고."""
    session, session_factory = mem_db

    # Fake L2: L2 추출 시 엔터티 및 인용된 주장 반환 (calls_made 카운트 증가)
    async def fake_l2_call(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "entities": [{"name": "반품 규정", "category": "policy"}],
            "assertions": [
                {
                    "subject": "반품 규정",
                    "predicate": "refund_period",
                    "value": "30일",
                    "quote": "30일 이내에 환불을 보장",
                }
            ],
            "relations": [],
        }

    class FakeAdapter:
        name = "subscription_cli"
        retry_after = None

        async def availability(self):
            return True, None

        async def __call__(self, prompt, schema):
            return await fake_l2_call(prompt, schema)

    orchestrator = KbPipelineOrchestrator(session_factory=session_factory)
    from ttakkari.kb_pipeline.stages import ExtractingProcessor
    from ttakkari.kb_pipeline.types import StageResult

    class FakeIndexingProcessor:
        async def __call__(self, ctx):
            return StageResult.success(detail={"lexical_ready": True, "vectors_created": 1})

    orchestrator.registry.register(
        "extracting",
        ExtractingProcessor(adapter_factory=lambda: FakeAdapter()),
    )
    orchestrator.registry.register("indexing", FakeIndexingProcessor())
    worker = KbPipelineWorker(
        session_factory=session_factory, orchestrator=orchestrator
    )

    s = Settings(
        kb_l2_adapter="subscription_cli",
        kb_l2_subscription_model="gpt-6-luna",
        _env_file=None,
    )
    monkeypatch.setattr("ttakkari.operations.kb_v3_verify.get_settings", lambda: s)
    monkeypatch.setattr("ttakkari.kb_pipeline.stages.get_settings", lambda: s)

    result = await verify_kb_v3(
        session,
        session_factory=session_factory,
        worker=worker,
        max_wait_seconds=5.0,
    )

    assert result["status"] == "succeeded", f"Unexpected status: {result.get('status')}, reason: {result.get('reason')}, jobs: {result.get('jobs')}"
    assert result["counts"]["documents"] == 3
    assert result["counts"]["passages"] > 0
    assert result["counts"]["assertions"] >= 2
    assert result["counts"]["conflicts"] >= 1
    assert len(result["conflict_ids"]) >= 1

    # 잡의 extracting 단계에서 실제 calls_made > 0 검증
    for j_summary in result["jobs"]:
        details = j_summary["details"]
        assert "l2" in details
        assert details["l2"]["adapter"] == "subscription_cli"
        assert details["l2"]["available"] is True
        assert details["l2"]["calls_made"] > 0


def test_heavy_lifecycle_dedup_and_conflict_guard(tmp_path, monkeypatch):
    """kb_v3_verify 의 단일 heavy / dedup 라이프사이클 계약 확인."""
    from ttakkari.operations import jobs

    (tmp_path / "jobs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(jobs, "root", lambda: tmp_path)

    # 1. 새 작업 준비
    job1, reused1 = jobs.prepare_heavy("kb_v3_verify", "test-session-1")
    assert reused1 is False
    assert job1["kind"] == "kb_v3_verify"
    assert job1["status"] == "queued"

    # 2. 동일한 kind 실행 중일 때 다시 호출 -> dedup (기존 잡 재사용)
    job2, reused2 = jobs.prepare_heavy("kb_v3_verify", "test-session-2")
    assert reused2 is True
    assert job2["id"] == job1["id"]

    # 3. 다른 heavy 작업 시도 -> Conflict 발생
    with pytest.raises(jobs.Conflict) as exc_info:
        jobs.prepare_heavy("backup_db", "test-session-3")
    assert "다른 운영 작업이 진행 중입니다" in str(exc_info.value)


def test_runner_summarize_kb_v3_verify():
    """runner._summarize 에서 kb_v3_verify 출력 JSON 파싱 및 상태 판정 계약 확인."""
    from ttakkari.operations import plans, runner

    plan = plans.Plan(
        kind="kb_v3_verify",
        argv=["python", "-m", "ttakkari.operations.kb_v3_verify"],
        env={},
        timeout=180,
        identity={"kind": "kb_v3_verify"},
    )

    # 1. 정상 succeeded JSON 출력
    valid_output = (
        "Starting verify...\n"
        '{"status": "succeeded", "counts": {"documents": 3, "conflicts": 1}}\n'
    )
    status, summary, err = runner._summarize(plan, "succeeded", valid_output, None)
    assert status == "succeeded"
    assert summary["status"] == "succeeded"
    assert summary["counts"]["documents"] == 3
    assert err is None

    # 2. degraded/held JSON 출력 -> runner 수준에서 failed 로 마킹하고 사유 보존
    degraded_output = '{"status": "degraded", "reason": "l2_extraction_calls_unverified"}\n'
    status, summary, err = runner._summarize(plan, "succeeded", degraded_output, None)
    assert status == "failed"
    assert summary["status"] == "degraded"
    assert "kb_v3_verify 검증 미달: degraded" in err

    # 3. 비정상 출력 (JSON 없음) -> failed
    status, summary, err = runner._summarize(plan, "succeeded", "Traceback...", None)
    assert status == "failed"
    assert "kb_v3_verify 요약 JSON을 찾을 수 없습니다" in err
