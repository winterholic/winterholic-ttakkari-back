"""663d56d5 독립 검증(P0-1~4, P1-1~7, P2)에서 나온 결함의 회귀 시험. 각 시험은 결함 재현 시나리오를 그대로 단언한다."""
from __future__ import annotations

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
from sqlalchemy import select, text

from ttakkari.db import sessionmaker
from ttakkari.models import AppSetting, ChatSession, Run, RunOrigin, RunStatus
from ttakkari.orchestrator import work
from ttakkari.orchestrator.goal_signals import extract_items, failure_kind, is_complex
from ttakkari.runs.manager import manager

COMPLEX = ("다음 4단계 백엔드 작업을 진행해 주세요.\n1. 데이터베이스 마이그레이션 스키마 작성 및 적용\n"
           "2. 신규 API 엔드포인트 구현 및 입력 유효성 검사\n3. 단위 테스트 및 통합 테스트 작성 후 통과 확인\n"
           "4. main 브랜치 푸시 및 백엔드 배포 예약\n")
DUP = ("두 부분으로 나눠서 진행해 주세요.\n[백엔드]\n1. 데이터베이스 마이그레이션 스키마 작성 및 적용\n2. 신규 API 엔드포인트 구현\n"
       "3. 단위 테스트 작성 후 통과 확인\n[프런트]\n1. 로그인 화면 수정 및 적용\n2. 오류 메시지 구현 및 정리\n3. 화면 테스트 작성 후 통과 확인\n")
REPORT = ("[작업 보고] 작업 세션 '로그인 수정' (worker_id=abc) 완료.\n결과:\n상태: 완료\n"
          "- 로그인 버그 수정 및 회귀 테스트 추가, 검증 통과 확인\n- 커밋 abc123 푸시 완료, 배포 예약 정리\n"
          "- 문서 작성과 마이그레이션 적용 완료, 보고서 첨부\n작업 세션 컨텍스트: 12% 사용, 여유 충분")


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


# ---- P0-1 ---------------------------------------------------------------------------------------------
async def test_p0_1_duplicate_numbered_labels_save_the_run_and_open_a_contract(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": DUP})
    assert r.status_code == 202, r.text
    ids = [q["id"] for q in (await get(client, h))["state"]["requirements"]]
    assert len(ids) == 6 and len(set(ids)) == 6
    async with sessionmaker()() as db:
        assert len(list(await db.scalars(select(Run).where(Run.session_id == UUID(sess["id"]))))) == 1
    assert len({i["label"] for i in extract_items(DUP)}) == 6


@pytest.mark.parametrize("failure", ["raise", "db_error"])
async def test_p0_1_intake_failure_never_blocks_saving_the_request(client, auth, make_dir, monkeypatch, failure):
    sess, h = await lead(client, auth, make_dir)

    async def broken(db, *args, **kwargs):
        if failure == "db_error":
            await db.execute(text("SELECT 1/0"))  # 트랜잭션을 망치는 DB 오류
        raise RuntimeError("boom")

    monkeypatch.setattr(work, "intake_request", broken)
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": COMPLEX})
    assert r.status_code == 202, r.text
    async with sessionmaker()() as db:
        assert len(list(await db.scalars(select(Run).where(Run.session_id == UUID(sess["id"]))))) == 1
    assert (await get(client, h))["state"] is None


async def test_p0_1_non_orchestrator_sessions_get_no_contract(client, auth, make_dir):
    sess, _ = await lead(client, auth, make_dir, kind="chat")
    r = await client.post(f"/api/sessions/{sess['id']}/runs", headers=auth, json={"prompt": COMPLEX})
    assert r.status_code == 202
    async with sessionmaker()() as db:
        assert await db.get(AppSetting, work.key(UUID(sess["id"]))) is None


# ---- P0-2 ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("origin", [RunOrigin.orchestrator, RunOrigin.system, RunOrigin.report, RunOrigin.desk, RunOrigin.user])
@pytest.mark.parametrize("status", ["blocked", "paused", "active"])
async def test_p0_2_no_origin_overwrites_a_live_contract(client, auth, make_dir, origin, status):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    extra = {} if status == "active" else {"blocker": "사용자 중단 요청 / 정확한 사유"}
    assert (await put(client, h, contract("기존 목표", status=status, **extra))).status_code == 200
    before = await get(client, h)
    await request(sid, COMPLEX, origin=origin)
    after = await get(client, h)
    assert after["state"] == before["state"]  # 요구·증거·blocker 그대로
    if origin in (RunOrigin.user, RunOrigin.desk):
        assert after["revision"] == before["revision"] + 1 and len(after["additional_requests"]) == 1
    else:
        assert after["revision"] == before["revision"] and "additional_requests" not in after


async def test_p0_2_complete_contract_is_archived_not_erased_by_user_request(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    reqs = [{"id": "R1", "text": "이전 요구", "status": "done", "evidence": "pytest -q: 58 passed"}]
    assert (await put(client, h, contract("이전 목표", reqs=reqs, status="complete", next_action=""))).status_code == 200
    await request(sid, COMPLEX, origin=RunOrigin.system)  # 시스템 메시지는 완료 계약도 건드리지 못한다
    assert (await get(client, h))["state"]["goal"] == "이전 목표"
    await request(sid, COMPLEX)
    assert "58 passed" in str((await raw(sid))["history"])


# ---- P0-3 ---------------------------------------------------------------------------------------------
async def test_p0_3_failed_run_defers_to_queued_user_run_and_live_worker(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await new_run(sid, status=RunStatus.queued, origin=RunOrigin.user, prompt="사용자 지시")
    w = await make_worker(sid)
    await new_run(sid, session_id=w, status=RunStatus.running)
    failed = await new_run(sid)
    await finish(failed, RunStatus.failed, error="connection reset by peer")
    assert continuations(await queued(sid)) == [] and len(await queued(sid)) == 1
    assert "transient_retries" not in await raw(sid)  # 소유자가 있으면 예산도 쓰지 않는다


async def test_p0_3_live_worker_alone_owns_the_next_step_after_a_failure(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    w = await make_worker(sid)
    await new_run(sid, session_id=w, status=RunStatus.running)
    await finish(await new_run(sid), RunStatus.failed, error="503 service unavailable")
    assert await queued(sid) == []


# ---- P0-4 ---------------------------------------------------------------------------------------------
async def test_p0_4_worker_report_run_never_creates_a_contract_or_continuation(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert is_complex(REPORT + "\n- 마이그레이션을 적용하고 푸시해서 배포해줘")  # 보고문이 복합 요청처럼 보여도
    report = await request(sid, REPORT, origin=RunOrigin.report)
    await finish(report.id, RunStatus.succeeded, result_text="확인")
    assert (await get(client, h))["state"] is None and await raw(sid) == {}
    assert continuations(await queued(sid)) == []
    # 계약이 없는 세션에서 after_run 을 직접 불러도 계약을 만들지 않는다.
    async with sessionmaker()() as db:
        s = await db.get(ChatSession, sid)
        run = Run(session_id=sid, workspace_id=s.workspace_id, engine="claude", origin=RunOrigin.report, prompt=REPORT,
                  status=RunStatus.succeeded)
        db.add(run)
        await db.flush()
        assert await work.after_run(db, s, run) is None
        await db.commit()
    assert await raw(sid) == {}


# ---- P1 -----------------------------------------------------------------------------------------------
async def test_p1_2_paraphrased_complete_warns_once_then_passes_with_confirmation(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await request(sid, COMPLEX)
    english = [{"id": k, "text": t, "status": "pending"} for k, t in
               (("A", "DB schema migration"), ("B", "API endpoints with validation"), ("C", "tests"), ("D", "push and deploy"))]
    # 계약이 자동 접수 상태이고 완료된 요구가 없으면 기획이 자기 계획으로 한 번 재구성할 수 있다.
    assert (await put(client, h, contract("backend work", reqs=english))).status_code == 200
    cur = await get(client, h)
    done = [{**r, "status": "done", "evidence": "pytest -q: 58 passed; commit abc123"} for r in cur["state"]["requirements"]]
    complete = {**cur["state"], "requirements": done, "status": "complete", "next_action": ""}
    r = await put(client, h, complete)
    assert r.status_code == 409 and "confirm_original_items" in r.text
    ok = await put(client, h, complete, confirm_original_items=True)
    assert ok.status_code == 200 and (await raw(sid))["original_items_confirmed"]


async def test_p1_2_faithful_contract_completes_without_confirmation(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    await request(UUID(sess["id"]), COMPLEX)
    cur = await get(client, h)
    done = [{**r, "status": "done", "evidence": "pytest -q: 58 passed"} for r in cur["state"]["requirements"]]
    r = await put(client, h, {**cur["state"], "requirements": done, "status": "complete", "next_action": ""})
    assert r.status_code == 200, r.text


async def test_p1_2_second_request_in_reused_session_is_not_checked_against_the_first(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await request(sid, "처음에는 이것만 해주세요: 문서 정리")  # 계약 없이 지나간 첫 요청
    first = [{"id": "R1", "text": "이전 작업", "status": "done", "evidence": "pytest -q: 1 passed"}]
    assert (await put(client, h, contract("이전", reqs=first, status="complete", next_action=""))).status_code == 200
    await request(sid, COMPLEX)  # 새 계약, 원본은 이 요청
    cur = await get(client, h)
    assert cur["original_request"]["prompt"] == COMPLEX


async def test_p1_3_no_progress_follow_ups_stop_after_two_continuations(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    first = await request(sid, COMPLEX)
    run, made = first.id, []
    for _ in range(5):
        await finish(run, RunStatus.succeeded, result_text="아무것도 안 함")
        follow = continuations(await queued(sid))
        made.append(bool(follow))
        if not follow:
            break
        run = follow[0].id
    assert made == [True, True, False] and work.MAX_UNCHANGED_CONTINUATIONS == 2
    state = (await get(client, h))["state"]
    assert state["status"] == "blocked" and "after 2 automatic continuations" in state["blocker"]


async def test_p1_4_progress_between_failures_resets_the_transient_budget(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    for i in range(3):  # 같은 오류가 세 번 나도 사이사이 진전이 있으면 blocked 가 아니다
        await finish(await new_run(sid), RunStatus.failed, error="connection reset by peer")
        assert (await get(client, h))["state"]["status"] == "active", i
        progress = await new_run(sid, events=[("run.diff", {"files": [{"path": f"f{i}.py"}], "new_files": []})])
        await finish(progress, RunStatus.succeeded)
        assert "transient_retries" not in await raw(sid)
        async with sessionmaker()() as db:  # 다음 반복이 소유자 없이 시작하게 대기 후속을 비운다
            for r in await queued(sid):
                (await db.get(Run, r.id)).status = RunStatus.cancelled
            await db.commit()


def test_p1_5_long_logs_are_not_complex_but_natural_compound_requests_are():
    log = "다음 에러가 왜 나는지 설명해줘\n" + "Traceback ... File x.py line 1 in fix\n" * 40
    question = ("우리 아키텍처에서 세션 캐시를 redis 로 옮기는 방안을 검토 중인데 장단점이 궁금해. " * 12) + "구현 난이도는 어느 정도일까?"
    fenced = "이 코드가 왜 실패하는지 알려줘\n```\n" + "fix build deploy push commit\n" * 40 + "```"
    assert not any(is_complex(t) for t in (log, question, fenced, "지금 어떻게 됐어?", "README 오타 수정하고 커밋해줘"))
    assert is_complex("버그 수정하고 회귀 테스트 추가해서 푸시, 배포, 보고서")  # 75자 미만 자연어 복합 요청
    assert is_complex("사용자 인증 흐름에서 토큰 갱신이 가끔 실패해. 원인을 분석하고, 재현 테스트를 작성하고, 수정해서 배포까지 진행해줘.")


# ---- P2 -----------------------------------------------------------------------------------------------
async def test_p2_1_distinct_error_signatures_have_a_total_cap(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    run, made = await new_run(sid), 0
    for name in "abcdefgh":
        await finish(run, RunStatus.failed, error=f"connection reset by {name}")
        follow = continuations(await queued(sid))
        if not follow:
            break
        made, run = made + 1, follow[0].id
    assert made == work.MAX_TRANSIENT_TOTAL
    assert (await get(client, h))["state"]["status"] == "blocked"


async def test_p2_2_derived_fields_are_not_persisted(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await finish(await new_run(sid), RunStatus.succeeded)
    stored = await raw(sid)
    assert not {"completed_scope", "pending_scope", "running_tasks", "goal_status", "stop_reason", "auto_resumable"} & stored.keys()
    assert {"completed_scope", "pending_scope", "auto_resumable"} <= (await get(client, h)).keys()


async def test_p2_5_active_contract_with_everything_done_is_not_abandoned(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    reqs = [{"id": "R1", "text": "일", "status": "done", "evidence": "pytest -q: 1 passed"}]
    assert (await put(client, h, contract(reqs=reqs))).status_code == 200
    await finish(await new_run(sid), RunStatus.succeeded)
    follow = continuations(await queued(sid))
    assert len(follow) == 1 and "complete" in follow[0].prompt


@pytest.mark.parametrize(("error", "kind"), [
    ("You've hit your usage limit. try again at 5pm", "usage_limit"),
    ("429 rate limit exceeded", "usage_limit"),
    ("Reached weekly limit for this account", "usage_limit"),
    ("The selected model is at capacity", "model_capacity"),
    ("model capacity exhausted", "engine_transient"),
    ("connection reset by peer", "engine_transient"),
    ("", "engine_transient"),
    ("authentication failed 401", "permanent"),
    ("permission denied: /etc", "permanent"),
    ("exit code 1", "permanent"),
])
def test_p2_7_failure_kind(error, kind):
    assert failure_kind(error) == kind
