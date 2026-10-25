"""Tests for OpenCode managed accounts and Copilot authentication isolation."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from ttakkari.opencode_accounts import (
    allocate,
    check_auth,
    execution_env,
    is_ready,
    load,
    login_command,
    record_run,
    status,
)


class OpencodeAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.account_id = uuid4()

    def tearDown(self):
        self.tmp.cleanup()

    def test_allocate_and_load_managed_profile(self):
        profile = allocate(self.base, self.account_id)
        self.assertTrue(profile.home.exists())
        self.assertTrue(profile.data_dir.exists())
        self.assertTrue(profile.config_dir.exists())
        self.assertTrue(profile.state_dir.exists())
        self.assertTrue(profile.cache_dir.exists())
        self.assertTrue(profile.manifest_path.exists())

        loaded = load(profile.home)
        self.assertEqual(loaded.account_id, self.account_id)

        st = status(profile.home)
        self.assertEqual(st["auth_state"], "setup_required")
        self.assertFalse(is_ready(profile.home))

    def test_execution_env_injects_xdg_paths_and_tmpdir(self):
        profile = allocate(self.base, self.account_id)
        env = execution_env(profile.home, {"EXISTING": "val"})
        self.assertEqual(env["EXISTING"], "val")
        self.assertEqual(env["XDG_DATA_HOME"], str(profile.data_dir))
        self.assertEqual(env["XDG_CONFIG_HOME"], str(profile.config_dir))
        self.assertEqual(env["XDG_STATE_HOME"], str(profile.state_dir))
        self.assertEqual(env["XDG_CACHE_HOME"], str(profile.cache_dir))
        self.assertTrue(env["TMPDIR"].startswith("/Users/winterholic/development/.tmp"))

    def test_login_command_contains_xdg_exports_and_copilot_flag(self):
        profile = allocate(self.base, self.account_id)
        cmd = login_command(profile.home)
        self.assertIn("XDG_DATA_HOME=", cmd)
        self.assertIn("XDG_CONFIG_HOME=", cmd)
        self.assertIn("auth login -p github-copilot", cmd)

    @patch("subprocess.run")
    def test_check_auth_detects_authenticated_copilot(self, mock_run):
        profile = allocate(self.base, self.account_id)

        # Mock --version and auth list
        ver_res = MagicMock(returncode=0, stdout="1.19.4\n", stderr="")
        list_res = MagicMock(returncode=0, stdout="Credentials auth.json\nGitHub Copilot oauth\n1 credentials\n", stderr="")
        mock_run.side_effect = [ver_res, list_res]

        st = check_auth(profile.home, cli_bin="opencode")
        self.assertEqual(st["auth_state"], "authenticated")
        self.assertEqual(st["cli_version"], "1.19.4")
        self.assertTrue(is_ready(profile.home))

    @patch("subprocess.run")
    def test_check_auth_handles_empty_credentials(self, mock_run):
        profile = allocate(self.base, self.account_id)

        ver_res = MagicMock(returncode=0, stdout="1.19.4\n", stderr="")
        list_res = MagicMock(returncode=0, stdout="Credentials auth.json\n0 credentials\n", stderr="")
        mock_run.side_effect = [ver_res, list_res]

        st = check_auth(profile.home, cli_bin="opencode")
        self.assertEqual(st["auth_state"], "setup_required")
        self.assertFalse(is_ready(profile.home))

    def test_record_run_success_updates_success_timestamp(self):
        profile = allocate(self.base, self.account_id)
        before = time.time() - 1
        record_run(profile.home, success=True)
        st = status(profile.home)
        self.assertIsNotNone(st["last_success_at"])
        self.assertGreater(st["last_success_at"], before)
        self.assertIsNone(st["last_error"])

    def test_record_run_marks_expired_on_auth_failure(self):
        profile = allocate(self.base, self.account_id)
        # First set as authenticated
        data = json.loads(profile.manifest_path.read_text())
        data["auth_state"] = "authenticated"
        profile.manifest_path.write_text(json.dumps(data))

        # Runtime fails with 401 unauthorized
        record_run(profile.home, success=False, error="API 401 Unauthorized token expired")
        st = status(profile.home)
        self.assertEqual(st["auth_state"], "auth_expired")
        self.assertEqual(st["last_error"], "auth_expired")
        self.assertFalse(is_ready(profile.home))

    def test_record_run_preserves_auth_state_on_general_failure(self):
        profile = allocate(self.base, self.account_id)
        data = json.loads(profile.manifest_path.read_text())
        data["auth_state"] = "authenticated"
        profile.manifest_path.write_text(json.dumps(data))

        record_run(profile.home, success=False, error="SyntaxError: unexpected token")
        st = status(profile.home)
        self.assertEqual(st["auth_state"], "authenticated")
        self.assertIn("SyntaxError", st["last_error"])


if __name__ == "__main__":
    unittest.main()
