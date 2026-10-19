"""설치된 CLI의 help 파서로 검사하며 모델이나 MCP 서버는 실행하지 않는다."""
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.codex import CodexAdapter


@pytest.mark.parametrize("thread", [None, "00000000-0000-0000-0000-000000000001"])
@pytest.mark.parametrize("with_mcp", [False, True])
def test_generated_command_is_accepted_by_installed_codex(tmp_path, monkeypatch, thread, with_mcp):
    binary = shutil.which("codex")
    if binary is None:
        pytest.skip("Codex CLI is not installed")
    monkeypatch.setattr("ttakkari.agents.codex.get_settings", lambda: SimpleNamespace(codex_bin=binary))
    monkeypatch.setattr("ttakkari.agents.codex.server_spec", lambda _: None)
    home = tmp_path / "codex-home"
    servers = {"ttakkari": {"command": "python", "args": [], "env": {"TTK_ORCH_TOKEN": "fake-token"}}}
    spec = LaunchSpec(cwd=tmp_path, prompt="do not execute", engine_session_id=thread,
                      model="test-model", effort="medium", outbox=tmp_path, write_roots=[tmp_path],
                      system_note="", mcp_servers=servers if with_mcp else {})
    adapter = CodexAdapter()
    try:
        argv = adapter.build_command(spec, {"CODEX_HOME": str(home), "TTAKKARI_RUN_ID": "parser-test"})
        result = subprocess.run([*argv[:-2], "--help"], capture_output=True, text=True, timeout=10, check=False,
                                env={"CODEX_HOME": str(home), "PATH": "/usr/bin:/bin"})
        assert result.returncode == 0, result.stderr
        assert "Usage: codex exec" in result.stdout
        assert "fake-token" not in " ".join(argv)
    finally:
        adapter.cleanup(spec)
