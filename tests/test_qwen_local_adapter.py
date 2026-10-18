import asyncio
from pathlib import Path

from ttakkari.agents.base import LaunchSpec, ParseState
from ttakkari.agents.qwen_local import QwenLocalAdapter


def spec(tmp_path: Path, *, resume: str | None = None) -> LaunchSpec:
    return LaunchSpec(
        cwd=tmp_path, prompt="do the bounded task", engine_session_id=resume,
        model="Qwen3.8-Flash-Next-Q2_K", effort="high", outbox=tmp_path / "outbox",
        write_roots=[tmp_path], system_note="private note", session_id="session-1",
    )


def test_command_is_fixed_and_task_is_out_of_band(tmp_path: Path):
    adapter = QwenLocalAdapter(worker_path=tmp_path / "bin/qwen-worker", enabled=True)
    s = spec(tmp_path, resume="qwen-session")
    argv = adapter.build_command(s, {})
    assert argv == [str(tmp_path / "bin/qwen-worker"), "run", "--workspace", str(tmp_path),
                    "--task-file", argv[argv.index("--task-file") + 1], "--turns", "20",
                    "--effort", "high", "--resume", "qwen-session"]
    task_path = Path(argv[argv.index("--task-file") + 1])
    assert task_path.parent == s.outbox
    assert task_path.read_text() == adapter.build_task_contract(
        system_note="private note", prompt="do the bounded task",
        workspace=str(tmp_path), turns=20, lineage="resume:qwen-session")
    adapter.cleanup(s)
    assert not task_path.exists()


def test_qwen_child_environment_does_not_inherit_backend_or_provider_secrets(monkeypatch):
    monkeypatch.setenv("TTK_ORCH_TOKEN", "backend-token")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "provider-token")
    monkeypatch.setenv("CODEX_HOME", "/secret/codex")
    child = QwenLocalAdapter.child_env()
    assert "TTK_ORCH_TOKEN" not in child
    assert "ANTHROPIC_AUTH_TOKEN" not in child
    assert "CODEX_HOME" not in child


async def test_qwen_run_slot_serializes_local_requests():
    from ttakkari.runs.manager import RunManager

    manager = RunManager()
    await manager._qwen_local_slot.acquire()
    waiting = asyncio.create_task(manager._qwen_local_slot.acquire())
    await asyncio.sleep(0)
    assert not waiting.done()
    manager._qwen_local_slot.release()
    await asyncio.wait_for(waiting, 1)
    manager._qwen_local_slot.release()


def test_disabled_and_missing_readiness_fail_closed(tmp_path: Path):
    adapter = QwenLocalAdapter(worker_path=tmp_path / "missing", enabled=False)
    assert adapter.readiness() == (False, "disabled")
    enabled = QwenLocalAdapter(worker_path=tmp_path / "missing", enabled=True)
    assert enabled.readiness() == (False, "worker_executable_missing")


def test_readiness_requires_seatbelt_hostmon_and_fixture_attestation(tmp_path: Path):
    worker = tmp_path / "bin/qwen-worker"
    worker.parent.mkdir()
    worker.write_text("#!/bin/sh\nprintf '%s\\n' '{\"contract\":\"qwen-local-readiness-v1\",\"checks\":{\"seatbelt\":true,\"hostmon\":true,\"fixture\":true}}'\n")
    worker.chmod(0o755)
    adapter = QwenLocalAdapter(worker_path=worker, enabled=True)
    assert adapter.readiness() == (True, "ready")
    worker.write_text("#!/bin/sh\nprintf '%s\\n' '{\"contract\":\"qwen-local-readiness-v1\",\"checks\":{\"seatbelt\":true,\"hostmon\":false,\"fixture\":true}}'\n")
    worker.chmod(0o755)
    assert adapter.readiness() == (False, "readiness_contract_unverified")


def test_success_result_records_session_files_model_and_resources(tmp_path: Path):
    adapter = QwenLocalAdapter(worker_path=tmp_path / "worker", enabled=True)
    state = ParseState()
    line = {"schema": 1, "status": "success", "exit_code": 0, "session_id": "s-new",
            "result_text": "done", "files": {"added": ["new.txt"], "modified": [], "deleted": []},
            "metrics": {"host_peaks": {"server_rss_max_bytes": 1024}, "elapsed_seconds": 3}}
    parsed = adapter.parse_line(line, state, tmp_path)
    assert state.result_text == "done"
    assert state.engine_session_id == "s-new"
    assert state.touched_files == {str(tmp_path / "new.txt")}
    assert parsed[0].type == "message"
    assert parsed[0].payload["model"] == "Qwen3.8-Flash-Next-Q2_K"


def test_non_success_and_malformed_final_are_errors(tmp_path: Path):
    adapter = QwenLocalAdapter(worker_path=tmp_path / "worker", enabled=True)
    for result in (
        {"schema": 1, "status": "partial", "exit_code": 1, "result_text": "partial"},
        {"schema": 1, "status": "blocked", "exit_code": 2, "result_text": None},
        {"schema": 1, "status": "error", "exit_code": 2, "result_text": None,
         "error": {"kind": "guard", "message": "local resource monitor unavailable"}},
        {"schema": 1, "status": "success", "exit_code": 0, "result_text": None},
    ):
        state = ParseState()
        adapter.parse_line(result, state, tmp_path)
        assert state.is_error
        assert state.error
        if result["status"] == "error":
            assert "[guard]" in (state.error or "")


def test_invalid_json_shape_is_rejected(tmp_path: Path):
    adapter = QwenLocalAdapter(worker_path=tmp_path / "worker", enabled=True)
    state = ParseState()
    adapter.parse_line({"schema": 99, "status": "success", "exit_code": 0, "result_text": "fake"}, state, tmp_path)
    assert state.is_error
    assert "schema" in (state.error or "")


def test_invalid_stdout_and_duplicate_final_json_are_rejected(tmp_path: Path):
    adapter = QwenLocalAdapter(worker_path=tmp_path / "worker", enabled=True)
    invalid = ParseState()
    adapter.parse_stdout_line("not json", invalid, tmp_path)
    assert invalid.is_error and "one JSON result" in (invalid.error or "")

    duplicate = ParseState()
    line = '{"schema":1,"status":"success","exit_code":0,"result_text":"done"}'
    adapter.parse_stdout_line(line, duplicate, tmp_path)
    adapter.parse_stdout_line(line, duplicate, tmp_path)
    assert duplicate.is_error and "more than one" in (duplicate.error or "")


def test_missing_final_or_nonzero_process_exit_fails_closed(tmp_path: Path):
    adapter = QwenLocalAdapter(worker_path=tmp_path / "worker", enabled=True)
    missing = ParseState()
    adapter.finalize_process(missing, return_code=0, stderr="not-json")
    assert missing.is_error and "without one final JSON" in (missing.error or "")

    failed = ParseState()
    adapter.parse_line({"schema": 1, "status": "success", "exit_code": 0, "result_text": "done"}, failed, tmp_path)
    adapter.finalize_process(failed, return_code=1, stderr="failure")
    assert failed.is_error and "exited 1" in (failed.error or "")
