"""Track A+B 독립 검증에서 찾은 결함의 재현 회귀 시험. 기존 시험은 건드리지 않고 반례 입력만 더한다."""
from __future__ import annotations

import asyncio
import subprocess
from uuid import UUID

import pytest
from goal_helpers import contract, lead, make_worker, new_run, put, raw, request
from sqlalchemy import select
from test_goal_confirm_original_items_mcp import _complete_state
from test_worker_completion import _spawn

from ttakkari.config import get_settings
from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession, Run, RunOrigin, RunStatus
from ttakkari.orchestrator import observed, work
from ttakkari.orchestrator.goal_signals import extract_items, unreported_items
from ttakkari.runs import completion
from ttakkari.runs.manager import manager


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


@pytest.fixture
def contract_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_completion_contract", True)


def _pool(tests=(), calls=3, partial=False):
    return {"calls": calls, "tests": list(tests), "shas": set(), "commits": [], "files": {}, "writes": {},
            "deploys": [], "runs": 1, "partial": partial}


def _test(outcome, passed=None, target="pytest:tests", run="r1"):
    return {"outcome": outcome, "passed": passed, "target": target, "run_id": run, "line": "x", "seq": 1}


def _git_repo(root) -> None:
    for args in (["init", "-q"], ["-c", "user.email=a@b", "-c", "user.name=n", "commit", "-q", "--allow-empty", "-m", "m"]):
        subprocess.run(["git", *args], cwd=root, check=True)


async def _codes(text, pool, roots=()):
    res = await observed.check_claims(text, pool, list(roots))
    return [c["code"] for c in res["contradictions"]], [c["code"] for c in res["unverified"]]


def _feed(events, run_id="r", git_tracked=False):
    ob = observed.Observer(git_tracked=git_tracked)
    for i, (kind, payload) in enumerate(events, 1):
        ob.feed(i, kind, payload)
    return ob.finish(run_id)


def _shell(cid, cmd, out, code=0):
    return [("tool.call", {"name": "Bash", "call_id": cid, "input": {"command": cmd}}),
            ("tool.result", {"call_id": cid, "output": out, "exit_code": code})]


# ---- A: 거짓 완료 우회 -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "환경 문제 없이 pytest 100 passed",
    "pytest 100 passed (미확인 항목 없음)",
    "pytest 100 passed, 미실행 0건",
    "No tests skipped. pytest 100 passed",
])
async def test_negated_decline_words_do_not_exempt_a_false_test_claim(text):
    contra, _ = await _codes(text, _pool())
    assert "test_not_run" in contra


async def test_declined_clause_does_not_exempt_a_false_commit_in_the_same_sentence(make_dir):
    root = make_dir()
    _git_repo(root)
    contra, unv = await _codes("배포는 미실행, 커밋 deadbee1 푸시 완료", _pool(), [root])
    assert contra == ["commit_not_found"] and unv == []
    # 정직한 미실행 선언 자체는 거부하지 않는다.
    assert await _codes("pytest 는 환경에 없어 미실행", _pool(), [root]) == ([], [])


async def test_all_letter_fake_sha_is_checked(make_dir):
    root = make_dir()
    _git_repo(root)
    assert (await _codes("커밋 deadbeef 푸시", _pool(), [root]))[0] == ["commit_not_found"]


@pytest.mark.parametrize("text", ["pytest 100 passed", "pytest: 100 passed in 3s", "테스트 100건 통과", "100 passed"])
async def test_claimed_pass_count_is_compared_whatever_the_phrasing(text):
    contra, _ = await _codes(text, _pool([_test("passed", 5)]))
    assert contra == ["test_count_mismatch"]
    assert await _codes(text.replace("100", "5"), _pool([_test("passed", 5)])) == ([], [])


async def test_summed_count_over_separate_targets_is_not_a_mismatch():
    tests = [_test("passed", 40, "pytest:tests/a.py", "r1"), _test("passed", 55, "pytest:tests/b.py", "r2")]
    assert await _codes("pytest 95 passed", _pool(tests)) == ([], [])


@pytest.mark.parametrize("claim_text", [
    "... main 27854b83, 기획 재실행 287 passed",
    "재검증: 전체 pytest 7081 passed exit=0 @ f67b5d0d, 프론트 전체 vitest 1937 passed",
    "ops_deploy_log 5685 passed, 3 skipped, 5016 subtests passed",
])
async def test_operational_case_outside_runs_evidence_count_with_other_observed_targets(claim_text):
    """운영 사례 재현: 다른 Run 에서 pytest 파일 단위로 통과한 44·55·99만 관측됐고,
    주장 건수(287, 7081, 5685)는 관측 범위 밖(이전 Run, 백그라운드 tail, 배포 게이트)의 실행인 경우.
    수정 전에는 test_count_mismatch 로 거부되었으나, 수정 후에는 unverified 로 분류된다."""
    tests = [
        _test("passed", 44, "pytest:tests/test_goal_summary_api.py", "r1"),
        _test("passed", 55, "pytest:tests/delivery", "r2"),
        _test("passed", 99, "pytest:tests/test_goal_reliability_track_ab_verify.py", "r3"),
    ]
    pool = _pool(tests)
    res = await observed.check_claims(claim_text, pool, [])
    assert res["contradictions"] == []
    assert any(u["code"] == "test_unobservable" for u in res["unverified"])
    assert observed.evidence_level(res) == "unverified"


async def test_same_target_test_count_mismatch_is_still_rejected():
    """같은 Run 에서 pytest 44 passed 를 관측한 뒤 다른 건수를 주장하면 여전히 거부."""
    tests = [_test("passed", 44, "pytest:tests/test_goal_summary_api.py", "r1")]
    pool = _pool(tests)
    res = await observed.check_claims("pytest 287 passed", pool, [])
    assert any(c["code"] == "test_count_mismatch" for c in res["contradictions"])
    assert observed.evidence_level(res) == "contradicted"


async def test_honest_failure_report_and_other_tool_failure_are_not_contradictions():
    assert await _codes("pytest 94 passed, 1 failed (기존 실패)", _pool([_test("failed")])) == ([], ["declared_failure"])
    # 마지막 관측이 ruff 실패여도 pytest 성공 주장은 pytest 결과와 맞춘다.
    tests = [_test("passed", 95), _test("failed", None, "ruff_check:src")]
    assert await _codes("pytest 95 passed", _pool(tests)) == ([], [])
    # `0 failed` 를 덧붙여 실패 보고인 척하는 우회는 통하지 않는다.
    assert (await _codes("pytest 100 passed, 0 failed", _pool()))[0] == ["test_not_run"]


@pytest.mark.parametrize("cmd", ["grep -rn pytest pyproject.toml", "cat tests/test_pytest_plugin.py",
                                 "cd /w && git commit -m 'add pytest config'", "uv add pytest", "echo pytest passed"])
def test_commands_that_only_mention_a_test_tool_are_not_test_runs(cmd):
    assert _feed(_shell("x", cmd, "ok")).get("tests") == []
    assert observed.is_test_cmd(cmd) is False


def test_real_test_commands_still_count():
    for cmd in ("uv run pytest tests/a.py -q", "cd /w && uv run pytest -q 2>&1 | tail -3", ".venv/bin/pytest", "npm run test"):
        assert observed.is_test_cmd(cmd), cmd
    assert observed.test_target("cd /w && grep x y && uv run pytest tests/a.py -q") == "pytest:tests/a.py"


async def test_partial_pool_downgrades_absence_based_contradictions(make_dir):
    root = make_dir()
    _git_repo(root)
    merged = observed.merge_pool([{"partial": True}, {"run_id": "r1", "calls": 3}])
    assert merged["partial"] is True and merged["runs"] == 1
    contra, unv = await _codes("커밋 abc1234567 푸시. pytest 9 passed", merged, [root])
    assert contra == [] and {"commit_unverifiable", "test_unobservable"} & set(unv) or "commit_unverifiable" in unv
    assert "test_not_run" not in contra


async def test_pool_observations_marks_partial_when_old_runs_are_skipped(client, auth, make_dir, monkeypatch):
    sess, _h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    for _ in range(3):
        await new_run(sid, status=RunStatus.succeeded, events=[("tool.call", {"name": "noop", "call_id": "c"})])
    monkeypatch.setattr(observed, "MAX_POOL_SCANS", 1)
    async with sessionmaker()() as db:
        pool_obs, _roots = await observed.pool_observations(db, await db.get(ChatSession, sid))
    assert pool_obs[0].get("partial") is True
    assert observed.merge_pool(pool_obs)["partial"] is True


async def test_planner_uses_commit_made_by_its_worker_not_in_its_own_runs(client, auth, make_dir):
    """기획 자신의 Run 에는 없는 증거: 작업자가 만든 커밋을 기획이 완료 증거로 쓴다."""
    root = make_dir()
    _git_repo(root)
    sess, h = await lead(client, auth, lambda: root)
    sid = UUID(sess["id"])
    wid = await make_worker(sid)
    await new_run(sid, session_id=wid, status=RunStatus.succeeded, events=_events(
        _shell("w1", "cd /wt && git commit -am fix", "[feat-x 4f3e2d1] fix bug\n 1 file changed")))
    c = contract("버그 수정", reqs=[{"id": "R1", "text": "수정", "status": "done", "evidence": "commit 4f3e2d1 on feat-x"}])
    res = await put(client, h, {**c, "status": "complete", "next_action": ""})
    assert res.status_code == 200, res.text
    assert (await raw(sid))["evidence_audit"]["R1"]["level"] == "observed"


def _events(pairs):
    return list(pairs)


# ---- A 운영 사례 B: 도구 이벤트 형태의 fixture --------------------------------------------------------------------
async def _case_b_worker(client, auth, make_dir, extra_events):
    prompt = "DS 조립기 브랜치를 병합하고 운영에 배포한다."
    _orch, wid = await _spawn(client, auth, make_dir, prompt)
    events = [
        *_shell("v1", "cd /wt/ds && vercel --prod", "Error: Upload aborted\nToo many requests - try again in 24 hours", 1),
        *extra_events]
    async with sessionmaker()() as db:
        run = (await db.scalars(select(Run).where(Run.session_id == UUID(wid)).order_by(Run.created_at.desc()))).first()
        from ttakkari.runs import events as ev
        for kind, payload in events:
            ev.append(db, run, kind, payload)
        run.result_text = "상태: 완료\nDS 조립기를 병합했고 `vercel --prod` 로 운영 배포 완료했습니다."
        run.status = RunStatus.succeeded
        sess = await db.get(ChatSession, UUID(wid))
        info = await completion.cross_check(db, sess, run, completion.annotate(sess, run))
        await db.commit()
    return info


async def test_case_b_failed_vercel_deploy_claimed_complete_is_flagged(client, auth, make_dir, contract_on):
    info = await _case_b_worker(client, auth, make_dir, [])
    assert info["violation"] is True and "deploy_failed" in info["evidence_mismatch"]


async def test_case_b_deploy_confirmed_through_another_path_is_not_blocked(client, auth, make_dir, contract_on):
    """같은 커밋이 Git 연동 배포로 Ready 가 된 것을 같은 Run 이 확인했다면(대체 경로) 거부하지 않는다."""
    info = await _case_b_worker(client, auth, make_dir, _shell("v2", "vercel ls ds-assembler", "ds-assembler  Ready  2m", 0))
    assert not info.get("violation") and "deploy_failed" not in (info.get("evidence_mismatch") or [])


async def test_case_d_honest_partial_report_is_not_cross_checked(client, auth, make_dir, contract_on):
    """사례 D: 정직한 부분 보고(`상태: 부분`, 미실행·승인 대기 명시)는 거부 대상이 아니다."""
    prompt = "1. 하네스 스키마 구현\n2. 러너 연동\n3. 배포 승인 요청"
    _orch, wid = await _spawn(client, auth, make_dir, prompt)
    async with sessionmaker()() as db:
        run = (await db.scalars(select(Run).where(Run.session_id == UUID(wid)).order_by(Run.created_at.desc()))).first()
        run.result_text = "상태: 부분\n남은 일: 대기\n1. 스키마 구현 완료\n2. 러너 연동 미실행\n3. 승인 대기"
        run.status = RunStatus.succeeded
        sess = await db.get(ChatSession, UUID(wid))
        info = await completion.cross_check(db, sess, run, completion.annotate(sess, run))
        await db.commit()
    assert not info.get("violation") and "evidence_mismatch" not in info


# ---- A: 보고의 항목 표기 ----------------------------------------------------------------------------------------
_CARD = ("완료 기준:\n1. 거짓 완료 차단 반례 탐색: 존재하지 않는 SHA, 실행 안 한 테스트, 실패 테스트\n"
         "2. 과잉 차단 탐색: 코드 변경 없는 분석·리뷰 작업, 이전 Run·다른 작업자 증거 재사용\n"
         "3. 원본 항목 확인: user_approved 가 에이전트 자기 주장만으로 통과하는지 확인")


@pytest.mark.parametrize("report", [
    "상태: 완료\n- 거짓 완료 차단 반례 탐색: 우회 3건 발견\n- 과잉 차단 탐색: 2건 확인\n- 원본 항목 확인: 자기 주장 통과 확인",
    "상태: 완료\n1번: 거짓 완료 우회 3건 발견\n2번: 과잉 차단 2건 확인\n3번: 자기 주장 통과 확인",
    "상태: 완료\n| 1 | 우회 3건 발견 |\n| 2 | 과잉 차단 2건 확인 |\n| 3 | 자기 주장 통과 확인 |",
    "상태: 완료\n거짓 완료를 막는지 반례를 돌려 우회됨\n과잉 차단은 비 Git 편집에서 발견\n승인 필드는 자기 주장으로 통과됨",
])
def test_compliant_item_reports_are_not_flagged_as_partial(report):
    assert unreported_items(extract_items(_CARD), report) == []


def test_report_covering_only_some_items_is_still_flagged():
    assert [m["label"] for m in unreported_items(extract_items(_CARD), "상태: 완료\n1번: 우회 3건 발견")] == ["2", "3"]
    assert [m["label"] for m in unreported_items(extract_items(_CARD), "상태: 완료\n전체적으로 잘 처리했습니다")] == ["1", "2", "3"]


# ---- A-5: 범위 제외 승인은 에이전트 자기 주장만으로 인정하지 않는다 -----------------------------------------------------
def _exclusion(complete_state):
    return [{"label": "1", "requirement_id": "A"}, {"label": "2", "requirement_id": "B"},
            {"label": "3", "requirement_id": "C"},
            {"label": "4", "excluded": True, "reason": "배포는 별도 단계로 진행하기로 합의", "user_approved": True}]


async def test_user_approved_claim_without_a_later_user_message_is_flagged_for_review(client, auth, make_dir):
    sid, h, _rev, complete = await _complete_state(client, auth, make_dir)
    res = await put(client, h, complete, original_item_dispositions=_exclusion(complete))
    assert res.status_code == 200, res.text  # 코드로 확정 못 하는 것은 거부하지 않고 검토 대상으로 둔다
    rec = next(d for d in (await raw(sid))["original_item_dispositions"] if d["kind"] == "excluded")
    assert rec["approval_claimed"] is True and rec["approval_observed"] is False and rec["user_approved"] is False
    assert (await raw(sid))["state"]["status"] == "complete"
    async with sessionmaker()() as db:
        data = await work.summary(db, sid)
    assert data["unapproved_scope_exclusions"] == 1 and data["user_action"] == "review"


async def test_user_approved_claim_after_a_later_user_message_is_recorded_as_approved(client, auth, make_dir):
    sid, h, _rev, complete = await _complete_state(client, auth, make_dir)
    await asyncio.sleep(0.01)
    await request(sid, "배포는 나중에 하고 나머지 먼저 마무리해줘", origin=RunOrigin.user)
    res = await put(client, h, complete, original_item_dispositions=_exclusion(complete))
    assert res.status_code == 200, res.text
    rec = next(d for d in (await raw(sid))["original_item_dispositions"] if d["kind"] == "excluded")
    assert rec["approval_observed"] is True and rec["user_approved"] is True
    async with sessionmaker()() as db:
        assert (await work.summary(db, sid))["unapproved_scope_exclusions"] == 0


async def test_requirement_id_must_point_to_an_existing_requirement(client, auth, make_dir):
    sid, h, _rev, complete = await _complete_state(client, auth, make_dir)
    ghost = [{"label": "1", "requirement_id": "ZZ"}, {"label": "2", "requirement_id": "B"},
             {"label": "3", "requirement_id": "C"}, {"label": "4", "requirement_id": "D"}]
    res = await put(client, h, complete, original_item_dispositions=ghost)
    assert res.status_code == 409 and "ZZ is not a done requirement" in res.text
    assert (await raw(sid))["state"]["status"] == "active"  # 거부된 완료는 계약을 바꾸지 않는다


# ---- B: 진전 판정 ----------------------------------------------------------------------------------------------
def _progress(events, ledger, run_id, git_tracked=False):
    obs = _feed(events, run_id, git_tracked)
    return observed.assess_progress(obs, ledger)


def test_noop_push_and_merge_phrasing_variants_are_not_progress():
    ledger = None
    for cmd in ("git push origin main", "git push origin HEAD", "git push origin HEAD:main"):
        out = _progress(_shell("c", cmd, "Everything up-to-date"), ledger, "r")
        ledger = out["ledger"]
        assert out["progressed"] is False, cmd
    for cmd in ("git merge main", "git merge origin/main"):
        out = _progress(_shell("c", cmd, "Already up to date."), ledger, "r")
        ledger = out["ledger"]
        assert out["progressed"] is False, cmd
    # 실제 푸시는 그대로 진전이다.
    real = _progress(_shell("c", "git push origin main", "To x\n   1a2b3c4..5d6e7f8  main -> main"), ledger, "r")
    assert real["progressed"] is True


def test_non_git_edits_to_the_same_file_in_later_runs_are_new_work():
    edit = [("tool.call", {"name": "Edit", "call_id": "e", "input": {"file_path": "/w/doc.md"}}),
            ("tool.result", {"call_id": "e", "output": "ok"})]
    first = _progress(edit, None, "run-aaaa1111")
    second = _progress(edit, first["ledger"], "run-bbbb2222")
    assert first["progressed"] and second["progressed"]
    # 같은 Run 안에서 같은 파일을 여러 번 고쳐도 한 번으로 센다.
    again = _progress(edit + edit, second["ledger"], "run-bbbb2222")
    assert again["progressed"] is False


@pytest.mark.parametrize("result_id", ["unknown", None, ""])
def test_placeholder_or_missing_result_call_id_falls_back_to_the_single_pending_call(result_id):
    result = {"output": "5 passed in 1s", "exit_code": 0}
    if result_id is not None:
        result["call_id"] = result_id
    obs = _feed([("tool.call", {"name": "bash", "call_id": "call_0", "input": {"command": "uv run pytest -q"}}),
                 ("tool.result", result)])
    assert len(obs["tests"]) == 1 and obs["unlinked_results"] == 0


def test_placeholder_result_ids_do_not_cross_link_parallel_calls():
    obs = _feed([("tool.call", {"name": "bash", "call_id": "a", "input": {"command": "uv run pytest -q"}}),
                 ("tool.call", {"name": "bash", "call_id": "b", "input": {"command": "ls"}}),
                 ("tool.result", {"call_id": "unknown", "output": "5 passed"}),
                 ("tool.result", {"call_id": "b", "output": "a.py"})])
    assert obs["tests"] == [] and obs["unlinked_results"] == 1

