"""Tests for GitHub Copilot CLI managed accounts and authentication isolation."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from uuid import uuid4

from ttakkari.copilot_accounts import (
    allocate,
    check_auth,
    execution_env,
    is_ready,
    load,
    login_command,
    record_run,
    status,
)


class CopilotAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.account_id = uuid4()

    def tearDown(self):
        self.tmp.cleanup()

    def test_allocate_and_load_managed_profile(self):
        profile = allocate(self.base, self.account_id)
        self.assertTrue(profile.home.exists())
        self.assertTrue(profile.manifest_path.exists())

        loaded = load(profile.home)
        self.assertEqual(loaded.account_id, self.account_id)
        self.assertFalse(loaded.is_system)

        st = status(profile.home)
        self.assertEqual(st["auth_state"], "setup_required")
        self.assertFalse(is_ready(profile.home))

    def test_system_account_readiness(self):
        # When home is None, status checks system default credentials
        st = status(None, is_system=True)
        self.assertIn(st["auth_state"], {"authenticated", "setup_required"})
        self.assertTrue(st["is_system"])

    def test_execution_env_injects_copilot_home_and_tmpdir(self):
        profile = allocate(self.base, self.account_id)
        env = execution_env(profile.home, {"EXISTING": "val"})
        self.assertEqual(env["EXISTING"], "val")
        self.assertEqual(env["COPILOT_HOME"], str(profile.home))
        self.assertEqual(env["COPILOT_ALLOW_ALL"], "true")
        self.assertTrue(env["TMPDIR"].startswith("/Users/winterholic/development/.tmp"))

    def test_system_execution_env_uses_home_copilot(self):
        env = execution_env(None, is_system=True)
        self.assertTrue(env["COPILOT_HOME"].endswith(".copilot"))
        self.assertEqual(env["COPILOT_ALLOW_ALL"], "true")

    def test_login_command_for_isolated_and_system(self):
        profile = allocate(self.base, self.account_id)
        cmd_isolated = login_command(profile.home, is_system=False)
        self.assertIn("COPILOT_HOME=", cmd_isolated)
        self.assertIn("login --device-code", cmd_isolated)

        cmd_system = login_command(None, is_system=True)
        self.assertNotIn("COPILOT_HOME=", cmd_system)
        self.assertIn("login --device-code", cmd_system)

    @patch("subprocess.run")
    def test_check_auth_detects_authenticated_copilot(self, mock_run):
        profile = allocate(self.base, self.account_id)

        # Create dummy config.json in profile home
        (profile.home / "config.json").write_text('{"theme": "dark"}')

        ver_res = MagicMock(returncode=0, stdout="GitHub Copilot CLI 1.0.103.\n", stderr="")
        conf_res = MagicMock(returncode=0, stdout="theme=dark\n", stderr="")
        mock_run.side_effect = [ver_res, conf_res]

        st = check_auth(profile.home, cli_bin="copilot")
        self.assertEqual(st["auth_state"], "authenticated")
        self.assertEqual(st["cli_version"], "GitHub Copilot CLI 1.0.103.")
        self.assertTrue(is_ready(profile.home))

    @patch("subprocess.run")
    def test_check_auth_handles_setup_required_when_no_config(self, mock_run):
        profile = allocate(self.base, self.account_id)

        ver_res = MagicMock(returncode=0, stdout="GitHub Copilot CLI 1.0.103.\n", stderr="")
        conf_res = MagicMock(returncode=1, stdout="", stderr="error: no credentials")
        mock_run.side_effect = [ver_res, conf_res]

        st = check_auth(profile.home, cli_bin="copilot")
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
        record_run(profile.home, success=False, error="Error: Access denied by policy settings (Request ID: 123)")
        st = status(profile.home)
        self.assertEqual(st["auth_state"], "auth_expired")
        self.assertEqual(st["last_error"], "auth_expired")
