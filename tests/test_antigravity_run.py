import asyncio
import textwrap
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from helpers import wait_run

from ttakkari.db import sessionmaker
from ttakkari.models import ChatSession
from ttakkari.orchestrator.runtime import issue_token


@pytest.mark.parametrize("ending", ["success", "error", "timeout", "cancel"])
async def test_mcp_config_survives_init_until_cli_exit_and_is_always_removed(
    ending, client, auth, new_session, make_dir, monkeypatch
):
    """agy emits init before loading MCP; exercise that order with a real child process."""
    from ttakkari.agents import antigravity
    from ttakkari.config import get_settings
    from ttakkari.runs import manager as manager_mod

    # ba4f0220: 일시 오류는 같은 Run 을 자동 재시도한다. 이 시험은 최종 실패 기록을 보므로 재시도 예산을 0 으로 둔다.
    monkeypatch.setattr(manager_mod, "MAX_EXECUTION_RECOVERY_ATTEMPTS", 0)

    account = await client.post("/api/accounts", json={
        "name": "MCP lifecycle fixture", "engine": "antigravity",
    }, headers=auth)
    assert account.status_code == 201, account.text
    _workspace, chat = await new_session(engine="antigravity")
    fixture_dir = make_dir()
    ready = fixture_dir / "mcp-config-loaded"
    fake_cli = fixture_dir / "agy_delayed_mcp.py"
    fake_cli.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, time
        from pathlib import Path
        print(json.dumps({{"event": "init", "conversation_id": "delayed-mcp",
                           "init": {{"model": "gemini-3.8-flash-high"}}}}), flush=True)
        time.sleep(0.2)
        config = Path('.agents/mcp_config.json')
        if not config.exists():
            print('MCP_CONFIG_REMOVED_BEFORE_LOAD', flush=True)
            raise SystemExit(2)
        assert config.stat().st_mode & 0o777 == 0o600
        assert 'ttakkari' in json.loads(config.read_text())['mcpServers']
        Path({str(ready)!r}).write_text('loaded')
        if {ending!r} in {{'timeout', 'cancel'}}:
            time.sleep(30)
        print(json.dumps({{"event": "result", "result": {{
            "conversation_id": "delayed-mcp",
            "status": 'SUCCESS' if {ending!r} == 'success' else 'ERROR',
            "response": 'MCP_CONFIG_LOADED', "error": 'fixture error'
        }}}}), flush=True)
        """))
    fake_cli.chmod(0o700)
    monkeypatch.setattr(antigravity, "get_settings", lambda: SimpleNamespace(
        antigravity_bin=str(fake_cli), run_timeout_seconds=30,
    ))
    monkeypatch.setattr(get_settings(), "run_timeout_seconds", 1.5 if ending == "timeout" else 30)
    response = await client.post(f"/api/sessions/{chat['id']}/runs", json={"prompt": "fixture"}, headers=auth)
    assert response.status_code == 202, response.text
    run_id = response.json()["id"]
    if ending == "cancel":
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert ready.exists(), "CLI could not load MCP after init"
        cancelled = await client.post(f"/api/runs/{run_id}/cancel", headers=auth)
        assert cancelled.status_code == 200, cancelled.text
    result = await wait_run(client, auth, run_id)
    assert ready.exists(), "CLI could not load MCP after init"
    assert result["status"] == {
        "success": "succeeded", "error": "failed", "timeout": "failed", "cancel": "cancelled",
    }[ending], result
    if ending == "success":
        assert result["result_text"] == "MCP_CONFIG_LOADED"
    if ending == "timeout":
        assert "시간 초과" in result["error"]
    config = Path("/private/tmp/ttakkari-antigravity") / chat["id"] / ".agents/mcp_config.json"
    assert not config.exists(), "MCP session token file survived CLI termination"


async def test_spawn_os_error_cleans_temporary_mcp_token_file(monkeypatch):
    from ttakkari.agents.antigravity import AntigravityAdapter
    from ttakkari.agents.base import LaunchSpec
    from ttakkari.runs.manager import RunManager

    session_id = str(uuid.uuid4())
    run_spec = LaunchSpec(
        cwd=Path("/workspace"), prompt="work", engine_session_id=None, model="gemini-3.8-flash-high",
        effort="high", outbox=Path("/outbox"), write_roots=[Path("/workspace")], system_note="",
        session_id=session_id,
        mcp_servers={"ttakkari": {"command": "/usr/bin/python3", "env": {"TTK_ORCH_TOKEN": "test-only"}}},
    )
    adapter = AntigravityAdapter()
    config = adapter.working_directory(run_spec) / ".agents" / "mcp_config.json"

    async def denied(*args, **kwargs):
        raise PermissionError("fixture spawn denied")

    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", denied)
    manager = RunManager()
    with pytest.raises(PermissionError, match="fixture spawn denied"):
        await manager._attempt(
            db=None, run=SimpleNamespace(id=uuid.uuid4(), pid=None), adapter=adapter, spec=run_spec,
            env={}, root=Path("/workspace"), h=None,
        )

    assert not config.exists()


async def test_termination_permission_error_still_removes_temporary_mcp_token_file(monkeypatch):
    from ttakkari.agents.antigravity import AntigravityAdapter
    from ttakkari.agents.base import LaunchSpec
    from ttakkari.runs.manager import RunManager

    run_spec = LaunchSpec(
        cwd=Path("/workspace"), prompt="work", engine_session_id=None, model="gemini-3.8-flash-high",
        effort="high", outbox=Path("/outbox"), write_roots=[Path("/workspace")], system_note="",
        session_id=str(uuid.uuid4()), mcp_servers={"ttakkari": {"command": "fixture-only"}},
    )
    adapter = AntigravityAdapter()
    config = adapter.working_directory(run_spec) / ".agents/mcp_config.json"
    stderr = asyncio.StreamReader()
    stderr.feed_eof()
    proc = SimpleNamespace(pid=12345, returncode=None, stderr=stderr)

    async def spawn(*args, **kwargs):
        return proc

    async def no_op(*args, **kwargs):
        pass

    async def denied(*args, **kwargs):
        raise PermissionError("fixture process-group signal denied")

    manager = RunManager()
    monkeypatch.setattr("ttakkari.runs.manager.asyncio.create_subprocess_exec", spawn)
    monkeypatch.setattr(manager, "_commit", no_op)
    monkeypatch.setattr(manager, "_pump", no_op)
    monkeypatch.setattr(manager, "_terminate", denied)
    try:
        with pytest.raises(PermissionError, match="process-group signal denied"):
            await manager._attempt(
                db=None, run=SimpleNamespace(id=uuid.uuid4(), pid=None), adapter=adapter, spec=run_spec,
                env={}, root=Path("/workspace"), h=None,
            )
        assert not config.exists(), "failed process termination leaked the MCP session token file"
    finally:
        adapter.cleanup(run_spec)


async def test_terminate_retries_process_group_after_eprem_and_reaps_exited_leader(monkeypatch):
    from ttakkari.runs.manager import RunManager

    calls = []

    class ExitedLeader:
        pid = 12345
        returncode = None

        async def wait(self):
            self.returncode = 0
            return self.returncode

    def killpg(pid, sig):
        calls.append((pid, sig))
        if len(calls) == 1:
            raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("ttakkari.runs.manager.os.killpg", killpg)
    await RunManager()._terminate(ExitedLeader())

    assert len(calls) == 2, "after reaping an exited group leader, signal the group again to catch live children"


async def test_terminate_does_not_suppress_eprem_while_leader_is_still_running(monkeypatch):
    from ttakkari.runs.manager import RunManager

    class RunningLeader:
        pid = 12345
        returncode = None

        async def wait(self):
            await asyncio.Future()

    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("ttakkari.runs.manager.os.killpg", denied)
    monkeypatch.setattr(
        "ttakkari.runs.manager.get_settings",
        lambda: SimpleNamespace(cancel_grace_seconds=0.01),
    )
    with pytest.raises(PermissionError, match="Operation not permitted"):
        await RunManager()._terminate(RunningLeader())


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


async def test_antigravity_empty_stdout_is_not_reported_as_success(client, auth, new_session, make_dir, monkeypatch):
    account = await client.post("/api/accounts", json={"name": "Fixture agy", "engine": "antigravity"}, headers=auth)
    assert account.status_code == 201, account.text
    _workspace, chat = await new_session(engine="antigravity")

    fake_cli = make_dir() / "agy_fake_empty.py"
    fake_cli.write_text("#!/usr/bin/env python3\nimport sys\nprint('operation not permitted', file=sys.stderr)\n")
    fake_cli.chmod(fake_cli.stat().st_mode | 0o111)

    from ttakkari.agents import antigravity

    monkeypatch.setattr(antigravity, "get_settings", lambda: SimpleNamespace(
        antigravity_bin=str(fake_cli), run_timeout_seconds=30,
    ))

    response = await client.post(f"/api/sessions/{chat['id']}/runs", json={"prompt": "return a result"}, headers=auth)
    assert response.status_code == 202, response.text
    run = await wait_run(client, auth, response.json()["id"])

    assert run["status"] == "failed"
    assert "operation not permitted" in run["error"].lower()


async def test_terminate_tolerates_eperm_on_the_second_signal_after_reaping(monkeypatch):
    """회수 뒤 재신호에서도 EPERM 이 날 수 있다. 운영에서 Muse Run 이 '내부 오류'로 실패한 경로다(2026-10-27)."""
    from ttakkari.runs.manager import RunManager

    class ExitedLeader:
        pid = 12345
        returncode = None

        async def wait(self):
            self.returncode = 0
            return self.returncode

    def denied(pid, sig):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr("ttakkari.runs.manager.os.killpg", denied)
    await RunManager()._terminate(ExitedLeader())
