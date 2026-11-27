"""복합 작업이 전체 범위 완료 전에 조용히 끝나지 않게 하는 12개 사용자 시나리오.

테스트 이름의 번호와 제목이 사용자 시나리오 원문에 그대로 대응한다. 각 테스트는 실제 경로를 부른다:
접수(queue_run·HTTP) → 계약 → 종료(RunManager._finish → work.after_run·보고·완료 확인) → 후속 Run 큐.
엔진 프로세스는 띄우지 않고(dispatch 무력화) DB 상태와 큐만 본다.

1) 10개 중 3개 수행 후 정상 종료 시 7개 유지·이어감   2) 미완료 상태 완료 선언 차단
3) 계약 누락 복합 요청이 조용히 종료되지 않음          4) 체크포인트 누락 성과 복원
5) 특정 작업 실패에도 독립 작업 계속                   6) 일시 엔진·계정 오류가 영구 중단으로 이어지지 않음
7) 해결 불가 문제 무한 재시도 없음                     8) 사용자 명시 중단 시 자동 이어가기 없음
9) 일반 질문·단순 요청 비실행                          10) 후속 요청 범위 변경 시 기존 완료 증거와 새 요청 연결
11) 여러 작업자 동시 종료 시 후속 Run 중복·누락 없음     12) 배포·세대 전환 중 목표와 후속 실행 유실 없음
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import pytest
from goal_helpers import (
    continuations,
    contract,
    finish,
    get,
    lead,
    make_worker,
    new_run,
    put,
    queued,
    raw,
    request,
)
from sqlalchemy import select

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunOrigin, RunStatus, ScheduledAction, utcnow
from ttakkari.orchestrator import work
from ttakkari.orchestrator.goal_signals import DESK_HEADER
from ttakkari.runs.manager import _compose_prompt, manager


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


TEN = "다음 10개 작업을 모두 진행해 주세요.\n" + "\n".join(f"{i}. 항목{i} 모듈 구현 및 테스트 작성" for i in range(1, 11))
NATURAL = "버그 수정하고 회귀 테스트 추가해서 푸시, 배포, 보고서"
NUMBERED4 = ("다음 4단계 백엔드 작업을 진행해 주세요.\n1. 데이터베이스 마이그레이션 스키마 작성 및 적용\n"
             "2. 신규 API 엔드포인트 구현 및 입력 유효성 검사\n3. 단위 테스트 및 통합 테스트 작성 후 통과 확인\n"
             "4. main 브랜치 푸시 및 백엔드 배포 예약\n")
TRACE = "다음 에러가 왜 나는지 설명해줘\n" + "Traceback ... File x.py line 1 in fix\n" * 40


def done(item, i):
    return {**item, "status": "done", "evidence": f"pytest tests/test_item{i}.py: 1 passed"}


async def test_scenario_01_three_of_ten_done_then_normal_exit_keeps_seven_and_continues(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    run = await request(sid, TEN)  # 사용자가 10개를 요청했다. 기획은 manage_work 로 계약을 직접 만들지 않았다.
    c = await get(client, h)
    assert len(c["state"]["requirements"]) == 10
    reqs = [done(r, i) if i < 3 else r for i, r in enumerate(c["state"]["requirements"])]
    assert (await put(client, h, {**c["state"], "requirements": reqs})).status_code == 200

    await finish(run.id, RunStatus.succeeded, result_text="3개를 끝냈습니다.")  # 엔진 턴이 정상 종료

    after = await get(client, h)
    assert after["state"]["status"] == "active"
    assert len(after["completed_scope"]) == 3 and len(after["pending_scope"]) == 7
    follow = continuations(await queued(sid))
    assert len(follow) == 1 and follow[0].origin == RunOrigin.orchestrator
    pending_line = next(ln for ln in follow[0].prompt.splitlines() if ln.startswith("- 남은 미완료 항목"))
    assert all(f"항목{i} 모듈" in pending_line for i in range(4, 11))
    assert not any(f"항목{i} 모듈" in pending_line for i in (1, 2, 3))
    assert "재실행 금지" in follow[0].prompt
    assert "completed_scope" not in await raw(sid)  # 파생 필드는 응답에만 붙고 저장되지 않는다


async def test_scenario_02_completion_declaration_with_unfinished_work_is_blocked(client, auth, make_dir, monkeypatch):
    from ttakkari.config import get_settings
    monkeypatch.setattr(get_settings(), "worker_completion_contract", True)
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    a_done = {"id": "A", "text": "Part A", "status": "done", "evidence": "pytest: 5 passed"}

    r = await put(client, h, contract(status="complete", reqs=[a_done, {"id": "B", "text": "Part B", "status": "pending"}]))
    assert r.status_code == 422 and "unfinished requirements: B" in r.text
    weak = {**a_done, "evidence": "완료"}
    r = await put(client, h, contract(status="complete", reqs=[weak]))
    assert r.status_code == 422 and "weak" in r.text

    # 작업자가 증거 없이 `상태: 완료` 만 보고하면 실제 종료 경로가 위반으로 보고 완료 확인 Run 을 넣는다.
    w = await make_worker(sid)
    wrun = await new_run(sid, session_id=w, status=RunStatus.running)
    await finish(wrun, RunStatus.succeeded, result_text="상태: 완료\n완료")
    async with sessionmaker()() as db:
        info = (await db.get(Run, wrun)).usage["completion"]
    assert info["violation"] and info["weak_evidence"]
    closure = [r for r in await queued(w) if (r.usage or {}).get("closure")]
    assert len(closure) == 1 and "실행 증거" in closure[0].prompt
    # 완료 확인 Run 이 남아 있는 동안 계약 완료도 막힌다.
    r = await put(client, h, contract(status="complete", reqs=[a_done]))
    assert r.status_code == 422 and "unfinished worker execution" in r.text
    # 증거를 갖춘 보고가 끝난 뒤에는 같은 완료 선언이 통과한다.
    await finish(closure[0].id, RunStatus.succeeded, result_text="상태: 완료\n1) foo.py 수정\n   $ pytest -q → 5 passed")
    assert (await put(client, h, contract(status="complete", reqs=[a_done]))).status_code == 200


async def test_scenario_03_complex_request_without_contract_never_ends_silently(client, auth, make_dir, monkeypatch):
    cases = [("http", NATURAL), ("desk", DESK_HEADER + "\n" + NUMBERED4)]
    for via, prompt in cases:
        sess, h = await lead(client, auth, make_dir)
        sid = UUID(sess["id"])
        if via == "http":
            r = await client.post(f"/api/sessions/{sid}/runs", headers=auth, json={"prompt": prompt})
            assert r.status_code == 202
            run_id = UUID(r.json()["id"])
        else:
            run_id = (await request(sid, prompt, origin=RunOrigin.desk)).id
        c = await get(client, h)  # manage_work 는 한 번도 호출하지 않았다
        assert c["state"]["status"] == "active" and c["original_request"]["run_id"] == str(run_id)
        assert len(c["state"]["requirements"]) >= (1 if via == "http" else 4)

        await finish(run_id, RunStatus.succeeded, result_text="일부만 하고 끝")
        follow = continuations(await queued(sid))
        assert len(follow) == 1, via
        assert follow[0].prompt.startswith("[미완료 목표 자동 이어가기]")
        assert (await get(client, h))["state"]["status"] == "active"


async def test_scenario_04_unrecorded_progress_is_restored_from_run_evidence(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract(reqs=[{"id": "1", "text": "Fix bug and add tests", "status": "pending"}]))).status_code == 200
    ev = [("run.diff", {"files": [{"path": "src/ttakkari/runs/manager.py"}], "new_files": []}),
          ("tool.call", {"name": "run_command", "input": {"command": "pytest tests/test_abc.py"}}),
          ("tool.result", {"output": "12 passed in 1.2s"})]
    run = await new_run(sid, events=ev)  # 체크포인트는 한 번도 안 불렀다
    before = await get(client, h)
    await finish(run, RunStatus.succeeded)

    c = await get(client, h)
    assert "src/ttakkari/runs/manager.py" in c["state"]["changed_files"]
    assert any("12 passed" in t for t in c["state"]["validation"])
    assert c["revision"] == before["revision"] + 1
    follow = continuations(await queued(sid))
    assert len(follow) == 1 and "파일 1개 변경" in follow[0].prompt

    # 대기 중인 소유자가 있어 후속을 만들지 않는 종료에서도 복원한 성과는 저장된다.
    run2 = await new_run(sid, events=[("run.diff", {"files": [{"path": "src/ttakkari/api/sessions.py"}], "new_files": []})])
    await finish(run2, RunStatus.succeeded)
    c2 = await get(client, h)
    assert {"src/ttakkari/runs/manager.py", "src/ttakkari/api/sessions.py"} <= set(c2["state"]["changed_files"])
    assert len(continuations(await queued(sid))) == 1


async def test_scenario_05_one_worker_failure_keeps_independent_work_going(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    reqs = [{"id": "W1", "text": "작업자1 과제", "status": "pending"}, {"id": "W2", "text": "작업자2 과제", "status": "pending"}]
    assert (await put(client, h, contract(reqs=reqs))).status_code == 200
    w1, w2 = await make_worker(sid, "작업자1"), await make_worker(sid, "작업자2")
    r1 = await new_run(sid, session_id=w1, status=RunStatus.running)
    r2 = await new_run(sid, session_id=w2, status=RunStatus.running)

    await finish(r1, RunStatus.failed, error="subprocess exit 1")  # 작업자1 실패
    assert (await get(client, h))["state"]["status"] == "active"
    reports = await queued(sid, RunOrigin.report)
    assert len(reports) == 1 and "실패" in reports[0].prompt and str(w1) in reports[0].prompt
    async with sessionmaker()() as db:
        assert (await db.get(Run, r2)).status == RunStatus.running  # 독립 작업자는 영향 없음

    # 기획이 실패 보고를 처리하는 동안 작업자2가 아직 일하므로 중복 후속은 없다.
    await finish(reports[0].id, RunStatus.succeeded, result_text="실패 확인")
    assert continuations(await queued(sid)) == []
    assert (await get(client, h))["state"]["status"] == "active"

    # 작업자2가 끝나면 그 보고를 받고, 실패한 W1 은 미완료로 남아 계속 이어진다.
    await finish(r2, RunStatus.succeeded, result_text="상태: 완료\n1) 작업자2 과제\n   $ pytest -q → 3 passed")
    report2 = (await queued(sid, RunOrigin.report))[0]
    c = await get(client, h)
    new = [{**r, "status": "done", "evidence": "pytest -q: 3 passed"} if r["id"] == "W2" else r for r in c["state"]["requirements"]]
    assert (await put(client, h, {**c["state"], "requirements": new})).status_code == 200
    await finish(report2.id, RunStatus.succeeded, result_text="W2 반영")
    follow = continuations(await queued(sid))
    assert len(follow) == 1 and "작업자1 과제" in follow[0].prompt
    after = await get(client, h)
    assert after["state"]["status"] == "active" and [r["id"] for r in after["pending_scope"]] == ["W1"]


async def test_scenario_06_transient_engine_and_account_errors_do_not_become_permanent_stop(client, auth, make_dir):
    # 일시 엔진 오류: 백오프 뒤 같은 목표를 이어간다.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    run = await new_run(sid)
    await finish(run, RunStatus.failed, error="connection reset by peer: 503 service unavailable")
    follow = continuations(await queued(sid))
    assert len(follow) == 1 and follow[0].resume_after > utcnow()
    assert (await get(client, h))["state"]["status"] == "active"

    # 계정 한도: 즉시 재시도하지 않고 대기하며, 반복돼도 blocked 로 굳지 않는다.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    limit = "You've hit your usage limit. Upgrade or try again later."
    run = await new_run(sid)
    for n in range(1, 4):
        await finish(run, RunStatus.failed, error=limit)
        follow = continuations(await queued(sid))
        assert len(follow) == 1, n
        assert follow[0].resume_after >= utcnow() + timedelta(seconds=60)  # 즉시 재시도 금지
        assert manager.is_waiting(follow[0].id)  # 해제 시각에 깨우도록 걸렸다
        state = await get(client, h)
        assert state["state"]["status"] == "active" and state["waiting_until"]
        run = follow[0].id  # 대기 끝에 이 후속이 다시 한도에 걸렸다고 본다
    assert (await raw(sid))["usage_waits"] == 3
    # 한도가 풀려 진전이 생기면 대기 예산이 비워진다.
    progress = await new_run(sid, events=[("run.diff", {"files": [{"path": "a.py"}], "new_files": []})])
    await finish(progress, RunStatus.succeeded)
    assert "usage_waits" not in await raw(sid)


async def test_scenario_07_unsolvable_problem_is_not_retried_forever(client, auth, make_dir):
    # (a) 영구 오류는 즉시 blocked.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    await finish(await new_run(sid), RunStatus.failed, error="authentication failed 401")
    assert (await get(client, h))["state"]["status"] == "blocked" and continuations(await queued(sid)) == []

    async def chain(errors, limit):
        sess, h = await lead(client, auth, make_dir)
        sid = UUID(sess["id"])
        await put(client, h, contract())
        run, made = await new_run(sid), 0
        for err in errors:
            await finish(run, RunStatus.failed, error=err)
            follow = continuations(await queued(sid))
            if not follow:
                break
            made += 1
            run = follow[0].id
        assert made <= limit
        return sid, (await get(client, h))["state"]

    # (b) 같은 지문은 2회 이어간 뒤 3번째에 blocked.
    sid, st = await chain(["503 service unavailable"] * 10, 2)
    assert st["status"] == "blocked" and (await raw(sid))["blocker_kind"] == "transient_exhausted"
    # (c) 서로 다른 지문도 연속 4회가 상한이다.
    sid, st = await chain([f"connection reset by host-{name}" for name in "abcdefghij"], work.MAX_TRANSIENT_TOTAL)
    assert st["status"] == "blocked"
    # (d) 한도 대기도 횟수 상한이 있다.
    sid, st = await chain(["usage limit reached"] * 30, work.MAX_USAGE_WAITS)
    assert st["status"] == "blocked" and (await raw(sid))["blocker_kind"] == "usage_limit_exhausted"
    # (e) 진전 없는 정상 종료는 후속 2회 뒤 3번째 무진전 턴에 blocked.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run, made = await new_run(sid), []
    for _ in range(5):
        await finish(run, RunStatus.succeeded, result_text="아무것도 안 함")
        follow = continuations(await queued(sid))
        made.append(bool(follow))
        if not follow:
            break
        run = follow[0].id
    # 체크포인트가 만든 첫 진전 뒤의 후속 2회까지 허용한다(진전 직후 1, 무진전 1).
    assert made == [True, True, False]
    assert (await get(client, h))["state"]["status"] == "blocked"


async def test_scenario_08_explicit_user_stop_never_auto_resumes(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await put(client, h, contract())
    run = await new_run(sid)
    await finish(run, RunStatus.succeeded)  # 후속이 하나 걸려 있다
    assert len(continuations(await queued(sid))) == 1
    async with sessionmaker()() as db:
        db.add_all([ScheduledAction(action_type="send_message", payload={"session_id": str(sid), "work_continuation": True},
                                    status="pending", due_at=utcnow() + timedelta(hours=1)),
                    ScheduledAction(action_type="send_message", payload={"session_id": str(sid)}, status="pending",
                                    due_at=utcnow() + timedelta(hours=1))])
        await db.commit()

    stopped = await new_run(sid)  # 사용자가 실행 중인 기획 Run 을 중단
    await finish(stopped, RunStatus.cancelled, error="사용자가 중단했습니다.")
    st = (await get(client, h))["state"]
    assert st["status"] == "paused" and "cancelled" in st["blocker"]
    assert continuations(await queued(sid)) == []  # 대기 중이던 자동 후속은 취소됐다
    async with sessionmaker()() as db:
        scheds = list(await db.scalars(select(ScheduledAction).order_by(ScheduledAction.created_at)))
    assert sorted(s.status for s in scheds) == ["cancelled", "pending"]  # 사용자 개인 예약은 보존

    # 예약·시스템·보고 Run 은 중단된 목표를 되살리지 못한다.
    revision = (await get(client, h))["revision"]
    for origin in (RunOrigin.orchestrator, RunOrigin.system, RunOrigin.report):
        await request(sid, NUMBERED4, origin=origin)
        c = await get(client, h)
        assert c["state"]["status"] == "paused" and c["revision"] == revision, origin
    # 사용자가 새 복합 요청을 보내도 계약은 되살아나지 않고 원문만 보존된다. 해제는 기획의 체크포인트로만 한다.
    new = await request(sid, NUMBERED4)
    c = await get(client, h)
    assert c["state"]["status"] == "paused" and [r["prompt"] for r in c["additional_requests"]] == [NUMBERED4]
    await finish(new.id, RunStatus.succeeded, result_text="확인")
    assert continuations(await queued(sid)) == []


async def test_scenario_09_questions_and_simple_requests_do_not_run_a_contract(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    for prompt in ("지금 어떻게 됐어?", "README 오타 수정하고 커밋해줘", TRACE,
                   ("세션 캐시를 redis 로 옮기는 방안의 장단점이 궁금해. " * 12) + "구현 난이도는 어떨까?"):
        run = await request(sid, prompt)
        await finish(run.id, RunStatus.succeeded, result_text="답변")
        assert (await get(client, h))["state"] is None, prompt[:20]
        assert await raw(sid) == {}
        assert continuations(await queued(sid)) == []
    # 대조군: 같은 경로가 복합 요청에서는 계약을 연다(위 검사가 헛돌지 않음).
    await request(sid, NUMBERED4)
    assert (await get(client, h))["state"]["status"] == "active"


async def test_scenario_10_followup_scope_change_links_prior_completion_evidence(client, auth, make_dir):
    # (a) 진행 중인 계약: 새 요청은 계약을 덮지 않고 원문이 추가 요청으로 남아 기획 프롬프트에 노출된다.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    base = contract("캐시 계층 개편", reqs=[{"id": "R1", "text": "redis 클라이언트 설치", "status": "done", "evidence": "pip show redis: 5.0"},
                                         {"id": "R2", "text": "세션 저장소 이전", "status": "pending"}])
    assert (await put(client, h, base)).status_code == 200
    follow = ("범위를 바꿔 주세요. redis 대신 로컬 캐시를 쓰고 싶어요.\n1. LRU 캐시 구현으로 교체\n"
              "2. 관련 회귀 테스트 추가\n3. 변경 사항 푸시 및 배포")
    new = await request(sid, follow)
    c = await get(client, h)
    assert c["state"]["status"] == "active" and [r["id"] for r in c["state"]["requirements"]] == ["R1", "R2"]
    assert [r["prompt"] for r in c["additional_requests"]] == [follow]
    async with sessionmaker()() as db:
        composed = await _compose_prompt(db, await db.get(Run, new.id))
    assert "추가 요청" in composed and "LRU 캐시 구현으로 교체" in composed and "pip show redis: 5.0" in composed
    # 기획이 범위를 합쳐 요구를 추가한다. 보존된 추가 요청은 체크포인트 뒤에도 남는다.
    merged = {**c["state"], "strategy": "로컬 캐시로 전환", "replan_reason": "사용자 범위 변경",
              "requirements": [*c["state"]["requirements"], {"id": "R3", "text": "LRU 로컬 캐시", "status": "pending"}]}
    assert (await put(client, h, merged)).status_code == 200
    assert len((await get(client, h))["additional_requests"]) == 1
    # 요구가 모두 done 이어도 추가 요청의 번호 항목이 대응되지 않아 보이면 먼저 경고하고, 확인하면 완료된다.
    cur = await get(client, h)
    all_done = [{**r, "status": "done", "evidence": r.get("evidence") or "pytest -q: 9 passed"} for r in cur["state"]["requirements"]]
    complete = {**cur["state"], "requirements": all_done, "status": "complete", "next_action": ""}
    warned = await put(client, h, complete)
    assert warned.status_code == 409 and "관련 회귀 테스트 추가" in warned.text
    assert (await put(client, h, complete, confirm_original_items=True)).status_code == 200
    assert (await raw(sid))["original_items_confirmed"]

    # (b) 완료된 계약 뒤 새 요청: 이전 완료 기록은 지우지 않고 보관하고 새 계약을 연다.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    old = contract("1차 배포", status="complete", reqs=[{"id": "R1", "text": "1차 구현", "status": "done", "evidence": "pytest -q: 58 passed"}],
                   next_action="")
    assert (await put(client, h, old)).status_code == 200
    new = await request(sid, NUMBERED4)
    c = await get(client, h)
    assert c["state"]["status"] == "active" and len(c["state"]["requirements"]) == 4
    assert c["original_request"]["run_id"] == str(new.id) and c["additional_requests"] == []
    hist = (await raw(sid))["history"]
    assert len(hist) == 1 and hist[0]["state"]["requirements"][0]["evidence"] == "pytest -q: 58 passed"
    async with sessionmaker()() as db:
        composed = await _compose_prompt(db, await db.get(Run, new.id))
    assert "이전 완료 계약" in composed and "58 passed" in composed


async def test_scenario_11_simultaneous_worker_finishes_neither_duplicate_nor_drop_followups(client, auth, make_dir):
    # (a) 작업자 4개와 기획 Run 이 동시에 끝난다: 보고는 하나로 합쳐져 하나도 빠지지 않고, 후속은 중복되지 않는다.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    workers = [await make_worker(sid, f"w{i}") for i in range(4)]
    runs = [await new_run(sid, session_id=w, status=RunStatus.running) for w in workers]
    lead_run = await new_run(sid)
    await asyncio.gather(*[finish(r, RunStatus.succeeded, result_text="상태: 완료\n1) 수정 a.py\n   $ pytest -q → 1 passed") for r in runs],
                         finish(lead_run, RunStatus.succeeded))
    reports = await queued(sid, RunOrigin.report)
    assert all(any(str(w) in rep.prompt for rep in reports) for w in workers)  # 누락 없음
    assert len(reports) == 1  # 중복 없음
    assert continuations(await queued(sid)) == []  # 보고가 다음 단계의 소유자

    # (b) 기획 Run 4개가 동시에 끝나도 후속 Run 은 하나만 생긴다.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    ids = [await new_run(sid) for _ in range(4)]
    await asyncio.gather(*[finish(i, RunStatus.succeeded) for i in ids])
    assert len(continuations(await queued(sid))) == 1

    # (c) 복합 요청 4개가 동시에 들어와도 계약은 하나이고 요청 원문은 모두 남는다.
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    prompts = [NUMBERED4.replace("4단계", f"{n}번째 4단계") for n in range(4)]
    created = await asyncio.gather(*[request(sid, p) for p in prompts])
    c = await get(client, h)
    keep = {c["original_request"]["run_id"], *(r["run_id"] for r in c["additional_requests"])}
    assert keep == {str(r.id) for r in created} and c["state"]["status"] == "active"


async def test_scenario_12_goal_and_followup_survive_generation_switch_and_restart(client, auth, make_dir, tmp_path):
    from test_generations import generations

    from ttakkari.generation import activate
    from ttakkari.models import RuntimeLease
    from ttakkari.runs.manager import resume_interrupted, resume_queued

    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    run = await request(sid, NUMBERED4)  # 접수에서 계약이 자동으로 열린다
    async with generations(tmp_path) as (old, new):
        await activate(old.id, expected=None)
        assert await old.claim(run.id, "leads", 8)  # 구세대가 기획 Run 을 실행 중
        await activate(new.id, expected=old.id)  # 배포: 새 세대로 전환, 구세대는 draining
        await finish(run.id, RunStatus.succeeded, result_text="일부만 끝")  # 구세대에서 Run 종료 → 후속 생성
        follow = continuations(await queued(sid))
        assert len(follow) == 1
        assert await old.claim(follow[0].id, "leads", 8) is None  # 구세대는 더 가져가지 못한다
        async with sessionmaker()() as db:
            lease = await db.get(RuntimeLease, run.id)
            if lease is not None:
                await db.delete(lease)  # 구세대 Run 의 lease 는 종료와 함께 풀린다
                await db.commit()
        token = await new.claim(follow[0].id, "leads", 8)
        assert token  # 새 세대가 후속을 이어받는다
        async with sessionmaker()() as db:
            assert (await db.get(RuntimeLease, follow[0].id)).generation_id == new.id
        assert (await get(client, h))["state"]["status"] == "active"

        # 새 세대 재시작: 후속이 실행 중에 끊긴다. 목표는 유지되고 후속은 중복되지 않으며 같은 Run 이 다시 큐에 선다.
        async with sessionmaker()() as db:
            cont = await db.get(Run, follow[0].id)
            cont.status = RunStatus.running
            await db.commit()
        await finish(follow[0].id, RunStatus.interrupted, error="서버 종료로 중단")
        assert (await get(client, h))["state"]["status"] == "active"
        assert len(continuations(await queued(sid))) == 0
        assert await resume_interrupted(force=True) == 1
        again = await queued(sid)
        assert [r.id for r in again] == [follow[0].id] and again[0].resume_after is not None
        async with sessionmaker()() as db:  # 백오프가 지난 시각으로 본다
            lease = await db.get(RuntimeLease, follow[0].id)
            if lease is not None:
                await db.delete(lease)
            (await db.get(Run, follow[0].id)).resume_after = utcnow() - timedelta(seconds=1)
            await db.commit()
        assert await resume_queued() >= 1
        assert await new.claim(follow[0].id, "leads", 8)
        async with sessionmaker()() as db:
            s = await db.get(ChatSession, sid)
            assert s is not None
            owners = list(await db.scalars(select(Run).where(Run.session_id == sid, Run.status.in_((RunStatus.queued, RunStatus.running)))))
        assert [r.id for r in owners] == [follow[0].id]  # 후속은 처음부터 끝까지 하나
        final = await get(client, h)
        assert final["state"]["status"] == "active" and len(final["pending_scope"]) == 4
