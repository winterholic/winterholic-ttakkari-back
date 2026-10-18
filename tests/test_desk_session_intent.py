"""Session allocation must preserve user intent even when a desk model picks incorrectly."""
import json

import pytest
from helpers import run_events, start_run, wait_run
from test_desk import _all_done, _desk, _leads, _wait

from ttakkari.runs import desk as module


async def _send(client, auth, sid, text):
    return await wait_run(client, auth, (await start_run(client, auth, sid, text))["id"])


async def _first(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    await _send(client, auth, desk["id"], "기존 작업 DESK_DELEGATE")
    lead = (await _leads(client, auth, desk["id"]))[0]
    await _wait(lambda: _all_done(client, auth, lead["id"]))
    return desk, lead


@pytest.mark.parametrize("text", [
    "새 세션에서 따로 진행해줘 DESK_CONTINUE",
    "별도 기획 세션으로 해줘 DESK_CONTINUE",
    "기존 거 말고 새세션 만들어줘 DESK_CONTINUE",
])
async def test_explicit_new_overrides_model_reuse(client, auth, make_dir, text):
    desk, old = await _first(client, auth, make_dir)
    result = await _send(client, auth, desk["id"], text)
    leads = await _leads(client, auth, desk["id"])
    assert len(leads) == 2
    event = next(e for e in await run_events(client, auth, result["id"]) if e["type"] == "desk.delegated")
    assert event["payload"]["lead_id"] != old["id"]
    assert all(x["parent_id"] == desk["id"] for x in leads)
    old_runs = (await client.get(f"/api/sessions/{old['id']}/runs", headers=auth)).json()
    assert len(old_runs) == 1


async def test_explicit_new_bypasses_title_dedup(client, auth, make_dir):
    desk, _ = await _first(client, auth, make_dir)
    await _send(client, auth, desk["id"], "새 세션 만들어줘 DESK_DELEGATE")
    leads = await _leads(client, auth, desk["id"])
    assert [x["title"] for x in leads] == ["desk title", "desk title"]


async def test_explicit_new_overrides_model_answer(client, auth, make_dir):
    desk = await _desk(client, auth, make_dir)
    await _send(client, auth, desk["id"], "새 세션으로 조사해줘 DESK_ANSWER")
    assert len(await _leads(client, auth, desk["id"])) == 1


async def test_structured_new_bypasses_stale_id_and_dedup(client, auth, make_dir, monkeypatch):
    desk, old = await _first(client, auth, make_dir)
    async def wrong(*args, **kwargs):
        return module.Attempt(json.dumps({"action": "delegate", "reply": "넘겼어요", "lead_id": old["id"],
                                         "title": old["title"], "session_mode": "new"}), None, 1)
    monkeypatch.setattr(module, "_ask", wrong)
    await _send(client, auth, desk["id"], "다른 일 조사해줘")
    assert len(await _leads(client, auth, desk["id"])) == 2


@pytest.mark.parametrize("target_kind", ["archived", "other_desk", "worker"])
async def test_invalid_target_creates_top_level_lead(client, auth, make_dir, monkeypatch, target_kind):
    desk, old = await _first(client, auth, make_dir)
    target = old["id"]
    if target_kind == "archived":
        assert (await client.patch(f"/api/sessions/{target}", json={"archived": True}, headers=auth)).status_code == 200
    elif target_kind == "other_desk":
        _, other = await _first(client, auth, make_dir)
        target = other["id"]
    else:
        r = await client.post("/api/sessions", json={"workspace_id": desk["workspace_id"], "kind": "worker", "parent_id": old["id"]}, headers=auth)
        assert r.status_code == 201, r.text
        target = r.json()["id"]
    async def wrong(*args, **kwargs):
        return module.Attempt(json.dumps({"action": "delegate", "reply": "넘겼어요", "lead_id": target,
                                         "title": "별개 작업", "session_mode": "continue"}), None, 1)
    monkeypatch.setattr(module, "_ask", wrong)
    result = await _send(client, auth, desk["id"], "관련 작업 이어가줘")
    event = next(e for e in await run_events(client, auth, result["id"]) if e["type"] == "desk.delegated")
    sid = event["payload"]["lead_id"]
    assert sid != target and sid != old["id"]
    new = (await client.get(f"/api/sessions/{sid}", headers=auth)).json()
    assert new["parent_id"] == desk["id"] and new["kind"] == "orchestrator"
    if target_kind == "archived":
        assert (await client.get(f"/api/sessions/{target}", headers=auth)).json()["archived_at"] is not None
