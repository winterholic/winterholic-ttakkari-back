"""실제 llama.cpp 서버에 붙는 접수 종단 시험. TTAKKARI_QWEN_LIVE_URL 이 있을 때만 돈다(예: http://127.0.0.1:18080).

백엔드 접수 경로(desk.handle) → desk_qwen → 실모델 → desk.route 이벤트까지 한 번에 확인한다. CI·일반 실행에서는 건너뛴다.
"""
from __future__ import annotations

import os

import pytest
from helpers import run_events, start_run, wait_run
from test_desk import _desk, _leads

from ttakkari.config import get_settings
from ttakkari.runs import desk_qwen

LIVE = os.environ.get("TTAKKARI_QWEN_LIVE_URL")
pytestmark = pytest.mark.skipif(not LIVE, reason="TTAKKARI_QWEN_LIVE_URL 이 없으면 실모델 시험을 건너뛴다")


@pytest.fixture(autouse=True)
def _live(monkeypatch):
    monkeypatch.setattr(get_settings(), "desk_qwen_enabled", True)
    monkeypatch.setattr(get_settings(), "desk_qwen_base_url", LIVE)
    desk_qwen.reset_cooldown()
    yield
    desk_qwen.reset_cooldown()


async def _send(client, auth, make_dir, text):
    desk = await _desk(client, auth, make_dir)
    run = await wait_run(client, auth, (await start_run(client, auth, desk["id"], text))["id"], timeout=90)
    route = next(e for e in await run_events(client, auth, run["id"]) if e["type"] == "desk.route")
    return desk, run, route["payload"]


async def test_live_social_is_answered_by_qwen(client, auth, make_dir):
    desk, run, route = await _send(client, auth, make_dir, "안녕하세요 좋은 아침이에요")
    assert run["status"] == "succeeded" and run["engine"] == "qwen" and route["engine"] == "qwen"
    assert route["action"] == "answer" and route["qwen"]["kind"] == "social" and run["result_text"].strip()
    assert await _leads(client, auth, desk["id"]) == []


async def test_live_unsupported_agent_status_is_handed_over_not_invented(client, auth, make_dir):
    desk, run, route = await _send(client, auth, make_dir, "지금 일하는 에이전트가 몇 개나 돼?")
    assert run["engine"] == "qwen" and route["action"] == "delegate"
    assert route["qwen"]["abstained"] is True
    assert run["result_text"].startswith(desk_qwen.ABSTAIN_TO_DELEGATE_REPLY)
    assert len(await _leads(client, auth, desk["id"])) == 1


async def test_live_work_request_goes_to_a_planner(client, auth, make_dir):
    desk, run, route = await _send(client, auth, make_dir, "README 를 영어로 번역해줘")
    assert run["engine"] == "qwen" and route["action"] == "delegate" and route["qwen"]["kind"] == "work"
    assert len(await _leads(client, auth, desk["id"])) == 1


async def test_live_populated_context_uses_default_timeout(client, auth, make_dir, monkeypatch):
    from ttakkari.runs import desk as module
    board = [f"- lead_id=lead{i} 「작업 {i}」 상태: 완료 · 최근 결과: " + "상세 결과 " * 80 for i in range(8)]
    history = ["사용자: " + "오래된 대화 " * 100] * 5 + ["사용자: README 번역 작업"]
    async def context(*_):
        return board, history
    monkeypatch.setattr(module, "_context", context)
    _, run, route = await _send(client, auth, make_dir, "README 를 영어로 번역해줘")
    assert run["engine"] == "qwen" and route["action"] == "delegate"
    assert route["qwen"]["context_compacted"] is True
    assert route["qwen"]["context_chars"] <= 2000
