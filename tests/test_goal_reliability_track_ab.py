"""2차 안정화 Track A(완료 증거 신뢰성) 및 Track B(실질적 진전 판정·재시도 제어) 회귀 테스트."""
from __future__ import annotations

import asyncio
from uuid import UUID

import pytest
from goal_helpers import (
    contract,
    lead,
    put,
    raw,
)
from sqlalchemy import select

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import (
    AuditLog,
    ChatSession,
    Run,
    RunEvent,
    RunOrigin,
    RunStatus,
    SessionKind,
)
from ttakkari.orchestrator import observed, work
from ttakkari.orchestrator.goal_signals import (
    extract_items,
    generic_claim,
    unreported_items,
    weak_evidence,
)
from ttakkari.runs import completion
from ttakkari.runs.manager import manager


async def _git_init(root) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", "init", cwd=str(root), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    await proc.wait()


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


@pytest.fixture
def contract_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_completion_contract", True)


# =====================================================================================================
# Track A: 완료 증거 신뢰성 (P0)
# =====================================================================================================

def test_track_a_evidence_classification_and_kinds():
    """A-2: KINDS 구분 및 ref 명세(source_run_id, seq, call_id, target, result, at)."""
    assert "observed_tool" in observed.KINDS
    assert "observed_file" in observed.KINDS
    assert "observed_git" in observed.KINDS
    assert "observed_test" in observed.KINDS
    assert "observed_external" in observed.KINDS
    assert "review" in observed.KINDS
    assert "unverified" in observed.KINDS
    assert "self_claim" in observed.KINDS

    ref = observed._ref("observed_test", target="tests/test_foo.py", source_run_id="run-123",
                        seq=42, call_id="call-99", result="5 passed", at="2026-11-28T00:00:00Z")
    assert ref["kind"] == "observed_test"
    assert ref["target"] == "tests/test_foo.py"
    assert ref["source_run_id"] == "run-123"
    assert ref["seq"] == 42
    assert ref["call_id"] == "call-99"
    assert ref["result"] == "5 passed"
    assert ref["at"] == "2026-11-28T00:00:00Z"


async def test_track_a_rejects_nonexistent_commit(client, auth, make_dir):
    """A-4: 저장소에 존재하지 않는 커밋 SHA 증거는 complete 통과 불가 (422 거부)."""
    root = make_dir()
    await _git_init(root)
    _sess, h = await lead(client, auth, lambda: root)
    c = contract("버그 수정", reqs=[{"id": "R1", "text": "코드 수정", "status": "done",
                                    "evidence": "commit 1234567890ab: fixed bug"}])
    res = await put(client, h, {**c, "status": "complete", "next_action": ""})
    assert res.status_code == 422
    assert "completion evidence contradicts observed run events" in res.text
    assert "commit_not_found" in res.text or "1234567890ab" in res.text


async def test_track_a_rejects_unrun_test_success_claim(client, auth, make_dir):
    """A-4: 관측된 테스트 실행이 없는데 테스트 통과 주장 시 complete 거부 (422)."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    # 세션에 최소 1회 이상의 도구 관측이 있으나 테스트가 없는 상태 생성
    async with sessionmaker()() as db:
        r1 = Run(session_id=sid, workspace_id=sess["workspace_id"], engine="claude",
                 origin=RunOrigin.user, prompt="작업해줘", status=RunStatus.succeeded)
        db.add(r1)
        await db.flush()
        ev1 = RunEvent(run_id=r1.id, seq=1, type="tool.call",
                       payload={"name": "run_command", "input": {"command": "echo hello"}, "call_id": "c1"})
        ev2 = RunEvent(run_id=r1.id, seq=2, type="tool.result",
                       payload={"call_id": "c1", "output": "hello", "exit_code": 0})
        db.add_all([ev1, ev2])
        await db.commit()

    c = contract("테스트 수행", reqs=[{"id": "R1", "text": "단위 테스트 통과", "status": "done",
                                      "evidence": "pytest: 50 passed in 1.2s"}])
    res = await put(client, h, {**c, "status": "complete", "next_action": ""})
    assert res.status_code == 422
    assert "test_not_run" in res.text or "관측된 테스트 실행이 없다" in res.text


async def test_track_a_rejects_failed_test_success_claim(client, auth, make_dir):
    """A-4: 실패한 테스트를 통과했다고 주장 시 모순으로 감지."""
    pool = {
        "commits": [], "shas": [], "files": {}, "writes": {}, "deploys": [],
        "tests": [{"target": "tests/test_foo.py", "outcome": "failed", "run_id": "r1", "seq": 10,
                   "line": "1 failed, 2 passed"}],
    }
    checked = await observed.check_claims("tests/test_foo.py 테스트 3 passed 로 통과 확인", pool, [])
    assert checked["contradictions"]
    assert any(c["code"] == "test_failed" for c in checked["contradictions"])
    assert observed.evidence_level(checked) == "contradicted"


def test_track_a_generic_long_prose_bypass_detected():
    """A-1: 긴 완료 문장 우회 검출 (generic_claim, weak_evidence)."""
    # 위치·명령·결과 수치가 없는 장문 완료 문장
    prose = "모든 요청 사항을 정상적으로 완료하였으며 전체 작업을 성공적으로 수행했습니다. 문제가 전혀 없음을 확인했습니다."
    assert generic_claim(prose) is True
    assert weak_evidence(prose) is True

    # 실제 위치나 명령어가 있는 문장은 일반 주장이 아님
    concrete = "src/ttakkari/orchestrator/work.py 수정 및 pytest tests/test_foo.py 5 passed 확인"
    assert generic_claim(concrete) is False
    assert weak_evidence(concrete) is False


def test_track_a_unreported_items_detection():
    """A-5: 작업자 지시의 번호 항목 중 일부만 다룬 보고 검출."""
    items = [
        {"label": "1", "text": "DB 마이그레이션 스키마 작성"},
        {"label": "2", "text": "신규 API 엔드포인트 구현"},
        {"label": "3", "text": "단위 테스트 통과 확인"},
        {"label": "4", "text": "운영 배포 예약"},
    ]
    # 1번 항목만 보고하고 완료 선언한 경우
    partial_report = "1) DB 마이그레이션 스키마 작성 완료했습니다. alembic upgrade head 실행함."
    unrep = unreported_items(items, partial_report)
    assert len(unrep) == 3
    assert {u["label"] for u in unrep} == {"2", "3", "4"}

    # 모든 항목을 다룬 경우
    full_report = ("1) DB 마이그레이션 스키마 작성 완료\n"
                   "2) 신규 API 엔드포인트 구현 완료 (/api/v2/items)\n"
                   "3) 단위 테스트 통과 (pytest 12 passed)\n"
                   "4) 운영 배포 예약 완료")
    assert unreported_items(items, full_report) == []


async def test_track_a_rejects_complete_when_worker_running(client, auth, make_dir):
    """A-4: 필수 작업자 실행 중인데 전체 완료 주장은 거부 (422)."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    async with sessionmaker()() as db:
        worker_sess = ChatSession(workspace_id=sess["workspace_id"], engine="claude", kind=SessionKind.worker,
                                  parent_id=sid, title="서브작업자")
        db.add(worker_sess)
        await db.flush()
        running_run = Run(session_id=worker_sess.id, workspace_id=sess["workspace_id"], engine="claude",
                          origin=RunOrigin.orchestrator, prompt="작업 중", status=RunStatus.running)
        db.add(running_run)
        await db.commit()

    c = contract("작업", reqs=[{"id": "R1", "text": "기본 작업", "status": "done", "evidence": "pytest: 5 passed"}])
    res = await put(client, h, {**c, "status": "complete", "next_action": ""})
    assert res.status_code == 422
    assert "unfinished worker execution" in res.text


async def test_track_a_reuses_prior_run_and_worker_evidence(client, auth, make_dir):
    """A-4: 이전 Run 또는 다른 작업자에서 관측된 유효한 증거는 재사용 가능."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    # 이전 Run 에 test 실행 이벤트 기록
    async with sessionmaker()() as db:
        r1 = Run(session_id=sid, workspace_id=sess["workspace_id"], engine="claude",
                 origin=RunOrigin.user, prompt="테스트 돌려줘", status=RunStatus.succeeded)
        db.add(r1)
        await db.flush()
        ev1 = RunEvent(run_id=r1.id, seq=1, type="tool.call",
                       payload={"name": "run_command", "input": {"command": "pytest tests/test_unit.py"}, "call_id": "c1"})
        ev2 = RunEvent(run_id=r1.id, seq=2, type="tool.result",
                       payload={"call_id": "c1", "output": "==== 10 passed in 0.5s ====", "exit_code": 0})
        db.add_all([ev1, ev2])
        await db.commit()

    c = contract("검증 작업", reqs=[{"id": "R1", "text": "단위 테스트", "status": "done",
                                    "evidence": "pytest tests/test_unit.py: 10 passed"}])
    # 이전 Run 의 관측을 찾아내어 422 모순 없이 완료 통과
    res = await put(client, h, {**c, "status": "complete", "next_action": ""})
    assert res.status_code == 200, res.text
    saved = await raw(sid)
    audit = saved["evidence_audit"]["R1"]
    assert audit["level"] == "observed"
    assert audit["refs"][0]["kind"] == "observed_test"


async def test_track_a_analysis_task_without_code_changes_passes(client, auth, make_dir):
    """A-3: 코드 변경 없는 분석·조사 작업은 파일 수정 없이도 완료 통과."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    # 조사 증거: 조회 자료와 원인 분석 명시
    evidence_text = ("docs/review/goal-reliability-operational-cases.md 조사 결과 확인: "
                     "원인은 백그라운드 테스트 대기 중 cancelled 발생이었음")
    c = contract("원인 분석", reqs=[{"id": "R1", "text": "사례 조사", "status": "done",
                                    "evidence": evidence_text}])
    res = await put(client, h, {**c, "status": "complete", "next_action": ""})
    assert res.status_code == 200, res.text
    saved = await raw(sid)
    audit = saved["evidence_audit"]["R1"]
    # 모순(contradicted)이 아니며, unverified/self_claim 으로 정상 보존
    assert audit["level"] != "contradicted"


async def test_track_a_unverifiable_external_claims_kept_as_unverified():
    """A-2/A-4: 환경상 검증 불가한 주장은 거부(모순)하지 않고 unverified 로 보존."""
    pool = {"commits": [], "shas": [], "files": {}, "writes": {}, "deploys": [], "tests": []}
    checked = await observed.check_claims("운영 환경 배포 완료 확인 (외부 프로덕션 모니터링)", pool, [])
    assert not checked["contradictions"]
    assert checked["unverified"]
    assert observed.evidence_level(checked) == "unverified"


# =====================================================================================================
# Track B: 실질적 진전 판정 및 재시도 제어 (P0)
# =====================================================================================================

def test_track_b_call_id_linking_and_parallel_interleaving():
    """B-2: 도구 호출과 결과를 call_id 로 연결하고 병렬 교차 호출에서도 오연결 없음."""
    obs = observed.Observer()
    # Call A: 테스트 명령, Call B: git status
    obs.feed(1, "tool.call", {"name": "run_command", "call_id": "call-A", "input": {"command": "pytest tests/test_a.py"}})
    obs.feed(2, "tool.call", {"name": "run_command", "call_id": "call-B", "input": {"command": "git status"}})

    # Result B 먼저 도착, 그 다음 Result A 도착
    obs.feed(3, "tool.result", {"call_id": "call-B", "output": "On branch main\nnothing to commit", "exit_code": 0})
    obs.feed(4, "tool.result", {"call_id": "call-A", "output": "==== 5 passed in 0.2s ====", "exit_code": 0})

    data = obs.finish("run-1")
    assert len(data["tests"]) == 1
    assert data["tests"][0]["target"] == "pytest:tests/test_a.py"
    assert data["tests"][0]["outcome"] == "passed"
    assert data["tests"][0]["call_id"] == "call-A"
    assert data["unlinked_results"] == 0
    assert data["unresolved_calls"] == 0


def test_track_b_failed_calls_not_counted_as_progress():
    """B-2: 실패한 도구 호출은 진전으로 세지 않고 failed 카운터에 기록."""
    obs = observed.Observer()
    obs.feed(1, "tool.call", {"name": "run_command", "call_id": "c1", "input": {"command": "git commit -m 'wip'"}})
    obs.feed(2, "tool.result", {"call_id": "c1", "output": "nothing to commit, working tree clean", "exit_code": 1, "is_error": True})

    obs.feed(3, "tool.call", {"name": "write_file", "call_id": "c2", "input": {"path": "src/foo.py"}})
    obs.feed(4, "tool.result", {"call_id": "c2", "output": "Permission denied", "exit_code": 1, "is_error": True})

    data = obs.finish("run-1")
    assert data["failed"]["commit"] == 1
    assert data["failed"]["write"] == 1
    assert not data["commits"]
    assert not data["writes"]

    # 진전 판정
    prog = observed.assess_progress(data, None)
    assert prog["progressed"] is False
    assert prog["repeated"]["failed_commit"] == 1
    assert prog["repeated"]["failed_write"] == 1


def test_track_b_edit_revert_oscillation_rejected():
    """B-3: 같은 파일의 수정·원복 반복(A->B->A)은 진전으로 인정되지 않음."""
    # Run 1: 패치 적용
    obs1 = {"facts": [{"k": "d:ph1:patch1_hash", "inv": "d:ph1:patch1_inv", "cat": "diff"}], "tests": []}
    p1 = observed.assess_progress(obs1, None)
    assert p1["progressed"] is True
    ledger = p1["ledger"]
    assert "d:ph1:patch1_inv" in ledger["blocked"]

    # Run 2: 원복 패치 적용 (blocked 에 등록된 inv 지문과 일치)
    obs2 = {"facts": [{"k": "d:ph1:patch1_inv", "inv": "d:ph1:patch1_hash", "cat": "diff"}], "tests": []}
    p2 = observed.assess_progress(obs2, ledger)
    assert p2["progressed"] is False
    assert p2["repeated"]["reverted"] == 1


def test_track_b_command_phrasing_only_rejected():
    """B-3: cd x && 등 명령 표현만 바꾼 동일 명령은 진전으로 인정되지 않음."""
    obs1 = observed.Observer()
    obs1.feed(1, "tool.call", {"name": "run_command", "call_id": "c1", "input": {"command": "cd /workspace && git status"}})
    obs1.feed(2, "tool.result", {"call_id": "c1", "output": "clean", "exit_code": 0})
    d1 = obs1.finish("r1")

    obs2 = observed.Observer()
    obs2.feed(1, "tool.call", {"name": "run_command", "call_id": "c2", "input": {"command": "git status"}})
    obs2.feed(2, "tool.result", {"call_id": "c2", "output": "clean", "exit_code": 0})
    d2 = obs2.finish("r2")

    p1 = observed.assess_progress(d1, None)
    assert p1["progressed"] is True  # 첫 inspect 사실
    p2 = observed.assess_progress(d2, p1["ledger"])
    assert p2["progressed"] is False  # 정규화되어 동일 사실로 판단
    assert p2["repeated"]["inspect"] == 1


def test_track_b_pending_review_pending_does_not_reset_budget():
    """B-3: pending->review->pending->review 상태 변경은 예산을 초기화하지 않음."""
    ledger = observed.new_ledger()
    val1 = {"requirements": [{"id": "R1", "status": "review"}]}
    prog1, ledger = work._checkpoint_progress(None, val1, ledger)
    assert prog1 is True
    assert ledger["req_rank"]["R1"] == 1  # review

    # review -> pending 으로 후퇴
    val2 = {"requirements": [{"id": "R1", "status": "pending"}]}
    prog2, ledger = work._checkpoint_progress(val1, val2, ledger)
    assert prog2 is False
    assert ledger["req_rank"]["R1"] == 1  # 최고 도달 rank 유지

    # pending -> review 재시도: 기존 도달 레벨(1)을 넘지 못하므로 진전 아님
    val3 = {"requirements": [{"id": "R1", "status": "review"}]}
    prog3, ledger = work._checkpoint_progress(val2, val3, ledger)
    assert prog3 is False

    # review -> done: 최고 도달 레벨 초과이므로 진전 인정
    val4 = {"requirements": [{"id": "R1", "status": "done"}]}
    prog4, ledger = work._checkpoint_progress(val3, val4, ledger)
    assert prog4 is True
    assert ledger["req_rank"]["R1"] == 2


def test_track_b_test_recovery_recognized_as_progress():
    """B-3: 이전 실패 검증의 성공 전환은 진전으로 인정."""
    # 1회차: 실패
    obs1 = {"facts": [], "tests": [{"target": "pytest:test_a.py", "outcome": "failed", "sig": "err1"}]}
    p1 = observed.assess_progress(obs1, None)
    assert p1["progressed"] is True  # 새 실패 지문 (weak progress 1)

    # 2회차: 동일 실패 반복 -> 진전 아님
    obs2 = {"facts": [], "tests": [{"target": "pytest:test_a.py", "outcome": "failed", "sig": "err1"}]}
    p2 = observed.assess_progress(obs2, p1["ledger"])
    assert p2["progressed"] is False
    assert p2["repeated"]["test_fail"] == 1

    # 3회차: 동일 테스트 통과 성공 -> 진전 인정!
    obs3 = {"facts": [], "tests": [{"target": "pytest:test_a.py", "outcome": "passed"}]}
    p3 = observed.assess_progress(obs3, p2["ledger"])
    assert p3["progressed"] is True
    assert "test_pass" in p3["new_categories"]


def test_track_b_investigation_capped_by_weak_streak():
    """B-3: 조사(새 대상 조회)는 MAX_WEAK_STREAK(2)회까지만 진전 인정 후 차단."""
    ledger = observed.new_ledger()
    # 1회차 조사
    p1 = observed.assess_progress({"facts": [{"k": "i:1", "cat": "inspect"}]}, ledger)
    assert p1["progressed"] is True
    assert p1["ledger"]["weak_streak"] == 1

    # 2회차 조사
    p2 = observed.assess_progress({"facts": [{"k": "i:2", "cat": "inspect"}]}, p1["ledger"])
    assert p2["progressed"] is True
    assert p2["ledger"]["weak_streak"] == 2

    # 3회차 조사: 연속 2회 초과로 progressed=False
    p3 = observed.assess_progress({"facts": [{"k": "i:3", "cat": "inspect"}]}, p2["ledger"])
    assert p3["progressed"] is False
    assert p3["repeated"]["weak_only_streak"] == 1


# =====================================================================================================
# Track B-5: 긴 Run 후반 이벤트 스캔 (Keyset Pagination)
# =====================================================================================================

async def test_track_b5_long_run_scans_tail_events_with_keyset_pagination(client, auth, make_dir):
    """B-5: 이벤트가 많은 긴 Run 에서도 keyset pagination 으로 후반 run.diff 가 누락되지 않음."""
    sess, _h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    async with sessionmaker()() as db:
        run = Run(session_id=sid, workspace_id=sess["workspace_id"], engine="claude",
                  origin=RunOrigin.user, prompt="긴 작업", status=RunStatus.succeeded)
        db.add(run)
        await db.flush()

        # 1200개의 tool.call/tool.result 이벤트 생성 (SCAN_PAGE=500 보다 큼)
        events = []
        for i in range(1, 1201):
            events.append(RunEvent(run_id=run.id, seq=i, type="tool.call" if i % 2 == 1 else "tool.result",
                                   payload={"call_id": f"c_{i // 2}", "name": "noop"}))
        # 마지막 seq 1201 에 run.diff 추가
        events.append(RunEvent(run_id=run.id, seq=1201, type="run.diff",
                               payload={"files": [{"path": "src/late_file.py", "added": 10, "removed": 0}]}))
        db.add_all(events)
        await db.commit()

        # observe_run 실행
        obs = await observed.observe_run(db, run)
        # 마지막 run.diff 가 정상 포함되었는지 확인
        assert obs["files"]
        assert any(f["path"] == "src/late_file.py" for f in obs["files"])
        assert any(f["k"].startswith("d:") for f in obs["facts"])


# =====================================================================================================
# 운영 사례 B / D Fixture 재현 검증
# =====================================================================================================

async def test_operational_case_b_fixture_reproduction(client, auth, make_dir, contract_on):
    """사례 B: 완료 선언했지만 일부 미충족인 경우 completion.evidence_mismatch 및 closure run 발생."""
    from test_worker_completion import _spawn
    prompt = ("다음을 구현하고 테스트하세요.\n1. DB 스키마 수정\n2. API 엔드포인트 구현\n"
              "3. 단위 테스트 통과\n4. 백엔드 배포 완료")
    _orch, wid = await _spawn(client, auth, make_dir, prompt)

    # 작업자가 1번만 수행하고 '완료' 선언한 결과 시뮬레이션
    async with sessionmaker()() as db:
        run = (await db.scalars(select(Run).where(Run.session_id == UUID(wid)).order_by(Run.created_at.desc()))).first()
        run.result_text = ("상태: 완료\n- 1: DB 스키마 수정 완료\n"
                           "나머지 API와 배포는 미실행 상태입니다.")
        run.status = RunStatus.succeeded
        info = completion.annotate(await db.get(ChatSession, UUID(wid)), run)
        # cross_check 수행
        info = await completion.cross_check(db, await db.get(ChatSession, UUID(wid)), run, info)
        await db.commit()

    # 결과 검증: evidence_mismatch 에 partial_items 가 기록되고 violation=True
    assert info["violation"] is True
    assert "partial_items" in info["evidence_mismatch"]
    assert info["evidence"]["unreported_items"]

    # closure_run 이 적격으로 판정됨
    async with sessionmaker()() as db:
        closure = await completion.closure_run(db, await db.get(ChatSession, UUID(wid)), run)
        assert closure is not None
        assert "[완료 확인]" in closure.prompt


async def test_operational_case_d_fixture_reproduction():
    """사례 D: 복합 목표를 단발 지시로 수행하다 요구가 누락된 경우 unreported_items 가 정확히 적출."""
    card = ("[접수에서 넘긴 사용자 요청]\n"
            "1. 인증 미들웨어 리팩토링\n"
            "2. 세션 만료 시간 설정 추가\n"
            "3. 보안 헤더 유닛 테스트 작성\n"
            "4. 변경 사항 staging 배포 및 검증")
    items = extract_items(card)
    assert len(items) == 4

    # 단발 지시 처리 후 반환된 보고 본문
    report_body = ("인증 미들웨어 코드를 정리했습니다.\n"
                   "- 1) 인증 미들웨어 리팩토링 완료: auth.py 모듈 분리")
    missing = unreported_items(items, report_body)
    assert len(missing) == 3
    assert [m["label"] for m in missing] == ["2", "3", "4"]


# =====================================================================================================
# §12 관측 이벤트 및 필드 검증
# =====================================================================================================

async def test_observability_events_emitted(client, auth, make_dir):
    """§12 관측: evidence_rejected, no_progress, continued 이벤트 및 필드 확인."""
    root = make_dir()
    await _git_init(root)
    _sess, h = await lead(client, auth, lambda: root)
    sid = UUID(_sess["id"])

    # 1. 먼저 active 계약을 등록한다.
    base = contract("테스트", reqs=[{"id": "R1", "text": "테스트", "status": "pending"}])
    assert (await put(client, h, base)).status_code == 200

    # 거짓 커밋 증거로 complete 시도 -> 422 거부 및 work.evidence_rejected 감사 로그 확인
    bad = {**base, "status": "complete", "next_action": "",
           "requirements": [{"id": "R1", "text": "테스트", "status": "done", "evidence": "commit 1234567890ab: not in repo"}]}
    res = await put(client, h, bad)
    assert res.status_code == 422
    async with sessionmaker()() as db:
        logs = list(await db.scalars(
            select(AuditLog).where(AuditLog.target_id == str(sid), AuditLog.action == "work.evidence_rejected")))
        assert len(logs) >= 1
        assert logs[0].outcome == "error"
        assert logs[0].detail["count"] >= 1

    # 2. after_run 에서 work.no_progress 및 work.continued 이벤트 필드 확인
    async with sessionmaker()() as db:
        # 첫 번째 run: 초기 체크포인트 반영으로 후속 run1_cont 생성
        run1 = Run(session_id=sid, workspace_id=_sess["workspace_id"], engine="claude",
                   origin=RunOrigin.user, prompt="1차 턴", status=RunStatus.succeeded)
        db.add(run1)
        await db.commit()
        cont1_id = await work.after_run(db, await db.get(ChatSession, sid), run1)
        await db.commit()
        assert cont1_id is not None

        # 생성된 후속 run1_cont 가 실행을 마치고 성공했으나 무진전인 경우 시뮬레이션
        cont1 = await db.get(Run, cont1_id)
        cont1.status = RunStatus.succeeded
        await db.commit()

        # 후속 run 의 after_run 호출 -> 무진전이므로 work.no_progress 방출 및 다음 후속 생성
        cont2_id = await work.after_run(db, await db.get(ChatSession, sid), cont1)
        await db.commit()
        assert cont2_id is not None

        # 생성된 이벤트 조회
        ev_rows = list(await db.scalars(
            select(RunEvent).where(RunEvent.run_id == cont1.id, RunEvent.type.in_(("work.no_progress", "work.continued")))))
        types = {e.type: e.payload for e in ev_rows}
        assert "work.no_progress" in types
        assert "repeated" in types["work.no_progress"]
        assert "unchanged_continuations" in types["work.no_progress"]

        assert "work.continued" in types
        assert types["work.continued"]["progressed"] is False
        assert "unchanged_continuations" in types["work.continued"]
        assert "auto_continuations" in types["work.continued"]
