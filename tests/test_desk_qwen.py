"""접수 1순위 로컬 Qwen(runs/desk_qwen). 가짜 llama.cpp 서버(httpx.MockTransport)로 정책 매핑·근거 검증·폴백을 확인한다."""
from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from helpers import run_events, start_run, wait_run
from test_desk import _desk, _leads

from ttakkari.config import get_settings
from ttakkari.runs import desk_qwen


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_qwen_enabled", True)
    desk_qwen.reset_cooldown()
    yield
    desk_qwen.reset_cooldown()


def _server(kind: str, evidence: str = "", lead_pick: str | None = None, reply: str = "안녕하세요!", calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["response_format"]["json_schema"]["name"]
        if calls is not None:
            calls.append((name, body))
        content = {"desk_kind": {"kind": kind, "evidence": evidence}, "desk_reply": {"reply": reply},
                   "desk_lead": {"lead_id": lead_pick}}[name]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}]})
    return handler


def _use(monkeypatch, handler):
    monkeypatch.setattr(desk_qwen, "_http_client", lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=timeout))


def test_prompts_match_the_measured_harness():
    # common30-v2 재측정(2026-10-16)에 쓴 프롬프트와 글자까지 같아야 한다. 바꾸면 다시 측정해야 한다.
    assert hashlib.sha256(desk_qwen.KIND_SYSTEM.encode()).hexdigest() == "d8326292d57c61eddf59caf5ca3ac7805de578c3e3780c59971772d4123b155c"
    assert hashlib.sha256(desk_qwen.REPLY_SYSTEM.encode()).hexdigest() == "f3987845b414c20fe08ad407ad9ce3c8bc3bf4f1a92542b7edb670f479c53e90"
    assert len(desk_qwen.FEWSHOT) == 18


def test_grounded_requires_real_quote_about_the_same_task():
    board = "- lead_id=aa11 「앱 아이콘 교체」 상태: 완료 · 작업 세션 1개(실행 중 0) · 최근 결과: 아이콘 3종을 교체하고 PR을 올렸습니다."
    hist = "(최근 대화 없음)"
    assert desk_qwen.grounded("「앱 아이콘 교체」 상태: 완료", board, hist, "앱 아이콘 작업 마무리됐어?")
    assert not desk_qwen.grounded("없음", board, hist, "앱 아이콘 작업 마무리됐어?")
    assert not desk_qwen.grounded("결제 서버 정상 가동 중", board, hist, "결제 서버는 살아 있어?")  # 원문에 없는 인용
    assert not desk_qwen.grounded("「앱 아이콘 교체」 상태: 완료", board, hist, "배포 끝났어?")  # 다른 일
    assert desk_qwen.grounded("「앱 아이콘 교체」 · 최근 결과: 아이콘 3종", board, hist, "아이콘 결과 알려줘")  # 조각 인용


async def test_decide_maps_kinds_to_actions(monkeypatch):
    _use(monkeypatch, _server("work"))
    d = await desk_qwen.decide([], [], "로그인 API에 토큰 추가해줘", [])
    assert (d.action, d.abstained, d.lead_id, d.calls) == ("delegate", False, None, 1)
    _use(monkeypatch, _server("social"))
    d = await desk_qwen.decide([], [], "고마워", [])
    assert (d.action, d.reply, d.calls) == ("answer", "안녕하세요!", 2)
    _use(monkeypatch, _server("status", evidence="없음"))
    d = await desk_qwen.decide([], [], "배포 끝났어?", [])
    assert (d.action, d.abstained, d.grounded) == ("delegate", True, False)
    assert d.reply == desk_qwen.ABSTAIN_TO_DELEGATE_REPLY
    _use(monkeypatch, _server("unclear"))
    assert (await desk_qwen.decide([], [], "음...", [])).abstained


async def test_status_with_real_evidence_answers_and_lead_is_limited_to_board(monkeypatch):
    board = ["- lead_id=aa11 「앱 아이콘 교체」 상태: 완료 · 작업 세션 1개(실행 중 0)"]
    _use(monkeypatch, _server("status", evidence="「앱 아이콘 교체」 상태: 완료", reply="네, 완료됐어요."))
    d = await desk_qwen.decide(board, [], "앱 아이콘 작업 끝났어?", ["aa11"])
    assert (d.action, d.reply, d.grounded) == ("answer", "네, 완료됐어요.", True)
    calls: list = []
    _use(monkeypatch, _server("work", lead_pick="aa11", calls=calls))
    d = await desk_qwen.decide(board, [], "아이콘 색 바꿔줘", ["aa11"])
    assert d.lead_id == "aa11"
    lead_call = next(b for n, b in calls if n == "desk_lead")
    assert lead_call["response_format"]["json_schema"]["schema"]["properties"]["lead_id"]["enum"] == [None, "aa11"]


async def test_failures_raise_and_cool_down(monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    _use(monkeypatch, boom)
    with pytest.raises(desk_qwen.QwenUnavailable):
        await desk_qwen.decide([], [], "안녕", [])
    assert not desk_qwen.available()  # 실패 직후에는 건너뛴다
    with pytest.raises(desk_qwen.QwenUnavailable) as e:
        await desk_qwen.decide([], [], "안녕", [])
    assert e.value.reason == "cooldown"


async def test_too_long_prompt_skips_without_cooldown(monkeypatch):
    _use(monkeypatch, _server("social"))
    with pytest.raises(desk_qwen.QwenUnavailable) as e:
        await desk_qwen.decide(["x" * 5000], [], "안녕", [])
    assert e.value.reason == "prompt_too_long" and desk_qwen.available()


async def test_desk_uses_qwen_first_and_creates_lead(client, auth, make_dir, monkeypatch):
    _use(monkeypatch, _server("work"))
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "다크모드 고쳐줘"))["id"])
    assert run["status"] == "succeeded" and run["engine"] == "qwen" and run["model"] == "qwen3.5-4b-q4_k_m"
    assert run["result_text"].startswith(desk_qwen.DELEGATE_REPLY)
    route = next(e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.route")
    assert route["payload"]["engine"] == "qwen" and route["payload"]["qwen"]["kind"] == "work"
    leads = await _leads(client, auth, desk["id"])
    assert len(leads) == 1 and leads[0]["title"] == "다크모드 고쳐줘"


async def test_desk_answers_social_with_qwen(client, auth, make_dir, monkeypatch):
    _use(monkeypatch, _server("social", reply="천만에요!"))
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "고마워"))["id"])
    assert run["result_text"] == "천만에요!" and run["engine"] == "qwen"
    assert await _leads(client, auth, desk["id"]) == []


async def test_desk_falls_back_to_luna_when_qwen_is_down(client, auth, make_dir, monkeypatch):
    def boom(request):
        raise httpx.ConnectError("refused")
    _use(monkeypatch, boom)
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], "안녕 DESK_ANSWER"))["id"])
    assert run["status"] == "succeeded" and run["engine"] == "codex" and run["result_text"] == "desk answer by codex"
    types = [e["type"] for e in await run_events(client, auth, run["id"])]
    assert "desk.retry" in types and "desk.route" in types
