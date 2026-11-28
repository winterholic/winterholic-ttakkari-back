"""원본 항목 대조 409 를 MCP manage_work 로 확인 처리하는 경로와, 번호 제목·접수 머리글 오탐 회귀 시험."""
from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
from goal_helpers import contract, get, lead, put, raw, request
from test_goal_reliability_regressions import COMPLEX

from ttakkari.main import app
from ttakkari.orchestrator import mcp_server as ms
from ttakkari.orchestrator.goal_signals import (
    extract_items,
    request_text,
    unmapped_items,
)


@pytest.fixture(autouse=True)
def no_dispatch(monkeypatch):
    from ttakkari.runs.manager import manager

    async def _noop(*args, **kwargs):
        return None
    monkeypatch.setattr(manager, "dispatch", _noop)


async def _complete_state(client, auth, make_dir):
    """원본 항목과 어긋난 영문 계약을 done 으로 채워 complete 직전까지 만든다. (세션 id, 인증 헤더, complete state)."""
    sess, h = await lead(client, auth, make_dir)
    sid = UUID(sess["id"])
    await request(sid, COMPLEX)
    english = [{"id": k, "text": t, "status": "pending"} for k, t in
               (("A", "DB schema migration"), ("B", "API endpoints with validation"), ("C", "tests"), ("D", "push and deploy"))]
    assert (await put(client, h, contract("backend work", reqs=english))).status_code == 200
    cur = await get(client, h)
    done = [{**r, "status": "done", "evidence": "pytest -q: 58 passed; commit abc123"} for r in cur["state"]["requirements"]]
    return sid, h, cur["revision"], {**cur["state"], "requirements": done, "status": "complete", "next_action": ""}


def _via_asgi(monkeypatch, headers):
    transport = httpx.ASGITransport(app=app)

    async def call(method, path, body=None, *, prefix="/api/orchestrator", params=None, timeout=30):
        async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as c:
            r = await c.request(method, f"{prefix}{path}", json=body, params=params)
        data = r.json()
        return json.dumps(data if r.status_code < 400 else {"error": r.status_code, **data}, ensure_ascii=False)

    monkeypatch.setattr(ms, "_call", call)


async def _tool(args: dict) -> dict:
    out = await ms.server.call_tool("manage_work", args)
    return json.loads(out.content[0].text)


async def test_mcp_manage_work_confirms_original_items_after_409(client, auth, make_dir, monkeypatch):
    sid, h, rev, complete = await _complete_state(client, auth, make_dir)
    _via_asgi(monkeypatch, h)
    schema = {t.name: t for t in await ms.server.list_tools()}["manage_work"].input_schema
    assert schema["properties"]["confirm_original_items"]["default"] is False

    first = await _tool({"operation": "checkpoint", "revision": rev, "state": complete})
    assert first["error"] == 409 and "original request items look unmapped" in first["message"]
    # 안내가 실제 호출 방법과 맞는다: state 안이 아니라 최상위 인자다.
    assert "top-level manage_work argument original_item_dispositions" in first["message"]
    # confirm_original_items=true 만으로는 거부되고 계약은 바뀌지 않는다 (Track A-5).
    again = await _tool({"operation": "checkpoint", "revision": rev, "state": complete, "confirm_original_items": True})
    assert again["error"] == 409 and "confirm_original_items=true alone is not accepted" in again["message"]
    assert (await raw(sid))["state"]["status"] == "active" and not (await raw(sid)).get("original_item_dispositions")

    disps = [{"label": "1", "requirement_id": "A"}, {"label": "2", "requirement_id": "B"},
             {"label": "3", "requirement_id": "C"}, {"label": "4", "requirement_id": "D"}]
    ok = await _tool({"operation": "checkpoint", "revision": rev, "state": complete, "original_item_dispositions": disps})
    assert "error" not in ok and ok["state"]["status"] == "complete"
    saved = await raw(sid)
    assert saved["state"]["status"] == "complete" and saved["original_item_dispositions"]


async def test_confirm_flag_inside_state_is_accepted_instead_of_422(client, auth, make_dir):
    """state 안에 넣은 original_item_dispositions 도 최상위로 올려 받는다."""
    sid, h, rev, complete = await _complete_state(client, auth, make_dir)
    body = {"operation": "checkpoint", "revision": rev}
    no = await client.post("/api/orchestrator/work", headers=h,
                           json={**body, "state": {**complete, "confirm_original_items": True}})
    assert no.status_code == 409, no.text
    disps = [{"label": "1", "requirement_id": "A"}, {"label": "2", "requirement_id": "B"},
             {"label": "3", "requirement_id": "C"}, {"label": "4", "requirement_id": "D"}]
    ok = await client.post("/api/orchestrator/work", headers=h,
                           json={**body, "state": {**complete, "original_item_dispositions": disps}})
    assert ok.status_code == 200, ok.text
    saved = await raw(sid)
    assert saved["state"]["status"] == "complete" and "original_item_dispositions" not in saved["state"]
    assert saved["original_item_dispositions"]


# ---- 오탐: 번호 제목·접수 머리글 --------------------------------------------------------------------------
CARD = ("[접수에서 넘긴 사용자 요청]\n1. 작업 배경 및 목적\n- 목표 계약 신뢰성 개선이 목적이다\n- 운영 사례를 근거로 한다\n"
        "2. 요구 사항 구현\n3. Track A\n   세부 설명은 들여쓴다\n4. 회귀 테스트 작성\n")


def test_numbered_section_titles_are_not_requirement_items_but_real_items_stay():
    items = extract_items(request_text(CARD, "user"))
    texts = [i["text"] for i in items]
    assert texts == ["요구 사항 구현", "회귀 테스트 작성"], texts
    # 아래 내용이 없는 동사 없는 항목은 실제 요구일 수 있어 남는다.
    assert [i["text"] for i in extract_items("1. 로그인 화면\n2. 결제 화면 구현\n3. 배포 예약")] == ["로그인 화면", "결제 화면 구현", "배포 예약"]


def test_desk_header_is_not_a_section_even_when_origin_is_not_desk():
    # 번호가 다시 1부터 시작하면 라벨 충돌을 구역 이름으로 가른다. 접수 머리글이 그 구역 이름이 되면 안 된다.
    text = "[접수에서 넘긴 사용자 요청]\n1. 마이그레이션 작성\n2. API 구현\n1. 화면 수정\n"
    for origin in ("desk", "user"):
        labels = [i["label"] for i in extract_items(request_text(text, origin))]
        assert labels == ["1", "2", "1-2"], (origin, labels)


def test_real_missing_item_is_still_detected():
    items = extract_items(request_text(CARD, "user"))
    reqs = [{"id": "R1", "text": "요구 사항 구현", "status": "done", "evidence": "ok"}]
    assert [m["text"] for m in unmapped_items(items, reqs)] == ["회귀 테스트 작성"]
