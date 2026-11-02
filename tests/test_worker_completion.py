"""작업자 완료 계약 미들웨어: 상태 줄 파싱, 보고 주석, 계약 위반 시 완료 확인 Run 한 번."""
from __future__ import annotations

import pytest
from helpers import wait_run
from test_orchestrator import _orch, _session, _wait
from ttakkari.config import get_settings
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import completion


@pytest.mark.parametrize("text,expected", [
    ("상태: 완료\n- 항목", "완료"),
    ("**상태: 부분**\n남음", "부분"),
    ("> 상태：막힘\n이유", "막힘"),
    ("서문 한 줄\n상태: 완료", "완료"),
    ("done", None),
    ("", None),
    (None, None),
    ("a\nb\nc\nd\ne\nf\n상태: 완료", None),  # 첫머리를 벗어나면 계약 이행으로 보지 않는다
    ("상태: 보류", None),
])
def test_parse_status(text, expected):
    assert completion.parse(text) == expected


def test_report_line_distinguishes_violation_and_status():
    assert "계약 위반" in completion.report_line({"status": None, "violation": True})
    assert "자동 완료 확인" in completion.report_line({"status": None, "violation": True, "closure_run_id": "x"})
    assert "상태=부분" in completion.report_line({"status": "부분", "violation": False})
    assert completion.report_line(None) is None


@pytest.fixture
def contract_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "worker_completion_contract", True)


async def _spawn(client, auth, make_dir, prompt):
    orch = await _session(client, auth, make_dir, kind="orchestrator")
    r = await client.post("/api/orchestrator/workers", headers=_orch(issue_token(orch["id"])),
                          json={"title": "계약", "prompt": prompt, "engine": "claude"})
    assert r.status_code == 201, r.text
    return orch, r.json()["worker_id"]


async def _runs(client, auth, sid):
    return (await client.get(f"/api/sessions/{sid}/runs", headers=auth)).json()


async def test_missing_status_triggers_one_closure_run(client, auth, make_dir, contract_on):
    orch, wid = await _spawn(client, auth, make_dir, "write report")
    first = (await _runs(client, auth, wid))[0]
    assert (await wait_run(client, auth, first["id"]))["status"] == "succeeded"

    async def closure_done():
        runs = await _runs(client, auth, wid)
        return runs if len(runs) == 2 and all(r["status"] == "succeeded" for r in runs) else None
    runs = await _wait(closure_done, what="완료 확인 Run")
    done = {r["id"]: r for r in runs}
    assert done[first["id"]]["usage"]["completion"]["violation"] is True
    closure = next(r for r in runs if r["id"] != first["id"])
    assert closure["prompt"].startswith("[완료 확인]") and closure["usage"]["closure"]["source_run_id"] == first["id"]
    assert completion.parse(closure["result_text"]) == "완료"

    async def reports():
        rs = [r for r in await _runs(client, auth, orch["id"]) if r["origin"] == "report"]
        return rs if len(rs) >= 2 else None
    texts = [r["prompt"] for r in await _wait(reports, what="보고 2건")]
    assert any("완료 계약 위반" in t and "자동 완료 확인" in t for t in texts)
    assert any("완료 계약: 상태=완료" in t for t in texts)
    # 완료 확인 Run 이 다시 위반해도 연쇄로 늘지 않는다.
    assert len(await _runs(client, auth, wid)) == 2


async def test_compliant_status_creates_no_closure_run(client, auth, make_dir, contract_on):
    orch, wid = await _spawn(client, auth, make_dir, "STATUS_부분 write report")
    first = (await _runs(client, auth, wid))[0]
    await wait_run(client, auth, first["id"])

    async def report():
        return next((r for r in await _runs(client, auth, orch["id"]) if r["origin"] == "report"), None)
    rep = await _wait(report, what="보고")
    assert "완료 계약: 상태=부분" in rep["prompt"]
    runs = await _runs(client, auth, wid)
    assert len(runs) == 1 and runs[0]["usage"]["completion"] == {"status": "부분", "violation": False}


async def test_contract_off_leaves_flow_unchanged(client, auth, make_dir):
    orch, wid = await _spawn(client, auth, make_dir, "write report")
    first = (await _runs(client, auth, wid))[0]
    await wait_run(client, auth, first["id"])

    async def report():
        return next((r for r in await _runs(client, auth, orch["id"]) if r["origin"] == "report"), None)
    rep = await _wait(report, what="보고")
    assert "완료 계약" not in rep["prompt"]
    assert len(await _runs(client, auth, wid)) == 1


async def test_worker_out_and_metrics_expose_completion(client, auth, make_dir, contract_on):
    orch, wid = await _spawn(client, auth, make_dir, "STATUS_막힘 write report")
    first = (await _runs(client, auth, wid))[0]
    await wait_run(client, auth, first["id"])
    h = _orch(issue_token(orch["id"]))
    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert detail["completion_status"] == "막힘"
    metrics = (await client.get("/api/orchestrator/workers/metrics", headers=h)).json()["metrics"]
    assert sum(m["contract_runs"] for m in metrics) == 1 and sum(m["blocked_runs"] for m in metrics) == 1


async def test_advisory_after_two_consecutive_unmet_runs(client, auth, make_dir, contract_on):
    orch, wid = await _spawn(client, auth, make_dir, "STATUS_부분 first")
    h = _orch(issue_token(orch["id"]))
    first = (await _runs(client, auth, wid))[0]
    await wait_run(client, auth, first["id"])
    assert (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()["advisory"] is None
    r = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"prompt": "STATUS_막힘 again"})
    assert r.status_code == 202, r.text

    async def second_done():
        runs = await _runs(client, auth, wid)
        return runs if len(runs) == 2 and all(x["status"] == "succeeded" for x in runs) else None
    await _wait(second_done, what="두 번째 Run")
    detail = (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()
    assert detail["completion_status"] == "막힘" and "연속 완료 기준 미달" in detail["advisory"]
    ok = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=h, json={"prompt": "STATUS_완료 fixed"})
    assert ok.status_code == 202

    async def third_done():
        runs = await _runs(client, auth, wid)
        return runs if len(runs) == 3 and all(x["status"] == "succeeded" for x in runs) else None
    await _wait(third_done, what="세 번째 Run")
    assert (await client.get(f"/api/orchestrator/workers/{wid}", headers=h)).json()["advisory"] is None
