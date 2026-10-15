import asyncio
import textwrap
import time
import uuid
from types import SimpleNamespace

from helpers import wait_run

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import issue_token


async def test_run_fails_closed_when_resume_returns_a_new_conversation(
    client, auth, new_session, make_dir, monkeypatch
):
    account = await client.post("/api/accounts", json={"name": "Fixture agy", "engine": "antigravity"}, headers=auth)
    assert account.status_code == 201, account.text
    account_id = account.json()["id"]
    _workspace, chat = await new_session(engine="antigravity")
    requested = "requested-conversation-id"
    async with sessionmaker()() as db:
        session = await db.get(ChatSession, uuid.UUID(chat["id"]))
        session.engine_sessions = {account_id: requested}
        session.engine_session_id = requested
        await db.commit()

    fake_cli = make_dir() / "agy_fake.py"
    fake_cli.write_text(textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json, time
        print(json.dumps({"event": "init", "conversation_id": "new-conversation-id",
                          "init": {"model": "gemini-3.8-flash-high"}}), flush=True)
        time.sleep(30)
        """
    ))
    fake_cli.chmod(fake_cli.stat().st_mode | 0o111)

    from ttakkari.agents import antigravity

    monkeypatch.setattr(antigravity, "get_settings", lambda: SimpleNamespace(
        antigravity_bin=str(fake_cli), run_timeout_seconds=30,
    ))

    response = await client.post(f"/api/sessions/{chat['id']}/runs", json={"prompt": "continue"}, headers=auth)
    assert response.status_code == 202, response.text
    run = await wait_run(client, auth, response.json()["id"])

    assert run["status"] == "failed"
    assert "could not resume" in run["error"].lower()
    assert requested in run["error"] and "new-conversation-id" in run["error"]
    saved = (await client.get(f"/api/sessions/{chat['id']}", headers=auth)).json()
    assert saved["engine_session_id"] == requested
    events = (await client.get(f"/api/runs/{run['id']}/events", headers=auth)).json()
    assert any(event["type"] == "error" and "new-conversation-id" in event["payload"]["message"]
               for event in events)


async def test_antigravity_worker_result_is_reported_to_its_orchestrator(
    client, auth, make_dir, monkeypatch
):
    account = await client.post("/api/accounts", json={"name": "Fixture agy", "engine": "antigravity"}, headers=auth)
    assert account.status_code == 201, account.text
    root = make_dir()
    workspace = await client.post("/api/workspaces", json={
        "name": root.name, "root_path": str(root),
    }, headers=auth)
    assert workspace.status_code == 201, workspace.text
    orchestrator = await client.post("/api/sessions", json={
        "workspace_id": workspace.json()["id"], "kind": "orchestrator",
    }, headers=auth)
    assert orchestrator.status_code == 201, orchestrator.text
    orch_id = orchestrator.json()["id"]
    orch_headers = {"Authorization": f"Bearer {issue_token(orch_id)}"}

    fake_cli = make_dir() / "agy_fake_success.py"
    fake_cli.write_text(textwrap.dedent(
        """\
        #!/usr/bin/env python3
        import json
        print(json.dumps({"event": "init", "conversation_id": "fixture-worker-conversation",
                          "init": {"model": "gemini-3.8-flash-high"}}), flush=True)
        print(json.dumps({"event": "step_update", "step_update": {
            "step_type": "agent_response", "text_delta": "Fixture finished"}}), flush=True)
        print(json.dumps({"event": "result", "result": {
            "conversation_id": "fixture-worker-conversation", "status": "SUCCESS",
            "response": "Fixture finished", "usage": {"total_tokens": 11}}}), flush=True)
        """
    ))
    fake_cli.chmod(fake_cli.stat().st_mode | 0o111)

    from ttakkari.agents import antigravity

    monkeypatch.setattr(antigravity, "get_settings", lambda: SimpleNamespace(
        antigravity_bin=str(fake_cli), run_timeout_seconds=30,
    ))
    created = await client.post("/api/orchestrator/workers", json={
        "title": "fixture report", "prompt": "Complete the isolated fixture task",
        "engine": "antigravity", "model": "gemini-3.8-flash-high", "effort": "high",
    }, headers=orch_headers)
    assert created.status_code == 201, created.text
    worker_id = created.json()["worker_id"]

    worker_runs = (await client.get(f"/api/sessions/{worker_id}/runs", headers=auth)).json()
    result = await wait_run(client, auth, worker_runs[0]["id"])
    assert result["status"] == "succeeded" and result["result_text"] == "Fixture finished"
    assert result["usage"]["total_tokens"] == 11
    assert result["usage"].get("cost_usd") is None

    deadline = time.monotonic() + 10
    report = None
    while time.monotonic() < deadline:
        runs = (await client.get(f"/api/sessions/{orch_id}/runs", headers=auth)).json()
        report = next((item for item in runs if item["origin"] == "report"), None)
        if report:
            break
        await asyncio.sleep(0.05)
    assert report is not None, "worker result was not reported to its orchestrator"
    assert "[작업 보고]" in report["prompt"] and "Fixture finished" in report["prompt"]
    assert (await wait_run(client, auth, report["id"]))["status"] == "succeeded"
