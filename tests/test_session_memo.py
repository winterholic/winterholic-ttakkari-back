"""컨텍스트 초기화 후 첫 Run의 이전 결정·발견 요약 재주입. 모델을 부르지 않는다."""
from datetime import UTC, datetime, timedelta

from helpers import wait_run
from test_orchestrator import _orch, _session

from ttakkari.config import get_settings
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import session_memo

NOW = datetime(2026, 11, 15, tzinfo=UTC)


def _digest(**kw):
    base = {"run_id": "ab12cd34", "status": "succeeded", "completion": None, "verification_status": None,
            "verification_note": None, "error": None, "changed": []}
    base.update(kw)
    return base


def test_build_lists_decisions_findings_and_remaining_first():
    text = session_memo.build([
        _digest(run_id="new", completion={"status": "부분", "violation": False},
                verification_note="배포는 다음 단계로 미룸", changed=["a.py", "b.py"]),
        _digest(run_id="old", status="failed", error="부팅 실패"),
    ])
    assert text.index("new") < text.index("old")  # 최신 먼저
    assert "부분" in text and "배포는 다음 단계로 미룸" in text
    assert "a.py" in text and "부팅 실패" in text
    assert len(text) <= session_memo.MAX_CHARS


def test_build_uses_at_most_three_runs():
    digests = [_digest(run_id=f"r{i}", error=f"오류{i}") for i in range(5)]
    text = session_memo.build(digests)
    assert "오류0" in text and "오류4" not in text


def test_build_is_empty_without_material():
    assert session_memo.build([]) == ""
    assert session_memo.build([_digest()]) == ""  # 상태만으로는 만들지 않는다
    assert session_memo.build([_digest(status="failed")]) == ""


def test_build_is_capped():
    one = _digest(verification_note="결정" * 1000, error="오류" * 1000,
                  changed=[f"src/ttakkari/runs/모듈{i}.py" for i in range(50)])
    text = session_memo.build([dict(one, run_id=f"r{i}") for i in range(3)])
    assert len(text) <= session_memo.MAX_CHARS
    assert "생략" in text


def test_from_run_picks_structured_fields_only():
    from types import SimpleNamespace

    run = SimpleNamespace(id="ab12cd34-0000-0000-0000-000000000000", status="succeeded",
                          usage={"completion": {"status": "완료", "violation": False},
                                 "verification_note": "기록",
                                 "input_tokens": 100, "context_handoff": {"text": "원문"}},
                          error=None, result_text="전체 응답 원문", prompt="지시 원문")
    digest = session_memo.from_run(run, ["a.py"])
    assert digest["run_id"] == "ab12cd34" and digest["changed"] == ["a.py"]
    assert digest["verification_note"] == "기록"
    assert "전체 응답 원문" not in str(digest) and "원문" not in str(digest.get("completion"))


def test_first_after_reset_detection():
    reset = NOW
    before = NOW - timedelta(minutes=5)
    after = NOW + timedelta(minutes=5)
    assert session_memo.first_after_reset(True, None, before, True)
    assert session_memo.first_after_reset(False, reset, before, True)
    assert not session_memo.first_after_reset(False, reset, after, True)  # 초기화 뒤에 이미 돌았다
    assert not session_memo.first_after_reset(False, None, before, True)
    assert not session_memo.first_after_reset(True, None, before, False)  # 이전 Run이 없다
    assert not session_memo.first_after_reset(False, None, None, False)


def _usage(run: dict) -> dict:
    return run.get("usage") or {}


async def _runs(client, auth, sid):
    return (await client.get(f"/api/sessions/{sid}/runs", headers=auth)).json()


async def test_fresh_message_reinjects_prior_summary(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_session_memo_ab_percent", 100)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", headers=h,
                             json={"title": "메모", "engine": "claude", "prompt": "write report"})).json()["worker_id"]
    first = (await _runs(client, auth, wid))[0]
    assert (await wait_run(client, auth, first["id"]))["status"] == "succeeded"
    card = {"goal": "이어서 한다.", "done_criteria": ["끝냈다."]}
    m = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h,
                          json={"card": card, "fresh_context": True})
    assert m.status_code == 202, m.text
    last = (await _runs(client, auth, wid))[-1]
    assert "[이전 결정·발견 요약" in last["prompt"]
    assert _usage(last)["session_memo_arm"] == "on"
    assert _usage(last)["session_memo_chars"] > 0


async def test_non_fresh_message_records_arm_without_memo(client, auth, make_dir, monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_session_memo_ab_percent", 100)
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    wid = (await client.post("/api/orchestrator/workers", headers=h,
                             json={"title": "메모대조", "engine": "claude", "prompt": "write report"})).json()["worker_id"]
    first = (await _runs(client, auth, wid))[0]
    assert (await wait_run(client, auth, first["id"]))["status"] == "succeeded"
    card = {"goal": "이어서 한다.", "done_criteria": ["끝냈다."]}
    m = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"card": card})
    assert m.status_code == 202, m.text
    last = (await _runs(client, auth, wid))[-1]
    assert "[이전 결정·발견 요약" not in last["prompt"]
    assert _usage(last)["session_memo_arm"] == "on"
    assert _usage(last)["session_memo_chars"] == 0


async def test_percent_zero_leaves_prompt_byte_identical(client, auth, make_dir):
    from ttakkari.runs import task_card
    from ttakkari.runs.task_card import TaskCard

    orch = await _session(client, auth, make_dir, kind="orchestrator")
    h = _orch(issue_token(orch["id"]))
    card = {"goal": "이어서 한다.", "done_criteria": ["끝냈다."]}
    wid = (await client.post("/api/orchestrator/workers", headers=h,
                             json={"title": "끔", "engine": "claude", "card": card})).json()["worker_id"]
    m = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h,
                          json={"card": card, "fresh_context": True})
    assert m.status_code == 202, m.text
    last = (await _runs(client, auth, wid))[-1]
    assert last["prompt"] == task_card.compose("", TaskCard(**card))
    assert "session_memo_arm" not in _usage(last)
