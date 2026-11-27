"""12 backend goal reliability scenario tests (A7).

1. 10개 중 3개 완료 후 종료: 미완료 7개 자동 이어가기 및 목표 active 유지
2. 미완료 완료 선언 방지: 미완료 요구 및 약한 증거 complete 거절 (422)
3. 계약 누락 복합 요청: manage_work 미호출 시에도 원본 기반 자동 계약 생성
4. 체크포인트 누락 성과 복원: Run 실행 증거(파일·테스트) 기반 진전 인정 및 성과 복원
5. 독립 작업 계속: 한 작업자 실패가 다른 작업자 및 전체 목표를 중단시키지 않음
6. 일시 오류 복구: 계정 제한/일시 엔진 오류 시 즉시 blocked되지 않고 재시도
7. 무한 재시도 방지: 동일 오류 지문 반복 시 상한 도달 후 blocked 전환
8. 사용자 중단: 사용자 취소 시 goal paused 전환 및 후속 Run·예약 정리
9. 단순 질문 비실행: 짧은 상태 질문에 불필요한 계약 및 후속 Run 생성 방지
10. 범위 변경 연결: 원본 요청 보존 및 재계획 사유와 함께 요구사항 적응
11. 동시 종료 중복·누락 방지: 동시 종료 시 락을 통해 후속 중복 생성 방지
12. 배포·세대 전환 유실 방지: 세대 전환 및 재시작 시에도 계약 후속 Run 보존
"""
from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import (
    ChatSession,
    Run,
    RunOrigin,
    RunStatus,
    ScheduledAction,
    SessionKind,
    utcnow,
)
from ttakkari.orchestrator import work
from ttakkari.orchestrator.runtime import issue_token


async def lead(client, auth, make_dir, kind="orchestrator"):
    root = make_dir()
    ws = (await client.post("/api/workspaces", headers=auth,
                           json={"name": root.name, "root_path": str(root)})).json()
    sess = (await client.post("/api/sessions", headers=auth,
                             json={"workspace_id": ws["id"], "kind": kind})).json()
    return sess, {"Authorization": f"Bearer {issue_token(UUID(sess['id']))}"}


# -------------------------------------------------------------------------------------------------
# Scenario 1: 10개 중 3개 완료 후 종료
# -------------------------------------------------------------------------------------------------
async def test_scenario_1_three_done_out_of_ten_continues_remaining_seven(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    reqs = []
    for i in range(1, 11):
        if i <= 3:
            reqs.append({"id": f"REQ-{i}", "text": f"Task {i} specification", "status": "done",
                         "evidence": f"pytest test_task_{i}.py: exit 0, 1 passed"})
        else:
            reqs.append({"id": f"REQ-{i}", "text": f"Task {i} specification", "status": "pending"})

    initial_state = {
        "goal": "Execute full 10-step specification suite",
        "strategy": "Sequential execution with deterministic test verification",
        "requirements": reqs,
        "next_action": "Execute Task 4",
        "status": "active",
    }
    r = await client.post("/api/orchestrator/work", headers=h,
                          json={"operation": "checkpoint", "revision": 0, "state": initial_state})
    assert r.status_code == 200, r.text

    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        run = Run(session_id=sid, workspace_id=s.workspace_id, engine="codex",
                  prompt="Completed 1..3, stopping turn", status=RunStatus.succeeded)
        db.add(run)
        await db.flush()
        continued_id = await work.after_run(db, s, run)
        assert continued_id is not None
        await db.commit()

    async with sessionmaker()() as db:
        cont_run = await db.get(Run, continued_id)
        assert cont_run.status == RunStatus.queued
        assert "미완료" in cont_run.prompt
        assert "REQ-4" in cont_run.prompt or "Task 4 specification" in cont_run.prompt
        # Check that done items are marked as do not repeat
        assert "이미 완료된 항목" in cont_run.prompt

    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert loaded["state"]["status"] == "active"
    assert len(loaded["completed_scope"]) == 3
    assert len(loaded["pending_scope"]) == 7


# -------------------------------------------------------------------------------------------------
# Scenario 2: 미완료 완료 선언 방지 (약한 증거 및 미완료 요구 거절)
# -------------------------------------------------------------------------------------------------
async def test_scenario_2_premature_done_declaration_rejected(client, auth, make_dir, monkeypatch):
    from ttakkari.config import get_settings
    monkeypatch.setattr(get_settings(), "worker_completion_contract", True)
    sess, h = await lead(client, auth, make_dir)

    # 1) 미완료 요구가 남은 상태에서 complete 선언 거절
    state_pending = {
        "goal": "Verify all invariants",
        "strategy": "Test each module",
        "requirements": [
            {"id": "A", "text": "Part A", "status": "done", "evidence": "pytest: 5 passed"},
            {"id": "B", "text": "Part B", "status": "pending"},
        ],
        "next_action": "Do part B",
        "status": "complete",
    }
    r = await client.post("/api/orchestrator/work", headers=h,
                          json={"operation": "checkpoint", "revision": 0, "state": state_pending})
    assert r.status_code == 422
    assert "unfinished requirements" in r.text

    # 2) 증거가 약한("완료", "done", "pass") 경우 Pydantic 레벨에서 거절
    state_weak = {
        "goal": "Verify all invariants",
        "strategy": "Test each module",
        "requirements": [
            {"id": "A", "text": "Part A", "status": "done", "evidence": "완료"},
        ],
        "next_action": "None",
        "status": "complete",
    }
    r2 = await client.post("/api/orchestrator/work", headers=h,
                           json={"operation": "checkpoint", "revision": 0, "state": state_weak})
    assert r2.status_code == 422
    assert "weak" in r2.text

    # 3) 작업자 세션에서 상태: 완료이나 약한 증거일 때 completion annotate 위반 판정
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, UUID(sess["id"]))
        worker = ChatSession(workspace_id=s.workspace_id, parent_id=s.id, title="worker", engine="claude", kind=SessionKind.worker)
        db.add(worker)
        await db.flush()
        w_run = Run(session_id=worker.id, workspace_id=s.workspace_id, engine="claude", origin=RunOrigin.orchestrator,
                    prompt="task", status=RunStatus.succeeded, result_text="상태: 완료\n완료")
        db.add(w_run)
        await db.flush()
        from ttakkari.runs import completion
        info = completion.annotate(worker, w_run)
        assert info is not None
        assert info.get("weak_evidence") is True
        assert info.get("violation") is True


# -------------------------------------------------------------------------------------------------
# Scenario 3: 계약 누락 복합 요청 자동 계약 생성
# -------------------------------------------------------------------------------------------------
async def test_scenario_3_complex_request_auto_creates_goal_contract(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    complex_prompt = (
        "[접수에서 넘긴 사용자 요청]\n"
        "다음 4단계 백엔드 작업을 진행해 주세요.\n"
        "1. 데이터베이스 마이그레이션 스키마 작성 및 적용\n"
        "2. 신규 API 엔드포인트 구현 및 입력 유효성 검사\n"
        "3. 단위 테스트 및 통합 테스트 작성 후 통과 확인\n"
        "4. main 브랜치 푸시 및 백엔드 배포 예약\n"
        "단계별로 꼼꼼하게 검증해 주세요."
    )

    # 에이전트가 manage_work를 자발적으로 호출하지 않고 그냥 Run을 큐잉함
    q_run = await client.post(f"/api/sessions/{sid}/runs", headers=auth, json={"prompt": complex_prompt})
    assert q_run.status_code == 202

    # 자동으로 AppSetting에 목표 계약이 등록되어 있어야 함
    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert loaded["state"] is not None
    assert loaded["state"]["status"] == "active"
    req_ids = [r["id"] for r in loaded["state"]["requirements"]]
    assert len(req_ids) >= 4

    # Run이 끝난 뒤 미완료 요구가 있으므로 자동으로 후속 Run이 생성되어야 함
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        run_obj = await db.get(Run, UUID(q_run.json()["id"]))
        run_obj.status = RunStatus.succeeded
        run_obj.result_text = "1번 스키마 작성 완료"
        cont_id = await work.after_run(db, s, run_obj)
        assert cont_id is not None
        await db.commit()


# -------------------------------------------------------------------------------------------------
# Scenario 4: 체크포인트 누락 성과 복원
# -------------------------------------------------------------------------------------------------
async def test_scenario_4_uncheckpointed_progress_restored_from_run_evidence(client, auth, make_dir):
    from ttakkari.runs import events

    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    initial_state = {
        "goal": "Code changes and tests",
        "strategy": "Direct implementation",
        "requirements": [{"id": "1", "text": "Fix bug and add tests", "status": "pending"}],
        "next_action": "Edit code and run pytest",
        "status": "active",
    }
    await client.post("/api/orchestrator/work", headers=h,
                      json={"operation": "checkpoint", "revision": 0, "state": initial_state})

    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        run = Run(session_id=sid, workspace_id=s.workspace_id, engine="claude",
                  prompt="Run edits without checkpointing", status=RunStatus.succeeded)
        db.add(run)
        await db.flush()

        # 도구 호출 및 파일 변경 이벤트 시뮬레이션
        events.append(db, run, "run.diff", {"files": [{"path": "src/ttakkari/runs/manager.py"}], "new_files": []})
        events.append(db, run, "tool.call", {"name": "run_command", "input": {"command": "pytest tests/test_abc.py"}})
        events.append(db, run, "tool.result", {"output": "12 passed in 1.2s"})
        await db.flush()

        cont_id = await work.after_run(db, s, run)
        assert cont_id is not None
        await db.commit()

    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    # 체크포인트를 직접 안 불렀어도 run_evidence에서 파일과 테스트가 상태에 복원되었고 unchanged_continuations가 0으로 리셋됨
    assert "src/ttakkari/runs/manager.py" in loaded["state"]["changed_files"]
    assert any("12 passed" in t for t in loaded["state"]["validation"])
    assert loaded["unchanged_continuations"] == 0


# -------------------------------------------------------------------------------------------------
# Scenario 5: 독립 작업 계속 (한 작업자 실패가 다른 작업자 및 전체 목표를 멈추지 않음)
# -------------------------------------------------------------------------------------------------
async def test_scenario_5_one_worker_failure_does_not_stop_independent_tasks(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    state_init = {
        "goal": "Parallel research and build",
        "strategy": "Spawn two workers",
        "requirements": [
            {"id": "W1", "text": "Worker 1 task", "status": "pending"},
            {"id": "W2", "text": "Worker 2 task", "status": "pending"},
        ],
        "next_action": "Wait for workers",
        "status": "active",
    }
    await client.post("/api/orchestrator/work", headers=h,
                      json={"operation": "checkpoint", "revision": 0, "state": state_init})

    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        w1 = ChatSession(workspace_id=s.workspace_id, parent_id=s.id, title="Worker 1", engine="claude", kind=SessionKind.worker)
        w2 = ChatSession(workspace_id=s.workspace_id, parent_id=s.id, title="Worker 2", engine="codex", kind=SessionKind.worker)
        db.add_all([w1, w2])
        await db.flush()

        # Worker 1 fails
        r1 = Run(session_id=w1.id, workspace_id=s.workspace_id, engine="claude", origin=RunOrigin.orchestrator,
                 prompt="worker 1 task", status=RunStatus.failed, error="subprocess exit 1")
        # Worker 2 is still running
        r2 = Run(session_id=w2.id, workspace_id=s.workspace_id, engine="codex", origin=RunOrigin.orchestrator,
                 prompt="worker 2 task", status=RunStatus.running)
        db.add_all([r1, r2])
        await db.flush()

        # Worker 1 failure should not mark the orchestrator session blocked or paused
        assert await work.after_run(db, w1, r1) is None
        await db.commit()

    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert loaded["state"]["status"] == "active"
    assert "Worker 2" in loaded["running_tasks"]


# -------------------------------------------------------------------------------------------------
# Scenario 6: 일시 오류 (계정 제한, 일시적 엔진 오류 시 blocked 대신 재시도)
# -------------------------------------------------------------------------------------------------
async def test_scenario_6_transient_error_retries_without_blocking_goal(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    state_init = {
        "goal": "Handle transient timeouts",
        "strategy": "Robust retries",
        "requirements": [{"id": "1", "text": "Network call", "status": "pending"}],
        "next_action": "Execute call",
        "status": "active",
    }
    await client.post("/api/orchestrator/work", headers=h,
                      json={"operation": "checkpoint", "revision": 0, "state": state_init})

    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        run = Run(session_id=sid, workspace_id=s.workspace_id, engine="codex",
                  prompt="Do network call", status=RunStatus.failed,
                  error="connection reset by peer: 503 service unavailable")
        db.add(run)
        await db.flush()
        cont_id = await work.after_run(db, s, run)
        assert cont_id is not None
        await db.commit()

    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    # 일시 오류 예산 내이므로 blocked가 아닌 active 유지
    assert loaded["state"]["status"] == "active"
    async with sessionmaker()() as db:
        cont_run = await db.get(Run, cont_id)
        assert "일시적 engine_transient 오류" in cont_run.prompt


# -------------------------------------------------------------------------------------------------
# Scenario 7: 해결 불가 무한 재시도 방지 (동일 오류 반복 시 상한 도달 후 blocked)
# -------------------------------------------------------------------------------------------------
async def test_scenario_7_repeated_same_error_signature_blocks_without_infinite_retry(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    state_init = {
        "goal": "Bounded retries",
        "strategy": "Try and stop",
        "requirements": [{"id": "1", "text": "Failing call", "status": "pending"}],
        "next_action": "Execute failing call",
        "status": "active",
    }
    await client.post("/api/orchestrator/work", headers=h,
                      json={"operation": "checkpoint", "revision": 0, "state": state_init})

    err_text = "503 service unavailable: rate limit exceeded at gateway"
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        # 1st failure -> retry 1
        r1 = Run(session_id=sid, workspace_id=s.workspace_id, engine="codex", prompt="task 1", status=RunStatus.failed, error=err_text)
        db.add(r1)
        await db.flush()
        c1 = await work.after_run(db, s, r1)
        assert c1 is not None

        # 2nd failure -> retry 2
        r2 = Run(session_id=sid, workspace_id=s.workspace_id, engine="codex", prompt="task 2", status=RunStatus.failed, error=err_text)
        db.add(r2)
        await db.flush()
        c2 = await work.after_run(db, s, r2)
        assert c2 is not None

        # 3rd failure with same signature -> budget exceeded (MAX_TRANSIENT_RETRIES=2), blocked!
        r3 = Run(session_id=sid, workspace_id=s.workspace_id, engine="codex", prompt="task 3", status=RunStatus.failed, error=err_text)
        db.add(r3)
        await db.flush()
        c3 = await work.after_run(db, s, r3)
        assert c3 is None
        await db.commit()

    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert loaded["state"]["status"] == "blocked"
    assert "failed" in loaded["state"]["blocker"]


# -------------------------------------------------------------------------------------------------
# Scenario 8: 사용자 중단 (사용자 취소 시 paused 전환 및 예약 취소)
# -------------------------------------------------------------------------------------------------
async def test_scenario_8_user_cancellation_pauses_goal_and_cancels_continuations(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    state_init = {
        "goal": "Long running work",
        "strategy": "Steps",
        "requirements": [{"id": "1", "text": "Step 1", "status": "pending"}],
        "next_action": "Do step 1",
        "status": "active",
    }
    await client.post("/api/orchestrator/work", headers=h,
                      json={"operation": "checkpoint", "revision": 0, "state": state_init})

    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        # 계약 자동 재개 예약
        sched = ScheduledAction(action_type="send_message", payload={"session_id": str(sid), "work_continuation": True},
                                status="pending", due_at=utcnow())
        db.add(sched)
        await db.flush()
        sched_id = sched.id

        # 사용자 취소 발생
        run = Run(session_id=sid, workspace_id=s.workspace_id, engine="codex", prompt="user work", status=RunStatus.cancelled,
                  error="User cancelled via web UI")
        db.add(run)
        await db.flush()
        assert await work.after_run(db, s, run) is None
        await db.commit()

    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert loaded["state"]["status"] == "paused"
    assert "cancelled" in loaded["state"]["blocker"].lower()

    async with sessionmaker()() as db:
        sched_obj = await db.get(ScheduledAction, sched_id)
        assert sched_obj.status == "cancelled"


# -------------------------------------------------------------------------------------------------
# Scenario 9: 단순 질문 비실행 (상태 질문에 계약 생성 및 후속 생성 없음)
# -------------------------------------------------------------------------------------------------
async def test_scenario_9_simple_question_does_not_create_contract_or_continuation(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    simple_prompt = "지금 어떻게 됐어?"
    q_run = await client.post(f"/api/sessions/{sid}/runs", headers=auth, json={"prompt": simple_prompt})
    assert q_run.status_code == 202

    # 계약이 생성되지 않아야 함
    loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
    assert loaded["state"] is None

    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        r = await db.get(Run, UUID(q_run.json()["id"]))
        r.status = RunStatus.succeeded
        r.result_text = "현재 진행 상황을 보고합니다."
        cont_id = await work.after_run(db, s, r)
        assert cont_id is None
        await db.commit()


# -------------------------------------------------------------------------------------------------
# Scenario 10: 범위 변경 연결 (원본 요청 보존 및 재계획 사유)
# -------------------------------------------------------------------------------------------------
async def test_scenario_10_scope_change_preserves_original_and_adapts_requirements(client, auth, make_dir):
    _, h = await lead(client, auth, make_dir)

    initial_state = {
        "goal": "Refactor caching layer",
        "strategy": "Redis migration",
        "requirements": [
            {"id": "1", "text": "Install redis client", "status": "done", "evidence": "pip install redis: ok"},
            {"id": "2", "text": "Migrate session storage", "status": "pending"},
        ],
        "next_action": "Migrate session storage",
        "status": "active",
    }
    r = await client.post("/api/orchestrator/work", headers=h,
                          json={"operation": "checkpoint", "revision": 0, "state": initial_state})
    assert r.status_code == 200

    # 범위 변경: 사용자 요청에 따라 세션 스토리지를 Memcached 대신 로컬 캐시로 변경하고 추가 항목 추가
    new_state = {
        "goal": "Refactor caching layer",
        "strategy": "Use local in-memory cache instead of remote Redis due to latency constraints",
        "requirements": [
            {"id": "1", "text": "Install redis client", "status": "done", "evidence": "pip install redis: ok"},
            {"id": "2", "text": "Migrate session storage", "status": "pending"},
            {"id": "3", "text": "Configure in-memory fallback cache", "status": "pending"},
        ],
        "next_action": "Configure in-memory fallback cache",
        "replan_reason": "사용자 지시에 따른 인메모리 캐시 대체 범위 변경",
        "status": "active",
    }
    r2 = await client.post("/api/orchestrator/work", headers=h,
                           json={"operation": "checkpoint", "revision": 1, "state": new_state})
    assert r2.status_code == 200
    res = r2.json()
    assert res["state"]["replan_reason"] == "사용자 지시에 따른 인메모리 캐시 대체 범위 변경"
    assert len(res["state"]["requirements"]) == 3
    assert res["state"]["requirements"][0]["evidence"] == "pip install redis: ok"


# -------------------------------------------------------------------------------------------------
# Scenario 11: 동시 종료 중복·누락 방지
# -------------------------------------------------------------------------------------------------
async def test_scenario_11_concurrent_run_finishes_do_not_duplicate_continuations(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    state_init = {
        "goal": "Concurrent finish safety",
        "strategy": "Single continuation owner",
        "requirements": [{"id": "1", "text": "Task", "status": "pending"}],
        "next_action": "Do task",
        "status": "active",
    }
    await client.post("/api/orchestrator/work", headers=h,
                      json={"operation": "checkpoint", "revision": 0, "state": state_init})

    async with sessionmaker()() as db1, sessionmaker()() as db2:
        s1 = await db1.get(ChatSession, sid)
        r1 = Run(session_id=sid, workspace_id=s1.workspace_id, engine="claude", prompt="Turn 1", status=RunStatus.succeeded)
        r2 = Run(session_id=sid, workspace_id=s1.workspace_id, engine="codex", prompt="Turn 2", status=RunStatus.succeeded)
        db1.add_all([r1, r2])
        await db1.commit()

        # db1 executes after_run and commits
        c1 = await work.after_run(db1, s1, r1)
        await db1.commit()
        assert c1 is not None

        # db2 executes after_run for r2; seeing active continuation queued, it must return None
        s2 = await db2.get(ChatSession, sid)
        c2 = await work.after_run(db2, s2, r2)
        await db2.commit()
        assert c2 is None

    async with sessionmaker()() as db:
        queued_runs = list(await db.scalars(select(Run).where(Run.session_id == sid, Run.status == RunStatus.queued)))
        # 정확히 1개의 후속 Run만 생성되어야 함 (중복 없음)
        assert len(queued_runs) == 1


# -------------------------------------------------------------------------------------------------
# Scenario 12: 배포·세대 전환 유실 방지
# -------------------------------------------------------------------------------------------------
async def test_scenario_12_continuation_survives_generation_and_restart(client, auth, make_dir, tmp_path):
    from test_generations import generations

    from ttakkari.generation import activate
    from ttakkari.models import RuntimeLease

    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])

    state_init = {
        "goal": "Deployment resiliency",
        "strategy": "Persist continuation across generation fence",
        "requirements": [{"id": "1", "text": "Survive restart", "status": "pending"}],
        "next_action": "Verify restart safety",
        "status": "active",
    }
    await client.post("/api/orchestrator/work", headers=h,
                      json={"operation": "checkpoint", "revision": 0, "state": state_init})

    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        r = Run(session_id=sid, workspace_id=s.workspace_id, engine="claude", prompt="turn", status=RunStatus.succeeded)
        db.add(r)
        await db.flush()
        cont_id = await work.after_run(db, s, r)
        await db.commit()

    # 세대 활성화 및 lease 확인
    async with generations(tmp_path) as (owner, _):
        await activate(owner.id, expected=None)
        token = await owner.claim(cont_id, "leads", 8)
        assert token

        async with sessionmaker()() as db:
            cont_run = await db.get(Run, cont_id)
            assert cont_run.status == RunStatus.queued
            assert (cont_run.usage or {}).get("work_continuation") is not None
            lease = await db.get(RuntimeLease, cont_id)
            assert lease is not None and lease.token == token

        # 세대 전환 및 fence 확인: 후속 Run은 유실되거나 손상되지 않고 보존됨
        loaded = (await client.post("/api/orchestrator/work", headers=h, json={"operation": "get"})).json()
        assert loaded["state"]["status"] == "active"
        assert loaded["pending_scope"][0]["id"] == "1"
