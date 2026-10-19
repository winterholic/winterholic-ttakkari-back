import asyncio
import json
import shlex
import uuid
from pathlib import Path

import pytest
from helpers import run_events, wait_run

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import issue_token
from ttakkari.runs.manager import QwenLocalAdapter


@pytest.fixture(params=[False, True], ids=["legacy", "generation"])
async def qwen_generation(request, tmp_path, monkeypatch):
    if not request.param:
        yield
        return
    from ttakkari import generation

    runtime = generation.GenerationRuntime(tmp_path, revision="a" * 40, api_url="http://127.0.0.1:18791")
    await runtime.register()
    await generation.activate(runtime.id, expected=None)
    monkeypatch.setattr(generation, "current", runtime)
    try:
        yield
    finally:
        await runtime.close()


def _write_worker(tmp_path: Path, *, run_result: str | None = None, exit_code: int = 0,
                  hang: bool = False) -> tuple[Path, Path]:
    worker = tmp_path / "bin/qwen-worker"
    capture = tmp_path / "args.txt"
    worker.parent.mkdir()
    script = (
        "#!/bin/sh\n"
        "if [ \"$1\" = doctor ]; then "
        "printf '%s\\n' '{\"contract\":\"qwen-local-readiness-v1\",\"checks\":{\"seatbelt\":true,\"hostmon\":true,\"fixture\":true}}'; exit 0; fi\n"
        f"printf '%s\\n' \"$@\" > '{capture}'\n"
    )
    if run_result is None:
        run_result = json.dumps({"schema": 1, "run_id": "fixture", "status": "success", "exit_code": 0,
                                 "session_id": "qwen-session", "result_text": "fixture complete",
                                 "files": {"added": [], "modified": [], "deleted": []},
                                 "metrics": {"host_peaks": {"server_rss_max_bytes": 1024}, "elapsed_seconds": 0.1}})
    if hang:
        script += "sleep 30\n"
    if run_result:
        script += f"printf '%s\\n' {shlex.quote(run_result)}\n"
    script += f"printf '%s\\n' '[qwen-worker] fixture stderr event' >&2\nexit {exit_code}\n"
    worker.write_text(script)
    worker.chmod(0o755)
    return worker, capture


@pytest.mark.asyncio
async def test_explicit_qwen_run_records_local_scope_streams_result_and_resumes(
    client, auth, make_dir, tmp_path, monkeypatch, qwen_generation,
):
    from ttakkari.runs import manager as run_manager

    worker_path, capture = _write_worker(tmp_path)
    monkeypatch.setattr(run_manager, "QWEN_LOCAL_ADAPTER", QwenLocalAdapter(worker_path=worker_path, enabled=True))
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)).json()
    orch_auth = {"Authorization": f"Bearer {issue_token(parent['id'])}"}
    created = await client.post("/api/orchestrator/workers", headers=orch_auth, json={
        "title": "Local Qwen fixture", "prompt": "fixture task", "engine": "qwen-local", "task_type": "general",
    })
    assert created.status_code == 201, created.text
    wid = created.json()["worker_id"]
    run = await wait_run(client, auth, created.json()["active_run_id"])
    assert (run["status"], run["engine"], run["model"]) == ("succeeded", "qwen-local", "Qwen3.8-Flash-Next-Q2_K")
    events = await run_events(client, auth, run["id"])
    receipt = next(e["payload"] for e in events if e["type"] == "worker.routing")
    assert receipt["selected"]["engine"] == "qwen-local"
    assert receipt["scope"]["account_scope"] == "personal"
    assert receipt["quota"] is None
    message = next(e["payload"] for e in events if e["type"] == "message")
    assert message["model"] == "Qwen3.8-Flash-Next-Q2_K"
    assert message["local_resources"] == {"server_rss_max_bytes": 1024}
    assert any(e["type"] == "log" and e["payload"].get("stream") == "stderr" for e in events)

    async with sessionmaker()() as db:
        session = await db.get(ChatSession, uuid.UUID(wid))
        assert any(v == "qwen-session" for v in (session.engine_sessions or {}).values())
    follow = await client.post(f"/api/orchestrator/workers/{wid}/messages", headers=orch_auth, json={"prompt": "continue fixture"})
    assert follow.status_code == 202, follow.text
    runs = (await client.get(f"/api/sessions/{wid}/runs", headers=auth)).json()
    resumed = await wait_run(client, auth, runs[-1]["id"])
    assert resumed["status"] == "succeeded"
    args = capture.read_text().splitlines()
    assert "--resume" in args and args[args.index("--resume") + 1] == "qwen-session"
    assert "--workspace" in args and args[args.index("--workspace") + 1] == str(root)
    task_file = Path(args[args.index("--task-file") + 1])
    assert not task_file.exists()


@pytest.mark.asyncio
async def test_qwen_cancel_terminates_child_and_removes_task_input(client, auth, make_dir, tmp_path, monkeypatch):
    from ttakkari.runs import manager as run_manager

    worker_path, capture = _write_worker(tmp_path, hang=True)
    monkeypatch.setattr(run_manager, "QWEN_LOCAL_ADAPTER", QwenLocalAdapter(worker_path=worker_path, enabled=True))
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)).json()
    orch_auth = {"Authorization": f"Bearer {issue_token(parent['id'])}"}
    created = await client.post("/api/orchestrator/workers", headers=orch_auth, json={
        "title": "Qwen cancellation fixture", "prompt": "wait until canceled", "engine": "qwen-local",
        "task_type": "general",
    })
    assert created.status_code == 201, created.text
    deadline = asyncio.get_running_loop().time() + 5
    while not capture.exists() and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.02)
    assert capture.exists(), "the fixed qwen-worker run command did not start"
    stopped = await client.post(f"/api/orchestrator/workers/{created.json()['worker_id']}/stop", headers=orch_auth)
    assert stopped.status_code == 200, stopped.text
    run = await wait_run(client, auth, created.json()["active_run_id"])
    assert run["status"] == "cancelled"
    args = capture.read_text().splitlines()
    task_file = Path(args[args.index("--task-file") + 1])
    assert not task_file.exists()


@pytest.mark.parametrize("mode,result,rc", [
    ("partial", {"schema": 1, "status": "partial", "exit_code": 1, "result_text": "partial"}, 1),
    ("blocked", {"schema": 1, "status": "blocked", "exit_code": 2, "error": {"kind": "guard", "message": "not ready"}}, 2),
    ("error", {"schema": 1, "status": "error", "exit_code": 2, "error": {"kind": "guard", "message": "local quota and resource monitor error"}}, 2),
    ("invalid_json", "not json", 0),
    ("missing_final", "", 0),
])
async def test_qwen_failure_statuses_never_fall_back_to_cloud(mode, result, rc, client, auth, make_dir, tmp_path, monkeypatch):
    from ttakkari.runs import manager as run_manager

    worker_path, _ = _write_worker(tmp_path, run_result=json.dumps(result) if isinstance(result, dict) else result,
                                   exit_code=rc)
    monkeypatch.setattr(run_manager, "QWEN_LOCAL_ADAPTER", QwenLocalAdapter(worker_path=worker_path, enabled=True))
    root = make_dir()
    ws = (await client.post("/api/workspaces", json={"name": root.name, "root_path": str(root)}, headers=auth)).json()
    parent = (await client.post("/api/sessions", json={"workspace_id": ws["id"], "kind": "orchestrator"}, headers=auth)).json()
    orch_auth = {"Authorization": f"Bearer {issue_token(parent['id'])}"}
    created = await client.post("/api/orchestrator/workers", headers=orch_auth, json={
        "title": f"Qwen {mode}", "prompt": "fixture task", "engine": "qwen-local", "task_type": "general",
    })
    assert created.status_code == 201, created.text
    run = await wait_run(client, auth, created.json()["active_run_id"])
    assert run["status"] == "failed" and run["engine"] == "qwen-local"
    assert run["usage"]["routing_request"]["account_scope"] == "personal"
    events = await run_events(client, auth, run["id"])
    assert not any(e["type"] in {"engine.fallback", "account.limited"} for e in events)
