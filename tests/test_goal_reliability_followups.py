"""0623175b 독립 재검증의 P1·P2 와 작업자 보고 불일치(백그라운드 대기 종료) 회귀 시험."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
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
from ttakkari.models import ChatSession, Run, RunOrigin, RunStatus, utcnow
from ttakkari.orchestrator import work
from ttakkari.orchestrator.goal_signals import classify_followup, is_complex
from ttakkari.runs import completion
from ttakkari.runs.manager import _compose_prompt, manager

TASK_CARD = ("목표: 백엔드 재작업 결과를 읽기 전용으로 독립 재검증한다.\n범위와 금지:\n"
             "- 읽기 전용 검증이다. 코드 수정, 커밋, push, 배포 요청을 하지 않는다. 임시 테스트는 새 worktree 안에만 둔다.\n"
             "- 서브 작업자를 만들지 않는다. 전체 테스트 스위트는 돌리지 않는다.\n완료 기준:\n1. P0 각각에 대해 판정한다.\n"
             "2. 시나리오를 다시 판정한다.\n3. 새 위험을 찾는다.\n4. 테스트를 돌린다.\n5. 결함을 나눠 적는다.")
NOT_COMPLEX = {
    "explain_verbs": "배포 과정에서 수정, 테스트, 구현 방식이 어떻게 다른지 설명해줘",
    "numbered_choice": "캐시 방식으로 뭐가 좋을까?\n1. redis 를 쓴다\n2. memcached 를 쓴다\n3. 로컬 캐시를 쓴다\n각각 장단점을 알려줘",
    "no_edit": "코드는 수정하지 말고 테스트 결과를 확인하고 원인을 분석해서 보고해줘",
    "readonly_task_card": TASK_CARD,
    "review_explain": "이 PR 리뷰하고 문제점 정리해서 설명해줘",
    "memo_question": "어제 회의에서 구현하기로 한 항목 정리하고 추가할 거 있는지 알려줘 수정 필요하면 말해줘",
    "numbered_explain": "다음 3가지 배포 방식을 설명해줘\n1. 수정 배포 방식\n2. 구현 배포 방식\n3. 롤백 배포 방식",
}
COMPLEX = {
    "natural": "백엔드 로그인 버그를 수정하고 회귀 테스트도 추가한 다음 main 에 푸시하고 배포까지 해줘. 끝나면 결과를 보고서로 정리해서 알려줘.",
    "short_compound": "버그 수정하고 회귀 테스트 추가해서 푸시, 배포, 보고서",
    "numbered": "다음을 진행해줘.\n1. 마이그레이션 작성\n2. API 구현\n3. 테스트 작성\n4. 배포",
    "with_unrelated_ban": "로그인 버그 수정하고 회귀 테스트 추가해서 푸시하고 배포해줘. 프론트는 건드리지 말고.",
}
WAITING_REPORT = "배포 사전 검증 및 push를 진행하고 있습니다. 완료 알림을 대기합니다.\n상태: 완료"


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


@pytest.fixture
def contract_on(monkeypatch):
    from ttakkari.config import get_settings
    monkeypatch.setattr(get_settings(), "worker_completion_contract", True)


# ---- P1-1 복합 오탐 ------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", sorted(NOT_COMPLEX))
async def test_explanations_choices_negations_and_readonly_requests_open_no_contract(client, auth, make_dir, name):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert not is_complex(NOT_COMPLEX[name])
    run = await request(sid, NOT_COMPLEX[name])
    assert (await get(client, h))["state"] is None
    await finish(run.id, RunStatus.succeeded, result_text="답변 드립니다")
    assert continuations(await queued(sid)) == [] and await raw(sid) == {}


@pytest.mark.parametrize("name", sorted(COMPLEX))
async def test_real_compound_requests_still_open_a_contract(client, auth, make_dir, name):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert is_complex(COMPLEX[name])
    run = await request(sid, COMPLEX[name])
    assert (await get(client, h))["state"]["status"] == "active"
    await finish(run.id, RunStatus.succeeded, result_text="일부만 함")
    assert len(continuations(await queued(sid))) == 1


def test_negated_and_explanatory_followups_are_not_scope_changes():
    assert classify_followup(NOT_COMPLEX["no_edit"]) != "scope_change"
    assert classify_followup("redis 말고 로컬 캐시를 쓰도록 바꿔줘") == "scope_change"


async def test_active_contract_ignores_explanatory_followups(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await request(sid, NOT_COMPLEX["no_edit"])
    await request(sid, NOT_COMPLEX["numbered_choice"])
    assert "additional_requests" not in await get(client, h)


# ---- P1-2 문구 변경은 진전이 아니다 ---------------------------------------------------------------------
async def test_external_wait_with_reworded_checkpoints_ends_blocked_within_the_bound(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    run, made = await new_run(sid), []
    for i in range(8):
        cur = await get(client, h)
        reworded = {**cur["state"], "completed_work": [*cur["state"]["completed_work"], f"팀 리드 승인 대기 확인 {i}"],
                    "next_action": f"승인 대기 {i}"}
        assert (await put(client, h, reworded)).status_code == 200
        await finish(run, RunStatus.succeeded, result_text="승인 대기")
        follow = continuations(await queued(sid))
        made.append(bool(follow))
        if not follow:
            break
        run = follow[0].id
    assert made == [True, True, False]
    assert (await get(client, h))["state"]["status"] == "blocked"


@pytest.mark.parametrize("kind", ["requirement_done", "changed_file", "validation"])
async def test_real_checkpoint_progress_keeps_the_goal_going(client, auth, make_dir, kind):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    run, made = await new_run(sid), []
    for i in range(6):
        cur = await get(client, h)
        st = {**cur["state"]}
        if kind == "requirement_done" and i < 2:
            st["requirements"] = [{**r, "status": "done", "evidence": f"pytest -q: {i + 1} passed"} if r["id"] == f"R{i + 1}" else r
                                  for r in st["requirements"]]
        elif kind == "changed_file":
            st["changed_files"] = [*st["changed_files"], f"src/f{i}.py"]
        elif kind == "validation":
            st["validation"] = [*st["validation"], f"pytest -q: {i} passed"]
        if st["requirements"] and all(r["status"] == "done" for r in st["requirements"]):
            break
        assert (await put(client, h, st)).status_code == 200
        await finish(run, RunStatus.succeeded, result_text="진행")
        follow = continuations(await queued(sid))
        made.append(bool(follow))
        if not follow:
            break
        run = follow[0].id
    assert all(made) and (await get(client, h))["state"]["status"] == "active"


def test_progress_then_stall_matches_the_tool_description():
    # 진전 뒤 후속 2회까지: 진전 직후 후속이 첫 번째, 무진전 후속이 두 번째, 그다음 무진전 턴은 blocked.
    src = Path(work.__file__).with_name("mcp_server.py").read_text()
    assert "the one issued right after a progress turn is the first" in src
    assert "rewording completed_work or evidence is not progress" in src
    assert work.MAX_UNCHANGED_CONTINUATIONS == 2


async def test_progress_then_stall_sequence(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    run, seq = await new_run(sid, events=[("run.diff", {"files": [{"path": "a.py"}], "new_files": []})]), []
    for _ in range(5):
        await finish(run, RunStatus.succeeded, result_text="x")
        follow = continuations(await queued(sid))
        seq.append(bool(follow))
        if not follow:
            break
        run = follow[0].id
    assert seq == [True, True, False]


# ---- 작업자 보고의 상태·본문 불일치 --------------------------------------------------------------------
async def _worker_report(client, auth, make_dir, text, status=RunStatus.succeeded):
    sess, _ = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    w = await make_worker(sid)
    run = await new_run(sid, session_id=w, status=RunStatus.running)
    await finish(run, status, result_text=text)
    async with sessionmaker()() as db:
        info = (await db.get(Run, run)).usage["completion"]
    closures = [r for r in await queued(w) if (r.usage or {}).get("closure")]
    return sid, w, run, info, closures


@pytest.mark.parametrize("text", [
    WAITING_REPORT,
    "테스트를 백그라운드로 걸어 두었습니다. 알림이 오면 확인합니다.\n상태: 완료",
    "상태: 완료\n1) 배포는 vercel 업로드를 진행 중입니다. 결과를 기다리는 중입니다.",
])
async def test_completion_declared_while_still_waiting_continues_through_closure(client, auth, make_dir, contract_on, text):
    sid, w, _, info, closures = await _worker_report(client, auth, make_dir, text)
    assert info["violation"] and info["inconsistent"] == "waiting"
    assert len(closures) == 1 and "포그라운드로 끝까지 기다려" in closures[0].prompt
    reports = await queued(sid, RunOrigin.report)
    assert "완료 계약 위반" in reports[0].prompt and "진행·대기 중" in reports[0].prompt
    # 완료 확인 Run 자신의 보고는 다시 검사하지 않아 폭주하지 않는다.
    await finish(closures[0].id, RunStatus.succeeded, result_text="아직 알림을 대기합니다.\n상태: 완료")
    assert [r for r in await queued(w) if (r.usage or {}).get("closure")] == []


async def test_completion_declared_with_partial_body_continues(client, auth, make_dir, contract_on):
    text = "상태: 완료\n1) 테스트 통과: pytest -q → 58 passed\n2) 운영 배포: 부분 충족입니다. vercel --prod 가 실패했습니다."
    _, _, _, info, closures = await _worker_report(client, auth, make_dir, text)
    assert info["inconsistent"] == "partial" and len(closures) == 1


@pytest.mark.parametrize("text", [
    "상태: 완료\n1) foo.py 수정\n   $ pytest -q → 58 passed\n미실행: 전체 스위트(범위 밖)\n하지 않음: 프런트 저장소(범위 밖)",
    "상태: 완료\n대기 중인 Run 이 없음을 확인했습니다. 진행 중이던 이전 Run 은 취소됨. 미완료 항목 없음.\n$ git log -1 → 0623175b",
    "상태: 부분\n남은 일: 대기\nCI 결과를 기다립니다. push 는 abc123 로 끝났습니다.",
    "상태: 막힘\n승인 알림을 대기합니다. 시도: pytest -q → 3 failed",
])
async def test_honest_reports_are_not_blocked_by_the_mismatch_rule(client, auth, make_dir, contract_on, text):
    _, _, _, info, closures = await _worker_report(client, auth, make_dir, text)
    assert not info["violation"] and "inconsistent" not in info and closures == []


def test_english_background_wait_with_status_line_is_detected():
    # 운영 사례(DS 조립기 첫 세션): 영어로 "테스트 체인이 끝나길 기다리며 턴을 끝낸다"고 쓴 보고.
    text = ("Waiting for the test chain to finish.\nThe timer is already set, so I'll end this turn and wait for it.\n"
            "The chain will notify me when it ends, so I'm stopping here until then.")
    assert completion.inconsistent_reason("완료", text, None) == "waiting"
    assert completion.inconsistent_reason("완료", "Ran the test chain: 794 passed. Nothing is pending.", None) is None


def test_inconsistent_reason_rules():
    assert completion.inconsistent_reason("부분", "배포 알림을 기다립니다", None) == "waiting"
    assert completion.inconsistent_reason("부분", "배포 알림을 기다립니다", "대기") is None
    assert completion.inconsistent_reason("완료", "모두 수정했고 $ pytest → 3 passed", None) is None


# ---- P2 ----------------------------------------------------------------------------------------------
async def test_new_user_request_releases_waiting_continuation_but_not_unowned_runs(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await finish(await new_run(sid), RunStatus.failed, error="You've hit your usage limit.")
    waiting = continuations(await queued(sid))[0]
    assert waiting.resume_after > utcnow()
    async with sessionmaker()() as db:  # 계약이 만들지 않은 같은 모양의 대기 Run 은 보존돼야 한다
        other = Run(session_id=sid, workspace_id=waiting.workspace_id, engine="claude", prompt="예약 작업", origin=RunOrigin.orchestrator,
                    status=RunStatus.queued, resume_after=utcnow() + timedelta(hours=1))
        db.add(other)
        await db.commit()
        other_id = other.id

    user = await request(sid, "지금 어떻게 돼가?")
    async with sessionmaker()() as db:
        released = await db.get(Run, waiting.id)
        assert released.status == RunStatus.cancelled and "supersedes" in released.error
        assert (await db.get(Run, other_id)).status == RunStatus.queued
    assert [r.id for r in await queued(sid)] == [other_id, user.id]
    await finish(user.id, RunStatus.succeeded, result_text="진행 중")
    # 사용자 Run 이 끝나면 미완료 요구로 후속이 다시 이어진다(대기 후속은 사용자 Run 뒤에 세워지지 않았다).
    assert (await get(client, h))["state"]["status"] == "active"


async def test_usage_wait_reads_try_again_in_hours_hint(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    assert (await put(client, h, contract())).status_code == 200
    await finish(await new_run(sid), RunStatus.failed, error="You've hit your usage limit. Try again in 3 hours.")
    wait = continuations(await queued(sid))[0].resume_after - utcnow()
    assert timedelta(hours=2, minutes=50) < wait < timedelta(hours=3, minutes=1)


async def test_original_request_of_a_new_contract_is_the_latest_request_not_the_first_of_the_session(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await request(sid, "이것저것 알려줘\n1. 현재 상태\n2. 다음 계획")  # 계약 없이 지나간 첫 요청
    await request(sid, "새 작업 하나만 부탁해")
    reqs = [{"id": "R1", "text": "전혀 다른 새 작업 수행", "status": "done", "evidence": "pytest -q: 3 passed"}]
    r = await put(client, h, contract("새 작업", reqs=reqs, status="complete", next_action=""))
    assert r.status_code == 200, r.text
    run_ids = list(await _run_ids(sid))
    assert (await raw(sid))["original_request"]["run_id"] == str(run_ids[-1])


async def _run_ids(sid):
    async with sessionmaker()() as db:
        return await db.scalars(select(Run.id).where(Run.session_id == sid, Run.origin == RunOrigin.user).order_by(Run.created_at, Run.id))


# ---- GPT 교차 검증 P1: 짧은 자연어 복합·한 줄 번호 항목 누락 ------------------------------------------------
SHORT_COMPOUND = {
    "counter_1": "조사하고 고쳐서 배포해",
    "counter_2": "이 부분 개선해줘: 1. 오류 원인 조사 / 2. 코드 수정 / 3. 회귀 테스트 추가",
    "investigate_then_fix": "원인 조사 후 수정하고 테스트 추가해서 배포해줘",
    "find_fix_test_push": "버그 찾아서 고치고 테스트 돌리고 푸시해줘",
    "analyze_fix_deploy": "분석하고 수정한 다음 배포까지 해",
    "comma_nouns_with_verb": "오류 수정, 테스트 추가, 배포 진행해",
    "letters": "A 조사하고 B 수정하고 C 배포해줘",
    "nominal_list": "오류 원인 조사, 코드 수정, 회귀 테스트 추가, 배포",
    "numbered_nominal_lines": "1. 원인 조사\n2. 코드 수정\n3. 테스트 추가",
    "paren_inline": "성능 개선해줘 (1) 쿼리 분석 (2) 인덱스 추가 (3) 벤치마크 작성",
    "improve_chain": "로그인 조사하고 개선하고 테스트 추가해줘",
}
SIMPLE_OR_ASK = {
    "two_verbs": "README 오타 수정하고 커밋해줘",
    "git_routine": "수정하고 커밋하고 푸시해줘",
    "status": "지금 어떻게 됐어?",
    "explain_how": "수정하고 테스트하고 배포하는 방법을 설명해줘",
    "date_not_items": "2026. 11. 28. 배포 일정 알려줘",
    "single_investigation": "이 에러 원인 조사해줘",
    "single_fix": "로그인 버그 고쳐줘",
    "inline_choices_explain": "배포 순서는 1. 빌드 2. 푸시 3. 확인 이야 설명해줘",
}


@pytest.mark.parametrize("name", sorted(SHORT_COMPOUND))
def test_short_compound_requests_are_complex(name):
    assert is_complex(SHORT_COMPOUND[name])


@pytest.mark.parametrize("name", sorted({**NOT_COMPLEX, **SIMPLE_OR_ASK}))
def test_explanations_and_simple_requests_stay_non_complex(name):
    assert not is_complex({**NOT_COMPLEX, **SIMPLE_OR_ASK}[name])


def test_inline_numbered_items_must_count_up_from_one():
    from ttakkari.orchestrator.goal_signals import extract_items
    assert [i["text"] for i in extract_items(SHORT_COMPOUND["counter_2"])] == ["오류 원인 조사", "코드 수정", "회귀 테스트 추가"]
    assert extract_items("2026. 11. 28. 배포 일정") == []


@pytest.mark.parametrize("name", ["counter_1", "counter_2", "investigate_then_fix", "comma_nouns_with_verb"])
async def test_short_compound_requests_open_a_contract_and_continue(client, auth, make_dir, name):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    run = await request(sid, SHORT_COMPOUND[name])
    state = (await get(client, h))["state"]
    assert state["status"] == "active"
    if name == "counter_2":
        assert [r["text"] for r in state["requirements"]] == ["오류 원인 조사", "코드 수정", "회귀 테스트 추가"]
    await finish(run.id, RunStatus.succeeded, result_text="일부만")
    assert len(continuations(await queued(sid))) == 1


# ---- 읽기 전용 단계·금지 조건이 섞인 복합 요청(기획 원본 요청 유형) ------------------------------------------
MIXED_COMPLEX = {
    "step_readonly": "운영 기록은 읽기 전용으로 조사하고, 원인을 고쳐서 테스트하고 배포해",
    "step_ban": "목표 계약을 수정하되 하람앱 계정은 변경하지 말고 회귀 테스트하고 배포해",
    "work_card": ("목표: 운영 기록을 읽기 전용으로 확인한 뒤 원인을 고치고 배포한다.\n완료 기준:\n1. 운영 기록은 읽기 전용으로 조사한다.\n"
                  "2. 원인을 수정한다.\n3. 회귀 테스트를 추가한다.\n4. main에 push하고 배포를 예약한다.\n범위와 금지:\n"
                  "- 하람앱 작업자의 세션·계정은 변경하지 않는다.\n- 서브 작업자를 만들지 않는다."),
    "numbered_stage": "1단계는 읽기 전용으로 조사해줘. 2단계에서 원인 수정하고 회귀 테스트 추가해서 배포해줘.",
    "ban_first": "계정은 건드리지 말고 원인 조사하고 수정해서 테스트하고 배포해",
    "ban_after": "수정하고 배포하되 프론트는 건드리지 마. 회귀 테스트도 추가해줘",
    "ban_trailing": "원인 조사하고 수정해서 테스트하고 배포해. 하람앱 세션은 건드리지 말고.",
}
MIXED_NOT_COMPLEX = {
    "whole_readonly_sentence": "이 작업은 읽기 전용이다. 원인을 분석하고 수정 방안을 작성해서 보고해줘",
    "readonly_items_with_ban": "읽기 전용 검증이다.\n1. 원인 조사\n2. 코드 수정 여부 판단\n3. 회귀 테스트 필요성 검토\n수정과 배포는 하지 않는다.",
    "ban_then_explain": "하람앱 계정은 변경하지 말고 설명만 해줘",
    "everything_banned": "수정하지 말고 배포하지 말고 테스트하지 마",
    "single_readonly_step": "운영 기록은 읽기 전용으로 조사해줘",
    "readonly_then_explain": "운영 기록을 읽기 전용으로 조사하고 원인을 설명해줘",
}


@pytest.mark.parametrize("name", sorted(MIXED_COMPLEX))
def test_read_only_step_or_single_ban_does_not_hide_a_compound_request(name):
    assert is_complex(MIXED_COMPLEX[name])


@pytest.mark.parametrize("name", sorted({**MIXED_NOT_COMPLEX, "readonly_task_card": TASK_CARD}))
def test_whole_request_read_only_and_total_bans_stay_non_complex(name):
    assert not is_complex({**MIXED_NOT_COMPLEX, "readonly_task_card": TASK_CARD}[name])


def test_scope_cleaning_keeps_the_commands_and_drops_only_the_limited_clause():
    from ttakkari.orchestrator.goal_signals import drop_negated, extract_constraints
    cleaned = drop_negated(MIXED_COMPLEX["step_ban"])
    assert "하람앱 계정" not in cleaned and "수정하되" in cleaned and "배포해" in cleaned
    cleaned = drop_negated(MIXED_COMPLEX["step_readonly"])
    assert "읽기 전용" not in cleaned and "고쳐서 테스트하고 배포해" in cleaned
    # 쉼표로 나열한 금지 목록은 문장째 뺀다.
    assert drop_negated("읽기 전용 검증이다. 코드 수정, 커밋, push, 배포 요청을 하지 않는다.") == ""
    assert extract_constraints(MIXED_COMPLEX["step_ban"]) == [MIXED_COMPLEX["step_ban"]]


@pytest.mark.parametrize("origin", [RunOrigin.user, RunOrigin.desk])
@pytest.mark.parametrize("name", ["step_readonly", "step_ban", "work_card"])
async def test_mixed_requests_open_a_contract_and_keep_the_prohibitions(client, auth, make_dir, origin, name):
    from ttakkari.orchestrator.goal_signals import DESK_HEADER
    prompt = MIXED_COMPLEX[name]
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    run = await request(sid, DESK_HEADER + "\n" + prompt if origin == RunOrigin.desk else prompt, origin=origin)
    c = await get(client, h)
    assert c["state"]["status"] == "active" and c["original_request"]["run_id"] == str(run.id)
    assert prompt in c["original_request"]["prompt"]  # 원본 전문 보존
    texts = " ".join(r["text"] for r in c["state"]["requirements"])
    if name == "step_ban":
        # 금지된 계정 변경이 "해야 할 일"로 바뀌지 않는다: 요구 문장이 금지 표현과 함께만 나온다.
        assert "하람앱 계정은 변경하지 말고" in texts
        assert any("원본 금지·제약" in d and "변경하지 말고" in d for d in c["state"]["decisions"])
    if name == "work_card":
        assert "읽기 전용으로 조사한다" in texts
        assert any("변경하지 않는다" in d for d in c["state"]["decisions"])
    # 이어가기와 프롬프트에도 금지 조건이 남는다.
    await finish(run.id, RunStatus.succeeded, result_text="일부만 함")
    follow = continuations(await queued(sid))
    assert len(follow) == 1
    if name != "step_readonly":
        assert "원본 금지·제약" in follow[0].prompt
    async with sessionmaker()() as db:
        composed = await _compose_prompt(db, await db.get(Run, follow[0].id))
    assert "원본 금지·제약" in composed if name != "step_readonly" else "읽기 전용으로 조사하고" in composed


async def test_whole_read_only_card_and_total_bans_open_no_contract_and_followups_keep_constraints(client, auth, make_dir):
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    for prompt in (TASK_CARD, MIXED_NOT_COMPLEX["whole_readonly_sentence"], MIXED_NOT_COMPLEX["ban_then_explain"],
                   MIXED_NOT_COMPLEX["everything_banned"]):
        run = await request(sid, prompt)
        await finish(run.id, RunStatus.succeeded, result_text="답변")
        assert (await get(client, h))["state"] is None and continuations(await queued(sid)) == []
    # 진행 중인 계약에 금지가 섞인 복합 요청이 오면 원문과 금지 조건이 추가 요청으로 보존된다.
    assert (await put(client, h, contract())).status_code == 200
    await request(sid, MIXED_COMPLEX["step_ban"])
    c = await get(client, h)
    assert c["additional_requests"][0]["constraints"] == [MIXED_COMPLEX["step_ban"]]
    async with sessionmaker()() as db:
        note = await work.prompt_note(db, await db.get(ChatSession, sid))
    assert "원본 금지·제약" in note and "하람앱 계정은 변경하지 말고" in note
