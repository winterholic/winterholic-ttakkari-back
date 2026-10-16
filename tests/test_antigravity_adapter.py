import uuid
from pathlib import Path

from ttakkari.agents.antigravity import AntigravityAdapter
from ttakkari.agents.base import LaunchSpec, ParseState


def spec(**overrides):
    values = {
        "cwd": Path("/workspace"),
        "prompt": "Do the task",
        "engine_session_id": None,
        "model": "gemini-3.8-flash-high",
        "effort": "high",
        "outbox": Path("/outbox"),
        "write_roots": [Path("/workspace"), Path("/outbox")],
        "system_note": "Keep the workspace boundary.",
    }
    values.update(overrides)
    return LaunchSpec(**values)


def test_launch_uses_explicit_conversation_id_and_no_global_continue(monkeypatch):
    monkeypatch.setattr("ttakkari.agents.antigravity.get_settings",
                        lambda: type("Settings", (), {
                            "antigravity_bin": "/opt/homebrew/bin/agy",
                            "run_timeout_seconds": 120,
                        })())
    argv = AntigravityAdapter().build_command(
        spec(engine_session_id="conversation-123"), {})

    assert argv[0:2] == ["/opt/homebrew/bin/agy", "-p"]
    assert argv[argv.index("--conversation") + 1] == "conversation-123"
    assert "--continue" not in argv
    assert "--output-format" in argv and "stream-json" in argv
    assert "--sandbox" in argv
    assert "--dangerously-skip-permissions" not in argv


def test_stream_parser_normalizes_session_text_tools_errors_and_usage():
    adapter = AntigravityAdapter()
    state = ParseState()

    assert adapter.parse_line({
        "event": "init",
        "conversation_id": "conv-1",
        "init": {"model": "gemini-3.8-flash-high", "tools": ["run_command"]},
    }, state, Path("/workspace"))[0].payload["engine_session_id"] == "conv-1"

    delta = adapter.parse_line({
        "event": "step_update",
        "step_update": {"step_type": "agent_response", "state": "ACTIVE", "text_delta": "hello"},
    }, state, Path("/workspace"))
    assert delta == [type(delta[0])("message", {"text": "hello"})]

    call = adapter.parse_line({
        "event": "step_update",
        "step_update": {
            "step_type": "tool", "state": "DONE", "tool_name": "run_command",
            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "pwd"}, "output": "/workspace"},
        },
    }, state, Path("/workspace"))
    assert [x.type for x in call] == ["tool.call", "tool.result"]
    assert call[0].payload["name"] == "run_command"
    assert call[1].payload["output"] == "/workspace"

    result = adapter.parse_line({
        "event": "result",
        "result": {
            "conversation_id": "conv-1", "status": "SUCCESS", "response": "hello",
            "usage": {"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
        },
    }, state, Path("/workspace"))
    assert state.result_text == "hello"
    assert state.usage["total_tokens"] == 15
    assert any(x.type == "usage" for x in result)

    failed = adapter.parse_line({
        "event": "result",
        "result": {"conversation_id": "conv-1", "status": "ERROR", "error": "denied", "response": ""},
    }, state, Path("/workspace"))
    assert state.is_error and "denied" in state.error
    assert any(x.type == "error" for x in failed)


def test_success_without_a_final_response_is_not_reported_as_a_successful_run():
    state = ParseState()
    events = AntigravityAdapter().parse_line({
        "event": "result",
        "result": {"conversation_id": "new-id", "status": "SUCCESS", "response": ""},
    }, state, Path("/workspace"))

    assert state.is_error
    assert "response" in state.error.lower()
    assert any(event.type == "error" for event in events)


def test_zero_exit_without_result_surfaces_stderr_as_run_error():
    state = ParseState()

    AntigravityAdapter().finalize_process(state, return_code=0, stderr="operation not permitted\n")

    assert state.is_error
    assert "operation not permitted" in state.error.lower()


def test_resume_rejects_cli_returning_a_different_conversation_id():
    state = ParseState(engine_session_id="requested-conversation")
    events = AntigravityAdapter().parse_line({
        "event": "init",
        "conversation_id": "new-conversation",
        "init": {"model": "gemini-3.8-flash-high"},
    }, state, Path("/workspace"))

    assert state.is_error
    assert state.engine_session_id == "requested-conversation"
    assert "requested-conversation" in state.error
    assert "new-conversation" in state.error
    assert [event.type for event in events] == ["error"]


def test_parser_reports_changed_files_and_permission_denials():
    adapter = AntigravityAdapter()
    state = ParseState()
    events = adapter.parse_line({
        "event": "step_update",
        "step_update": {
            "step_type": "tool", "state": "DONE", "tool_name": "write_to_file",
            "tool_info": {"name": "write_to_file", "parameters": {"FilePath": "src/a.txt"},
                          "output": "ok"},
        },
    }, state, Path("/workspace"))
    assert str(Path("/workspace/src/a.txt")) in state.touched_files
    assert events[0].type == "tool.call"

    blocked = adapter.parse_line({
        "event": "step_update",
        "step_update": {
            "step_type": "tool", "state": "DONE", "tool_name": "run_command",
            "tool_info": {"name": "run_command", "parameters": {"CommandLine": "cat ~/.ssh/id_ed25519"},
                          "error": {"type": "permission_denied", "message": "[ttakkari-guard] blocked"}},
        },
    }, state, Path("/workspace"))
    assert any(x.type == "guard.blocked" for x in blocked)


def test_tool_stream_deduplicates_call_events_and_never_persists_file_contents():
    adapter = AntigravityAdapter()
    state = ParseState()
    active = adapter.parse_line({
        "event": "step_update",
        "step_update": {
            "step_index": 9, "step_type": "tool", "state": "ACTIVE", "tool_name": "write_to_file",
            "tool_info": {"name": "write_to_file",
                          "parameters": {"FilePath": "ui.tsx", "Content": "PRIVATE_CONTENT"}},
        },
    }, state, Path("/workspace"))
    done = adapter.parse_line({
        "event": "step_update",
        "step_update": {
            "step_index": 9, "step_type": "tool", "state": "DONE", "tool_name": "write_to_file",
            "tool_info": {"name": "write_to_file",
                          "parameters": {"FilePath": "ui.tsx", "Content": "PRIVATE_CONTENT"}, "output": "ok"},
        },
    }, state, Path("/workspace"))
    assert [event.type for event in active] == ["tool.call"]
    assert active[0].payload["input"] == {"path": "ui.tsx", "bytes": len("PRIVATE_CONTENT")}
    assert [event.type for event in done] == ["tool.result"]
    assert "PRIVATE_CONTENT" not in repr(active + done)


def test_actual_cli_target_file_field_is_tracked():
    state = ParseState()
    result = AntigravityAdapter().parse_line({
        "event": "step_update",
        "step_update": {
            "step_index": 2, "step_type": "tool", "state": "DONE", "tool_name": "write_to_file",
            "tool_info": {"name": "write_to_file", "parameters": {"TargetFile": "src/new.tsx"}},
        },
    }, state, Path("/workspace"))

    assert str(Path("/workspace/src/new.tsx")) in state.touched_files
    assert result[0].payload["input"]["path"] == "src/new.tsx"


def test_session_mcp_config_is_private_and_removed_by_terminal_cleanup(monkeypatch):
    from ttakkari.config import Settings

    monkeypatch.setattr("ttakkari.agents.antigravity.get_settings", lambda: Settings(
        _env_file=None, antigravity_bin="/opt/homebrew/bin/agy",
    ))
    session_id = str(uuid.uuid4())
    adapter = AntigravityAdapter()
    run_spec = spec(session_id=session_id, mcp_servers={
        "ttakkari": {"command": "/usr/bin/python3", "args": ["-m", "ttakkari.orchestrator.mcp_server"],
                     "env": {"TTK_ORCH_TOKEN": "fixture-only"}},
    })
    project = adapter.working_directory(run_spec)
    adapter.build_command(run_spec, {})
    config = project / ".agents" / "mcp_config.json"
    assert config.exists()
    assert config.stat().st_mode & 0o777 == 0o600

    adapter.cleanup(run_spec)
    assert not config.exists()
