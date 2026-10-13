"""접수 분류 판정·평가 문항·재생 평가(ADR 0013 '판정')."""
from __future__ import annotations

import asyncio
import json
import os
import sys

from helpers import run_events, start_run, wait_run
from test_desk import _desk, _leads

from ttakkari.db import sessionmaker
from ttakkari.runs import desk_eval


async def _desk_run(client, auth, desk_id: str, prompt: str) -> dict:
    return await wait_run(client, auth, (await start_run(client, auth, desk_id, prompt))["id"])


async def _judge(client, auth, run_id: str, verdict: str, **extra):
    return await client.post(f"/api/runs/{run_id}/desk-feedback", json={"verdict": verdict, **extra}, headers=auth)


async def test_feedback_rejected_for_non_desk_runs(client, auth, new_session, make_dir):
    _, chat = await new_session()
    run = await wait_run(client, auth, (await start_run(client, auth, chat["id"], "hello"))["id"])
    r = await _judge(client, auth, run["id"], "ok")
    assert r.status_code == 422, r.text

    # 접수 아래 기획 세션의 Run(origin=desk)도, 접수 세션의 보고 Run 도 판정 대상이 아니다.
    desk = await _desk(client, auth, make_dir)
    await _desk_run(client, auth, desk["id"], "고쳐줘 DESK_DELEGATE")
    lead = (await _leads(client, auth, desk["id"]))[0]
    lead_run = (await client.get(f"/api/sessions/{lead['id']}/runs", headers=auth)).json()[0]
    assert (await _judge(client, auth, lead_run["id"], "ok")).status_code == 422
    assert (await _judge(client, auth, "00000000-0000-0000-0000-000000000000", "ok")).status_code == 404
    assert (await client.get("/api/desk/feedback", headers=auth)).json() == []


async def test_feedback_saved_overwritten_and_shown_on_run(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    run = await _desk_run(client, auth, desk["id"], "오늘 날씨 DESK_ANSWER")
    assert run["desk_feedback"] is None

    r = await _judge(client, auth, run["id"], "should_delegate", note="일이다")
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["verdict"], body["decided"], body["expected"], body["note"]) == ("should_delegate", "answer", "delegate", "일이다")
    assert body["message"] == "오늘 날씨 DESK_ANSWER" and body["lead_id"] is None
    # redo 없이는 넘기지 않는다
    assert await _leads(client, auth, desk["id"]) == []

    r = await _judge(client, auth, run["id"], "ok")
    assert r.status_code == 200 and r.json()["expected"] == "answer" and r.json()["note"] is None
    rows = (await client.get("/api/desk/feedback", headers=auth)).json()
    assert len(rows) == 1 and rows[0]["verdict"] == "ok" and rows[0]["created_at"] == body["created_at"]

    fb = (await client.get(f"/api/runs/{run['id']}", headers=auth)).json()["desk_feedback"]
    assert fb["verdict"] == "ok" and fb["decided"] == "answer"
    listed = (await client.get(f"/api/sessions/{desk['id']}/runs", headers=auth)).json()
    assert next(x for x in listed if x["id"] == run["id"])["desk_feedback"]["verdict"] == "ok"

    # 이미 바로 답한 Run 에 "바로 답했어야 해"는 말이 안 된다
    assert (await _judge(client, auth, run["id"], "should_answer")).status_code == 409
    assert (await client.post(f"/api/runs/{run['id']}/desk-feedback", json={"verdict": "nope"},
                              headers=auth)).status_code == 422


async def test_redo_delegates_answered_message_to_new_lead_once(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    # 기존 기획 세션이 있어도 redo 는 새 기획 세션을 만든다(이어 맡을 판단이 없다).
    await _desk_run(client, auth, desk["id"], "다른 일 DESK_DELEGATE")
    before = {x["id"] for x in await _leads(client, auth, desk["id"])}
    run = await _desk_run(client, auth, desk["id"], "빌드 깨진 거 봐줘 DESK_ANSWER")

    r = await _judge(client, auth, run["id"], "should_delegate", redo=True)
    assert r.status_code == 200, r.text
    lead_id = r.json()["lead_id"]
    leads = await _leads(client, auth, desk["id"])
    assert lead_id not in before and lead_id in {x["id"] for x in leads} and len(leads) == len(before) + 1
    lead_run = (await client.get(f"/api/sessions/{lead_id}/runs", headers=auth)).json()[0]
    assert lead_run["origin"] == "desk"
    assert lead_run["prompt"].startswith("[접수에서 넘긴 사용자 요청]\n빌드 깨진 거 봐줘 DESK_ANSWER")
    ev = [e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.delegated"]
    assert len(ev) == 1 and ev[0]["payload"]["redo"] is True and ev[0]["payload"]["lead_id"] == lead_id

    again = await _judge(client, auth, run["id"], "should_delegate", redo=True)
    assert again.status_code == 200 and again.json()["lead_id"] == lead_id
    assert len(await _leads(client, auth, desk["id"])) == len(before) + 1


async def _seed(client, auth, make_dir) -> None:
    desk = await _desk(client, auth, make_dir)
    ok = await _desk_run(client, auth, desk["id"], "안녕 DESK_ANSWER")
    wrong = await _desk_run(client, auth, desk["id"], "그냥 질문인데 DESK_DELEGATE")
    assert (await _judge(client, auth, ok["id"], "ok")).status_code == 200
    assert (await _judge(client, auth, wrong["id"], "should_answer")).status_code == 200


async def test_eval_set_contains_message_and_expected(client, auth, make_dir):
    await _seed(client, auth, make_dir)
    items = (await client.get("/api/desk/eval-set", headers=auth)).json()
    # 최근 판정부터
    assert items == [{"message": "그냥 질문인데 DESK_DELEGATE", "expected": "answer"},
                     {"message": "안녕 DESK_ANSWER", "expected": "answer"}]
    r = await client.get("/api/desk/eval-set", params={"format": "jsonl", "limit": 1}, headers=auth)
    assert r.headers["content-type"].startswith("application/x-ndjson")
    assert [json.loads(x) for x in r.text.splitlines()] == items[:1]


async def test_desk_eval_replays_with_fake_engines(client, auth, make_dir):
    await _seed(client, auth, make_dir)
    async with sessionmaker()() as db:
        report = await desk_eval.run_eval(db)
        claude_only = await desk_eval.run_eval(db, limit=1, engine="claude")
    # 가짜 엔진은 DESK_DELEGATE 면 넘긴다 → 판정(answer)과 어긋나는 1건이 오답
    assert report.correct == 1 and len(report.cases) == 2
    assert [c.message for c in report.wrong] == ["그냥 질문인데 DESK_DELEGATE"] and report.wrong[0].got == "delegate"
    assert report.median_ms is not None
    assert len(claude_only.cases) == 1 and claude_only.cases[0].account.startswith("claude")
    text = desk_eval.format_report(report)
    assert text.startswith("정확도 1/2 (50%)") and "기대 answer / 결과 delegate" in text

    # 실제 CLI 진입점(별도 프로세스, 같은 테스트 DB·가짜 엔진)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "ttakkari.cli", "desk-eval", "--limit", "2", env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, err.decode()
    assert out.decode().startswith("정확도 1/2 (50%) · 지연 중앙값")
