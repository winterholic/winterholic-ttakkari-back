"""평시 병렬-fit Qwen 배정, 1슬롯+대기열, 기획 최하위를 고정한다.

모델 없이 고정 가짜 qwen-worker와 fake 클라우드 에이전트로만 돈다.
"""
import json
import shlex
import uuid
from pathlib import Path

import pytest
from conftest import agent_calls
from helpers import run_events, wait_event, wait_run, wait_status

from ttakkari.db import sessionmaker
from ttakkari.models import Run
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs import profiles

FIT = {"needs_mcp": False, "narrow_scope": True, "verifiable": True}


def _write_qwen_worker(tmp_path: Path, *, name: str = "qwen-worker", run_result=None,
                       exit_code: int = 0, sleep_s: float = 0) -> Path:
    worker = tmp_path / f"bin-{name}/{name}"
    worker.parent.mkdir(parents=True, exist_ok=True)
    if run_result is None:
        run_result = {"schema": 1, "run_id": "fixture", "status": "success", "exit_code": 0,
                      "session_id": "qwen-session", "result_text": "fixture complete",
                      "files": {"added": [], "modified": [], "deleted": []},
                      "metrics": {"host_peaks": {"server_rss_max_bytes": 1024}, "elapsed_seconds": 0.1}}
    result_line = run_result if isinstance(run_result, str) else json.dumps(run_result)
    script = (
        "#!/bin/sh\n"
        "if [ \"$1\" = doctor ]; then "
        "printf '%s\\n' '{\"contract\":\"qwen-local-readiness-v1\",\"checks\":{\"seatbelt\":true,\"hostmon\":true,\"fixture\":true}}'; exit 0; fi\n"
        + (f"sleep {sleep_s}\n" if sleep_s else "")
        + f"printf '%s\\n' {shlex.quote(result_line)}\n"
        + f"exit {exit_code}\n"
    )
    worker.write_text(script)
    worker.chmod(0o755)
    return worker


def _ready_qwen(monkeypatch, worker: Path):
    from ttakkari.runs import manager as run_manager_mod
    from ttakkari.runs.manager import QwenLocalAdapter
    monkeypatch.setattr(run_manager_mod, "QWEN_LOCAL_ADAPTER",
                        QwenLocalAdapter(worker_path=worker, enabled=True))


async def _orchestrator(client, auth, make_dir, *, pool: str = "personal"):
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator",
                                                       "pool": pool}, headers=auth)).json()
    return parent, {"Authorization": f"Bearer {issue_token(parent['id'])}"}


async def _spawn(client, orch_auth, *, prompt="parallel fixture task", **kw) -> dict:
    body = {"title": "Parallel Qwen fixture", "prompt": prompt, **kw}
    created = await client.post("/api/orchestrator/workers", headers=orch_auth, json=body)
    assert created.status_code == 201, created.text
    return created.json()


def _routing_receipts(events: list[dict]) -> list[dict]:
    return [e["payload"] for e in events if e["type"] == "worker.routing"]


async def _cooldown_all_cloud(client, auth) -> None:
    existing = (await client.get("/api/accounts", headers=auth)).json()
    ids = [a["id"] for a in existing]
    for name, engine in (("c", "claude"), ("x", "codex"), ("agy", "antigravity")):
        created = await client.post("/api/accounts", json={"name": f"pq-{name}", "engine": engine},
                                    headers=auth)
        assert created.status_code == 201, created.text
        ids.append(created.json()["id"])
    for aid in ids:
        profiles.cool_down(uuid.UUID(aid), 3600)


@pytest.mark.asyncio
async def test_parallel_fit_worker_routes_to_idle_qwen_with_parallel_trigger(
        client, auth, make_dir, tmp_path, monkeypatch):
    """병렬+선언 완료 작업자는 평시에도 Qwen으로 간다(trigger parallel, 검증 필수)."""
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    first = await _spawn(client, orch_auth, prompt="SLEEP")
    await wait_status(client, auth, first["active_run_id"], "running", timeout=30.0)
    try:
        second = await _spawn(client, orch_auth, task_type="general", qwen_fit=FIT)
        run = await wait_run(client, auth, second["active_run_id"], timeout=30.0)
        assert run["status"] == "succeeded", run
        assert run["engine"] == "qwen-local", run
        receipts = _routing_receipts(await run_events(client, auth, run["id"]))
        assert receipts, "worker.routing receipt missing"
        full = [r for r in receipts if r.get("selected") is not None]
        assert full and all(r["trigger"] == "parallel" for r in full), receipts
        assert all(r["mode"] == "worker-parallel" for r in full), receipts
        assert all(r["verification_required"] is True for r in full), receipts
        assert full[0]["recommended_verifier"]["task_type"] == "verification"
        qwen = full[0]["qwen"]
        assert qwen["decision"] == "selected" and qwen["fit"] is True, qwen
        assert qwen["parallel_siblings"] >= 1, qwen
        async with sessionmaker()() as db:
            row = await db.get(Run, uuid.UUID(run["id"]))
            assert row.usage["routing_trigger"] == "parallel"
            assert row.usage["qwen_attempted"] is True
            assert row.usage["verification_status"] == "pending"
    finally:
        await client.post(f"/api/orchestrator/workers/{first['worker_id']}/stop", headers=orch_auth)


@pytest.mark.asyncio
async def test_lone_fit_worker_stays_on_cloud_with_single_run_reason(
        client, auth, make_dir, tmp_path, monkeypatch):
    """단일 작업은 fit 선언이 있어도 클라우드로 가고 사유가 영수증에 남는다."""
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    spawned = await _spawn(client, orch_auth, task_type="general", qwen_fit=FIT)
    run = await wait_run(client, auth, spawned["active_run_id"], timeout=30.0)
    assert run["status"] == "succeeded", run
    assert run["engine"] != "qwen-local", run
    receipts = _routing_receipts(await run_events(client, auth, run["id"]))
    full = [r for r in receipts if r.get("selected") is not None]
    assert full, receipts
    assert full[0]["qwen"]["decision"] == "cloud", full[0]["qwen"]
    assert "single_run" in full[0]["qwen"]["reasons"], full[0]["qwen"]


@pytest.mark.asyncio
async def test_undeclared_fit_never_routes_to_qwen(client, auth, make_dir, tmp_path, monkeypatch):
    """병렬이어도 fit 미선언이면 Qwen 후보가 아니다(fail closed)."""
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    first = await _spawn(client, orch_auth, prompt="SLEEP")
    await wait_status(client, auth, first["active_run_id"], "running", timeout=30.0)
    try:
        second = await _spawn(client, orch_auth, task_type="routine")
        run = await wait_run(client, auth, second["active_run_id"], timeout=30.0)
        assert run["status"] == "succeeded", run
        assert run["engine"] != "qwen-local", run
        receipts = _routing_receipts(await run_events(client, auth, run["id"]))
        full = [r for r in receipts if r.get("selected") is not None]
        assert full, receipts
        reasons = full[0]["qwen"]["reasons"]
        assert "mcp_required_or_unknown" in reasons, reasons
    finally:
        await client.post(f"/api/orchestrator/workers/{first['worker_id']}/stop", headers=orch_auth)


@pytest.mark.asyncio
async def test_busy_slot_sends_parallel_fit_worker_to_cloud_with_wait_estimate(
        client, auth, make_dir, tmp_path, monkeypatch):
    """슬롯 사용 중이면 급한 병렬 작업은 클라우드로 가고 대기 추정치가 남는다."""
    from ttakkari.config import get_settings
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path, name="hang", sleep_s=30))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    holder = await _spawn(client, orch_auth, prompt="hold the qwen slot", engine="qwen-local",
                          task_type="general")
    await wait_status(client, auth, holder["active_run_id"], "running", timeout=30.0)
    try:
        status = (await client.get("/api/orchestrator/accounts/usage", headers=orch_auth)).json()
        qwen = status["qwen_local"]
        assert qwen["available"] is True and qwen["busy"] is True, qwen
        assert qwen["current"]["run_id"] == holder["active_run_id"], qwen
        assert qwen["slots"] == 1 and qwen["queue_length"] == 0, qwen
        second = await _spawn(client, orch_auth, task_type="general", qwen_fit=FIT)
        run = await wait_run(client, auth, second["active_run_id"], timeout=30.0)
        assert run["status"] == "succeeded", run
        assert run["engine"] != "qwen-local", run
        receipts = _routing_receipts(await run_events(client, auth, run["id"]))
        full = [r for r in receipts if r.get("selected") is not None]
        assert full, receipts
        assert full[0]["qwen"]["decision"] == "cloud", full[0]["qwen"]
        assert "qwen_busy" in full[0]["qwen"]["reasons"], full[0]["qwen"]
        assert "estimated_wait_s" in full[0]["qwen"], full[0]["qwen"]
    finally:
        await client.post(f"/api/orchestrator/workers/{holder['worker_id']}/stop", headers=orch_auth)
    assert get_settings().qwen_parallel_routing is True


@pytest.mark.asyncio
async def test_explicit_qwen_queues_behind_busy_slot_and_runs_sequentially(
        client, auth, make_dir, tmp_path, monkeypatch):
    """명시 지정 Qwen은 슬롯이 차면 실패가 아니라 대기열에서 순서대로 돈다."""
    from ttakkari.config import get_settings
    monkeypatch.setattr(get_settings(), "qwen_queue_retry_seconds", 2)
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path, name="slow", sleep_s=5))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    first = await _spawn(client, orch_auth, prompt="first qwen", engine="qwen-local",
                         task_type="general")
    await wait_status(client, auth, first["active_run_id"], "running", timeout=30.0)
    second = await _spawn(client, orch_auth, prompt="second qwen", engine="qwen-local",
                          task_type="general")
    queued = await wait_event(client, auth, second["active_run_id"], "qwen.queued", timeout=30.0)
    assert queued["payload"]["position"] == 1, queued["payload"]
    first_run = await wait_run(client, auth, first["active_run_id"], timeout=30.0)
    second_run = await wait_run(client, auth, second["active_run_id"], timeout=60.0)
    assert first_run["status"] == "succeeded", first_run
    assert second_run["status"] == "succeeded", second_run
    assert second_run["engine"] == "qwen-local", second_run
    async with sessionmaker()() as db:
        a = await db.get(Run, uuid.UUID(first_run["id"]))
        b = await db.get(Run, uuid.UUID(second_run["id"]))
        assert a.finished_at is not None and b.started_at is not None
        assert b.started_at >= a.finished_at, "qwen runs overlapped"


@pytest.mark.asyncio
async def test_emergency_stays_on_cloud_wait_when_qwen_busy(
        client, auth, make_dir, tmp_path, monkeypatch):
    """전 클라우드 한도라도 슬롯이 차면 비상은 타지 않고 대기 사유에 qwen_busy가 남는다."""
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path, name="hold", sleep_s=30))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    holder = await _spawn(client, orch_auth, prompt="hold the slot", engine="qwen-local",
                          task_type="general")
    await wait_status(client, auth, holder["active_run_id"], "running", timeout=30.0)
    await _cooldown_all_cloud(client, auth)
    spawned = await _spawn(client, orch_auth, task_type="routine")
    try:
        waiting = await wait_event(client, auth, spawned["active_run_id"], "run.waiting", timeout=30.0)
        assert waiting["payload"]["qwen_local"] == {"ready": False, "reason": "qwen_busy"}, waiting["payload"]
        run = (await client.get(f"/api/runs/{spawned['active_run_id']}", headers=auth)).json()
        assert run["status"] == "queued", run
    finally:
        await client.post(f"/api/runs/{spawned['active_run_id']}/cancel", headers=auth)
        await client.post(f"/api/orchestrator/workers/{holder['worker_id']}/stop", headers=orch_auth)


@pytest.mark.asyncio
async def test_parallel_qwen_failure_hands_progress_and_files_to_cloud(
        client, auth, make_dir, tmp_path, monkeypatch):
    """평시 Qwen 실패는 변경내역+실행기록을 싣고 클라우드로 인계되며 한 번만 시도한다."""
    failing = {"schema": 1, "run_id": "fixture", "status": "error", "exit_code": 1,
               "session_id": "qwen-session", "result_text": "partial work",
               "files": {"added": [], "modified": ["/tmp/parallel-qwen/touched.py"], "deleted": []},
               "error": {"message": "synthetic qwen failure", "kind": "test"}}
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path, run_result=failing, exit_code=1))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    first = await _spawn(client, orch_auth, prompt="SLEEP")
    await wait_status(client, auth, first["active_run_id"], "running", timeout=30.0)
    before = len(agent_calls())
    try:
        second = await _spawn(client, orch_auth, task_type="general", qwen_fit=FIT)
        run = await wait_run(client, auth, second["active_run_id"], timeout=30.0)
        assert run["status"] == "succeeded", run
        assert run["engine"] != "qwen-local", run
        events = await run_events(client, auth, run["id"])
        fallbacks = [e["payload"] for e in events if e["type"] == "engine.fallback"]
        assert [f["reason"] for f in fallbacks] == ["qwen_failed"], fallbacks
        receipts = _routing_receipts(events)
        assert receipts[0]["trigger"] == "parallel", receipts
        cloud_argv = [" ".join(c.get("argv", [])) for c in agent_calls()[before:]
                      if c["engine"] == run["engine"]]
        assert cloud_argv, "cloud handoff never launched"
        assert "Previous account handoff" in cloud_argv[0], cloud_argv[0][-2000:]
        assert "touched.py" in cloud_argv[0], cloud_argv[0][-2000:]
        async with sessionmaker()() as db:
            row = await db.get(Run, uuid.UUID(run["id"]))
            assert row.usage["qwen_attempted"] is True
            assert row.usage["qwen_cloud_handoff"] is True
            assert row.usage["routing_trigger"] == "parallel"
        status = (await client.get("/api/orchestrator/accounts/usage", headers=orch_auth)).json()
        recent = status["qwen_local"]["recent"]
        assert recent["qwen_runs"] >= 1 and recent["cloud_handoffs"] >= 1, recent
    finally:
        await client.post(f"/api/orchestrator/workers/{first['worker_id']}/stop", headers=orch_auth)


@pytest.mark.asyncio
async def test_orchestrator_emergency_receipt_marks_qwen_selected_not_parallel(
        client, auth, make_dir, tmp_path, monkeypatch):
    """기획은 비상 경로로만 Qwen에 가고 영수증에 선택 사실이 남는다."""
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path))
    await _cooldown_all_cloud(client, auth)
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)},
                            headers=auth)).json()
    sess = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"},
                              headers=auth)).json()
    started = await client.post(f"/api/sessions/{sess['id']}/runs", json={"prompt": "plan under limits"},
                                headers=auth)
    assert started.status_code == 202, started.text
    run = await wait_run(client, auth, started.json()["id"], timeout=30.0)
    assert run["status"] == "succeeded", run
    assert run["engine"] == "qwen-local", run
    receipts = _routing_receipts(await run_events(client, auth, run["id"]))
    full = [r for r in receipts if r.get("selected") is not None]
    assert full and all(r["trigger"] == "emergency" for r in full), receipts
    assert full[0]["mode"] == "orchestrator-degraded", full[0]
    assert full[0]["qwen"]["decision"] == "selected", full[0]["qwen"]
    assert full[0]["qwen"]["explicit"] is False, full[0]["qwen"]


@pytest.mark.asyncio
async def test_qwen_local_status_visible_for_personal_and_hidden_for_work(
        client, auth, make_dir):
    """account_usage.qwen_local은 개인 범위에만 슬롯 상태를, 회사 범위에는 제외를 내놓는다."""
    _, personal_auth = await _orchestrator(client, auth, make_dir, pool="personal")
    personal = (await client.get("/api/orchestrator/accounts/usage", headers=personal_auth)).json()
    qwen = personal["qwen_local"]
    assert qwen["available"] is True, qwen
    assert qwen["slots"] == 1 and qwen["busy"] is False, qwen
    assert qwen["queue_length"] == 0 and qwen["queued"] == [], qwen
    assert qwen["current"] is None, qwen
    assert qwen["policy"]["orchestrator"] == "emergency_only_lowest_priority", qwen["policy"]
    assert set(qwen["recent"]) >= {"qwen_runs", "verified_passed", "cloud_handoffs"}, qwen["recent"]
    _, work_auth = await _orchestrator(client, auth, make_dir, pool="work")
    work = (await client.get("/api/orchestrator/accounts/usage", headers=work_auth)).json()
    assert work["qwen_local"] == {"available": False, "reason": "company_scope_excluded"}, work["qwen_local"]


@pytest.mark.asyncio
async def test_parallel_routing_flag_off_keeps_cloud_with_reason(
        client, auth, make_dir, tmp_path, monkeypatch):
    """qwen_parallel_routing을 끄면 병렬-fit도 클라우드로 가고 사유가 남는다."""
    from ttakkari.config import get_settings
    monkeypatch.setattr(get_settings(), "qwen_parallel_routing", False)
    _ready_qwen(monkeypatch, _write_qwen_worker(tmp_path))
    _, orch_auth = await _orchestrator(client, auth, make_dir)
    first = await _spawn(client, orch_auth, prompt="SLEEP")
    await wait_status(client, auth, first["active_run_id"], "running", timeout=30.0)
    try:
        second = await _spawn(client, orch_auth, task_type="general", qwen_fit=FIT)
        run = await wait_run(client, auth, second["active_run_id"], timeout=30.0)
        assert run["status"] == "succeeded", run
        assert run["engine"] != "qwen-local", run
        receipts = _routing_receipts(await run_events(client, auth, run["id"]))
        full = [r for r in receipts if r.get("selected") is not None]
        assert full, receipts
        assert "parallel_routing_disabled" in full[0]["qwen"]["reasons"], full[0]["qwen"]
    finally:
        await client.post(f"/api/orchestrator/workers/{first['worker_id']}/stop", headers=orch_auth)
