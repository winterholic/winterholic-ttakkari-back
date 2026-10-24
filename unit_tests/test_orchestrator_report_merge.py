"""오케스트레이터 작업자 결과 보고 병합(merge) 및 원자성·멱등성 계약 검증 단위 테스트."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ttakkari.models import ChatSession, Run, RunOrigin, RunStatus, SessionKind
from ttakkari.runs.manager import _enqueue_report, manager


class FakeDb:
    def __init__(
        self,
        parent_session: ChatSession,
        existing_runs: list[Run] | None = None,
        leases: set[uuid.UUID] | None = None,
    ) -> None:
        self.parent_session = parent_session
        self.runs: list[Run] = list(existing_runs or [])
        self.leases: set[uuid.UUID] = set(leases or [])
        self.added: list[object] = []
        self.flushed = False
        self.scalar_statements: list[object] = []

    async def get(self, entity, ident):
        if entity == ChatSession and ident == self.parent_session.id:
            return self.parent_session
        from ttakkari.models import RuntimeLease
        if entity == RuntimeLease:
            return MagicMock() if ident in self.leases else None
        return None

    async def scalar(self, stmt):
        self.scalar_statements.append(stmt)
        # parent with_for_update query
        stmt_str = str(stmt)
        if "FROM sessions" in stmt_str:
            return self.parent_session

        if "FROM runtime_leases" in stmt_str:
            return None

        # pending report query: select(Run).where(Run.session_id == parent.id, Run.status == RunStatus.queued, Run.origin == RunOrigin.report)
        matching = [
            r
            for r in self.runs
            if r.session_id == self.parent_session.id
            and r.status == RunStatus.queued
            and r.origin == RunOrigin.report
        ]
        return matching[-1] if matching else None

    def add(self, obj) -> None:
        if isinstance(obj, Run) and getattr(obj, "last_seq", None) is None:
            obj.last_seq = 0
        self.added.append(obj)
        if isinstance(obj, Run):
            self.runs.append(obj)

    async def flush(self) -> None:
        self.flushed = True


def _make_worker(parent_id: uuid.UUID, title: str) -> tuple[ChatSession, Run]:
    ws_id = uuid.uuid4()
    worker = ChatSession(
        id=uuid.uuid4(),
        workspace_id=ws_id,
        kind=SessionKind.worker,
        parent_id=parent_id,
        title=title,
        engine="claude",
        model="claude-sonnet-4-5",
    )
    run = Run(
        id=uuid.uuid4(),
        session_id=worker.id,
        workspace_id=ws_id,
        status=RunStatus.succeeded,
        result_text="done",
        origin=RunOrigin.orchestrator,
        last_seq=0,
    )
    return worker, run


@pytest.mark.asyncio
async def test_queued_reports_are_merged_in_single_process_mode(monkeypatch):
    """단일 프로세스 모드(manager.runtime is None): busy 오케스트레이터의 대기 보고는 하나로 합쳐진다."""
    monkeypatch.setattr(manager, "_runtime", None)

    parent_id = uuid.uuid4()
    orch = ChatSession(
        id=parent_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )
    db = FakeDb(orch)

    # Worker A 종료 -> 첫 보고 생성
    worker_a, run_a = _make_worker(parent_id, "a")
    pid_a = await _enqueue_report(db, worker_a, run_a)
    assert pid_a == parent_id
    reports = [r for r in db.runs if r.origin == RunOrigin.report]
    assert len(reports) == 1
    assert reports[0].status == RunStatus.queued
    assert "'a'" in reports[0].prompt

    # Worker B 종료 -> 기존 대기 보고에 합쳐짐 (새 보고 Run 생성 안 됨)
    worker_b, run_b = _make_worker(parent_id, "b")
    pid_b = await _enqueue_report(db, worker_b, run_b)
    assert pid_b == parent_id
    reports = [r for r in db.runs if r.origin == RunOrigin.report]
    assert len(reports) == 1
    assert reports[0].status == RunStatus.queued
    assert "'a'" in reports[0].prompt and "'b'" in reports[0].prompt


@pytest.mark.asyncio
async def test_queued_reports_are_merged_in_generation_runtime_mode(monkeypatch):
    """세대 런타임 모드(manager.runtime is not None): un-owned 대기 보고는 동일하게 병합된다."""
    fake_runtime = MagicMock()
    monkeypatch.setattr(manager, "_runtime", fake_runtime)

    # is_owned가 False를 반환하도록 모의 (아직 어떤 세대도 lease를 쥐지 않은 queued 상태)
    async def mock_is_owned(run_id):
        return False

    monkeypatch.setattr(manager, "is_owned", mock_is_owned)

    parent_id = uuid.uuid4()
    orch = ChatSession(
        id=parent_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )
    db = FakeDb(orch)

    # Worker A 종료 -> 첫 보고 생성
    worker_a, run_a = _make_worker(parent_id, "a")
    await _enqueue_report(db, worker_a, run_a)
    assert len([r for r in db.runs if r.origin == RunOrigin.report]) == 1

    # Worker B 종료 -> 세대 런타임 하에서도 대기 보고에 정상 병합되어야 함
    worker_b, run_b = _make_worker(parent_id, "b")
    await _enqueue_report(db, worker_b, run_b)
    reports = [r for r in db.runs if r.origin == RunOrigin.report]
    assert len(reports) == 1, (
        "세대 런타임 하에서도 queued 보고가 2개로 분리되지 않고 병합되어야 함"
    )
    assert "'a'" in reports[0].prompt and "'b'" in reports[0].prompt


@pytest.mark.asyncio
async def test_running_or_owned_report_is_not_mutated_and_creates_new_report(
    monkeypatch,
):
    """이미 실행 중이거나 lease를 획득한 보고는 수정하지 않고, 새로운 대기 보고를 생성한다."""
    parent_id = uuid.uuid4()
    orch = ChatSession(
        id=parent_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )

    # 이미 존재하는 보고가 있지만, 이미 소유(실행) 중인 경우
    existing_rep = Run(
        id=uuid.uuid4(),
        session_id=parent_id,
        workspace_id=orch.workspace_id,
        status=RunStatus.queued,
        prompt="[작업 보고] 작업 세션 'a' 완료.",
        origin=RunOrigin.report,
        last_seq=0,
    )
    db = FakeDb(orch, existing_runs=[existing_rep])

    # is_owned가 True 반환 (이미 러너가 소유하여 시작 직전/실행 중)
    async def mock_is_owned(run_id):
        return run_id == existing_rep.id

    monkeypatch.setattr(manager, "is_owned", mock_is_owned)

    # Worker B 종료 -> 실행 중인 보고를 덮어쓰지 않고 새 보고 Run 생성
    worker_b, run_b = _make_worker(parent_id, "b")
    await _enqueue_report(db, worker_b, run_b)

    reports = [r for r in db.runs if r.origin == RunOrigin.report]
    assert len(reports) == 2, "소유 중인 보고서는 훼손하지 않고 분리 생성되어야 함"
    assert "'b'" not in existing_rep.prompt
    assert "'b'" in reports[1].prompt


@pytest.mark.asyncio
async def test_parent_session_is_locked_for_update_during_enqueue():
    """부모 세션을 with_for_update로 잠가 동시 종료 시의 레이스 컨디션을 방지한다."""
    parent_id = uuid.uuid4()
    orch = ChatSession(
        id=parent_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )
    db = FakeDb(orch)

    worker_a, run_a = _make_worker(parent_id, "a")
    await _enqueue_report(db, worker_a, run_a)

    # with_for_update가 포함된 select(ChatSession) 쿼리가 호출되었는지 확인
    session_queries = [s for s in db.scalar_statements if "FROM sessions" in str(s)]
    assert len(session_queries) >= 1
    assert "FOR UPDATE" in str(session_queries[0])


@pytest.mark.asyncio
async def test_claim_and_enqueue_report_share_serialization_lock():
    """GenerationRuntime.claim()과 _enqueue_report()가 동일한 ChatSession 및 Run에 대해
    FOR UPDATE 락을 사용하여 직렬화 지점을 공유하는지 검증한다."""
    parent_id = uuid.uuid4()
    orch = ChatSession(
        id=parent_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )
    db = FakeDb(orch)

    # 1. _enqueue_report 검증
    worker_a, run_a = _make_worker(parent_id, "a")
    await _enqueue_report(db, worker_a, run_a)

    session_locks = [s for s in db.scalar_statements if "FROM sessions" in str(s) and "FOR UPDATE" in str(s)]
    assert len(session_locks) >= 1, "_enqueue_report는 부모 세션을 FOR UPDATE로 잠가야 함"

    run_locks = [s for s in db.scalar_statements if "FROM runs" in str(s) and "FOR UPDATE" in str(s)]
    assert len(run_locks) >= 1, "_enqueue_report는 대기 보고 Run을 FOR UPDATE로 잠가야 함"


@pytest.mark.asyncio
async def test_race_claim_committed_during_enqueue_creates_new_report_instead_of_mutating(
    monkeypatch,
):
    """대기 보고 확인 직후 다른 세대가 lease를 획득(claim)하는 경쟁 상황에서,
    이미 claim된 보고를 훼손하지 않고 새로운 대기 보고를 분리 생성하여 보고 유실을 막는다."""
    fake_runtime = MagicMock()
    fake_runtime.role = "executor"
    fake_runtime.execution_mode = "local"
    fake_runtime.id = uuid.uuid4()
    monkeypatch.setattr(manager, "_runtime", fake_runtime)

    parent_id = uuid.uuid4()
    orch = ChatSession(
        id=parent_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )
    initial_report = Run(
        id=uuid.uuid4(),
        session_id=parent_id,
        workspace_id=orch.workspace_id,
        status=RunStatus.queued,
        prompt="[작업 보고] 기존 작업 완료.",
        origin=RunOrigin.report,
        last_seq=0,
    )
    db = FakeDb(orch, existing_runs=[initial_report])

    claim_intervened = False

    async def simulated_is_owned(run_id, db=None):
        nonlocal claim_intervened
        if run_id == initial_report.id:
            claim_intervened = True
            if db is not None and hasattr(db, "leases"):
                db.leases.add(initial_report.id)
            return True
        return False

    monkeypatch.setattr(manager, "is_owned", simulated_is_owned)

    worker, run = _make_worker(parent_id, "interleaved-worker")
    pid = await _enqueue_report(db, worker, run)
    assert pid == parent_id
    assert claim_intervened is True

    # 1. 이미 claim된 initial_report의 prompt는 변경되지 않고 보존됨
    assert "[작업 보고] 기존 작업 완료." == initial_report.prompt
    assert "interleaved-worker" not in initial_report.prompt

    # 2. 새로운 report Run이 분리 생성되어 새 보고 내용이 온전히 보존됨
    reports = [r for r in db.runs if r.origin == RunOrigin.report]
    assert len(reports) == 2, "claim된 보고는 훼손하지 않고 새로운 대기 보고가 생성되어야 함"
    assert "interleaved-worker" in reports[1].prompt


@pytest.mark.asyncio
async def test_claim_locks_session_and_run_for_update_before_leasing():
    """GenerationRuntime.claim()이 ChatSession과 Run에 대해 FOR UPDATE 락을 획득하는지 검증한다."""
    from ttakkari.generation import GenerationRuntime

    runtime = GenerationRuntime(Path("/tmp"), revision="a" * 40, api_url="http://127.0.0.1:18791")
    runtime.id = uuid.uuid4()
    runtime.role = "executor"
    runtime.execution_mode = "local"

    sess_id = uuid.uuid4()
    run_id = uuid.uuid4()
    sess = ChatSession(
        id=sess_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )
    run = Run(
        id=run_id,
        session_id=sess_id,
        workspace_id=sess.workspace_id,
        status=RunStatus.queued,
        prompt="report prompt",
        origin=RunOrigin.report,
        last_seq=0,
    )

    statements = []

    class FakeClaimSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, entity, ident):
            from ttakkari.models import RuntimeControl, RuntimeLease
            if entity == Run and ident == run_id:
                return run
            if entity == RuntimeControl:
                return RuntimeControl(id=1, epoch=1, active_generation=runtime.id)
            if entity == RuntimeLease:
                return None
            return None

        async def scalar(self, stmt):
            from ttakkari.models import RuntimeControl
            statements.append(stmt)
            s_str = str(stmt)
            if "ORDER BY runs.created_at" in s_str:
                return run_id
            if "count" in s_str.lower():
                return 0
            if "FROM runtime_control" in s_str:
                return RuntimeControl(id=1, epoch=1, active_generation=runtime.id)
            if "FROM sessions" in s_str:
                return sess
            if "FROM runs" in s_str:
                return run
            return None

        def add(self, obj):
            pass

        async def commit(self):
            pass

        async def execute(self, stmt):
            pass

    import ttakkari.generation as gen_mod
    orig_sm = gen_mod.sessionmaker
    gen_mod.sessionmaker = lambda: FakeClaimSession
    try:
        token = await runtime.claim(run_id, "leads", 2)
        assert token is not None

        # 세션 FOR UPDATE 확인
        sess_for_update = [s for s in statements if "FROM sessions" in str(s) and "FOR UPDATE" in str(s)]
        assert len(sess_for_update) >= 1, "claim은 ChatSession을 FOR UPDATE로 잠가야 함"

        # Run FOR UPDATE 확인
        run_for_update = [s for s in statements if "FROM runs" in str(s) and "FOR UPDATE" in str(s)]
        assert len(run_for_update) >= 1, "claim은 Run을 FOR UPDATE로 잠가야 함"
    finally:
        gen_mod.sessionmaker = orig_sm


@pytest.mark.asyncio
async def test_concurrent_claim_and_enqueue_interleaving_simulation():
    """비동기 단계 시뮬레이션으로 인터리빙을 모사하여 동시 진행 시에도 보고서 내용이 온전히 보존됨을 검증한다."""
    parent_id = uuid.uuid4()
    orch = ChatSession(
        id=parent_id,
        workspace_id=uuid.uuid4(),
        kind=SessionKind.orchestrator,
        title="Orchestrator",
        engine="claude",
        model="claude-sonnet-4-5",
    )
    db = FakeDb(orch)

    # Worker 1 완료 -> 첫 번째 대기 보고 생성
    w1, r1 = _make_worker(parent_id, "worker-1")
    await _enqueue_report(db, w1, r1)
    assert len([r for r in db.runs if r.origin == RunOrigin.report]) == 1
    rep1 = next(r for r in db.runs if r.origin == RunOrigin.report)

    # 상황 A: Worker 2 보고 병합 완료 후 claim이 진행되는 경우
    w2, r2 = _make_worker(parent_id, "worker-2")
    await _enqueue_report(db, w2, r2)
    assert len([r for r in db.runs if r.origin == RunOrigin.report]) == 1
    assert "worker-1" in rep1.prompt and "worker-2" in rep1.prompt

    # 이제 claim이 이 rep1을 소유함
    db.leases.add(rep1.id)

    # 상황 B: claim으로 소유된 이후 Worker 3이 보고를 등록하는 경우
    w3, r3 = _make_worker(parent_id, "worker-3")
    await _enqueue_report(db, w3, r3)

    reports = [r for r in db.runs if r.origin == RunOrigin.report]
    assert len(reports) == 2, "이미 소유된 rep1은 보존되고 새 rep2가 생성되어야 함"
    assert "worker-3" in reports[1].prompt
    assert "worker-3" not in rep1.prompt
