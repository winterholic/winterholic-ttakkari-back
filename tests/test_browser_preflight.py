"""Read-only browser preflight unit tests; no browser or database is started.

Run: .venv/bin/python -m unittest discover -s tests -p test_browser_preflight.py
"""
import ctypes
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ttakkari.agents import browser_preflight as probe
from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.codex import CodexAdapter


class BrowserPreflightTests(unittest.TestCase):
    def test_non_macos_does_not_load_macos_library(self):
        with patch.object(probe.sys, "platform", "linux"), patch.object(probe.ctypes, "CDLL") as loader:
            result = probe.inspect_permissions()
        self.assertEqual(result["status"], "not_applicable")
        loader.assert_not_called()

    def _mac_probe(self, results):
        check = unittest.mock.Mock(side_effect=results)
        library = SimpleNamespace(sandbox_check=check)
        with patch.object(probe.sys, "platform", "darwin"), \
                patch.object(probe.ctypes, "CDLL", return_value=library), \
                patch.object(probe.ctypes.c_int, "in_dll", return_value=ctypes.c_int(0)), \
                patch.object(probe.os, "getpid", return_value=123):
            result = probe.inspect_permissions()
        return result, check

    def test_denial_is_blocked_and_only_queries_own_process(self):
        result, check = self._mac_probe([1, 0, 0])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["checks"][0]["result"], "denied")
        self.assertIn("MachPortRendezvousServer.123", result["checks"][0]["name"])
        self.assertTrue(all(call.args[0] == 123 for call in check.call_args_list))
        self.assertEqual(check.argtypes, [ctypes.c_int, ctypes.c_char_p, ctypes.c_int])

    def test_allowed_is_not_a_browser_success_claim(self):
        result, _ = self._mac_probe([0, 0, 0])
        self.assertEqual(result["status"], "permissions_allowed")
        self.assertFalse(result["browser_launched"])

    def test_query_error_is_not_treated_as_permission(self):
        result, _ = self._mac_probe([-1, 0, 0])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["checks"][0]["result"], "query_error")

    def test_library_failure_is_unknown(self):
        with patch.object(probe.sys, "platform", "darwin"), \
                patch.object(probe.ctypes, "CDLL", side_effect=OSError("unavailable")):
            self.assertEqual(probe.inspect_permissions()["status"], "unknown")


class BrowserInstructionTests(unittest.TestCase):
    def test_restricted_macos_new_and_resumed_runs_receive_preflight(self):
        root = Path("/tmp/project with spaces")
        for sandbox in ("workspace-write", "read-only", "invalid"):
            for thread in (None, "existing-thread"):
                with self.subTest(sandbox=sandbox, thread=thread):
                    spec = LaunchSpec(root, "test UI", thread, None, None, root, [root], "original note",
                                      codex_sandbox=sandbox)
                    with patch("ttakkari.agents.codex.sys.platform", "darwin"), \
                            patch("ttakkari.agents.codex.get_settings", return_value=SimpleNamespace(codex_bin="codex")):
                        argv = CodexAdapter().build_command(spec, {})
                    flags = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-c"]
                    note = json.loads(next(value.split("=", 1)[1] for value in flags
                                           if value.startswith("developer_instructions=")))
                    self.assertIn("original note", note)
                    self.assertIn("browser_preflight.py", note)
                    self.assertIn("Do not retry", note)
                    self.assertIn('approval_policy="never"', flags)
                    self.assertNotIn('sandbox_mode="danger-full-access"', flags)

    def test_other_environments_keep_existing_note(self):
        root = Path("/tmp/project")
        for platform, sandbox in (("linux", "workspace-write"), ("darwin", "danger-full-access")):
            spec = LaunchSpec(root, "test UI", None, None, None, root, [root], "original note",
                              codex_sandbox=sandbox)
            with patch("ttakkari.agents.codex.sys.platform", platform), \
                    patch("ttakkari.agents.codex.get_settings", return_value=SimpleNamespace(codex_bin="codex")):
                argv = CodexAdapter().build_command(spec, {})
            note = next(value for value in argv if value.startswith("developer_instructions="))
            self.assertEqual(json.loads(note.split("=", 1)[1]), "original note")
