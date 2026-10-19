"""Browser tool configuration tests. Does not launch a browser or model."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.codex import CodexAdapter


class BrowserMcpTests(unittest.TestCase):
    def test_codex_registers_restricted_browser_tools_and_keeps_sandbox(self):
        from ttakkari.agents import browser_mcp

        with tempfile.TemporaryDirectory() as temp:
            cli = Path(temp) / "cli.js"
            cli.touch()
            with patch.object(browser_mcp, "MCP_CLI", cli), \
                    patch.object(browser_mcp.shutil, "which", return_value="/usr/bin/node"):
                self._assert_codex_configuration()

    def _assert_codex_configuration(self):
        import tomllib

        for thread in (None, "existing-thread"):
            root = Path("/tmp/verification with spaces")
            spec = LaunchSpec(root, "verify UI", thread, None, None, root, [root], "original")
            with tempfile.TemporaryDirectory() as home, \
                    patch("ttakkari.agents.codex.sys.platform", "darwin"), \
                    patch("ttakkari.agents.codex.get_settings", return_value=SimpleNamespace(codex_bin="codex")):
                # SEC2-AGT-01: 서버 설정은 argv(-c)가 아닌 프로파일 파일에 적힌다.
                adapter = CodexAdapter()
                try:
                    argv = adapter.build_command(
                        spec, {"CODEX_HOME": home, "TTAKKARI_RUN_ID": f"browser-{thread}"})
                    flags = dict(value.split("=", 1) for i, arg in enumerate(argv[:-1]) if arg == "-c"
                                 for value in [argv[i + 1]])
                    self.assertFalse(any("mcp_servers" in flag for flag in flags))
                    profile = Path(home) / f"{argv[argv.index('-p') + 1]}.config.toml"
                    browser = tomllib.loads(profile.read_text(encoding="utf-8"))["mcp_servers"]["ttakkari_browser"]
                finally:
                    adapter.cleanup(spec)
            self.assertEqual(json.loads(flags["sandbox_mode"]), "workspace-write")
            self.assertEqual(json.loads(flags["approval_policy"]), "never")
            tools = browser["enabled_tools"]
            self.assertIn("browser_navigate", tools)
            self.assertIn("browser_take_screenshot", tools)
            self.assertNotIn("browser_run_code_unsafe", tools)
            self.assertNotIn("browser_evaluate", tools)
            self.assertNotIn("browser_file_upload", tools)
            for tool in tools:
                self.assertEqual(browser["tools"][tool]["approval_mode"], "approve")
            args = browser["args"]
            self.assertIn("--isolated", args)
            self.assertIn("--headless", args)
            self.assertIn("--sandbox", args)
            self.assertIn("--no-webmcp", args)
            self.assertNotIn("--no-sandbox", args)
            self.assertIn("cwd", browser)
            note = json.loads(flags["developer_instructions"])
            self.assertIn("ttakkari_browser", note)
            self.assertEqual(spec.mcp_servers, {})

    def test_factory_missing_runtime_returns_none(self):
        from ttakkari.agents import browser_mcp

        with tempfile.TemporaryDirectory() as temp, \
                patch.object(browser_mcp, "MCP_CLI", Path(temp) / "missing.js"):
            self.assertIsNone(browser_mcp.server_spec(Path(temp)))

    def test_factory_missing_node_returns_none(self):
        from ttakkari.agents import browser_mcp

        with patch.object(browser_mcp.shutil, "which", return_value=None):
            self.assertIsNone(browser_mcp.server_spec(Path("/tmp/output")))
