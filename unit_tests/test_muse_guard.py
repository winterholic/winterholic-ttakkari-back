import tempfile
import unittest
from pathlib import Path

import pytest

from ttakkari.security.muse_hook import decision

pytestmark = [pytest.mark.security]



class MuseGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()).resolve() / 'muse-workspace'
        self.policy = {'cwd': str(self.root), 'write_roots': [str(self.root)],
                       'protected': ['/private/protected'], 'private_root': '/private/muse-policy',
                       'mcp_servers': ['ttakkari']}

    def test_workspace_write_allowed_and_external_write_denied(self):
        self.assertIsNone(decision({'tool_name': 'write_file', 'tool_input': {'path': 'a.py'}}, self.policy))
        self.assertIsNotNone(decision({'tool_name': 'write_file', 'tool_input': {'path': '/etc/a.py'}}, self.policy))

    def test_unknown_or_unreadable_tool_payload_is_denied(self):
        self.assertIsNotNone(decision({'tool_name': 'unknown', 'tool_input': {}}, self.policy))
        self.assertIsNotNone(decision({'tool_name': 'exec_command', 'tool_input': {}}, self.policy))

    def test_force_push_and_recursive_delegation_are_denied(self):
        self.assertIsNotNone(decision({'tool_name': 'exec_command', 'tool_input': {'cmd': 'git push --force origin main'}}, self.policy))
        self.assertIsNotNone(decision({'tool_name': 'spawn_agent', 'tool_input': {}}, self.policy))
        self.assertIsNotNone(decision({'tool_name': 'mcp__ttakkari__spawn_worker', 'tool_input': {}}, self.policy))

    def test_worker_delegation_needs_orchestrator_role(self):
        spawn = {'tool_name': 'mcp__ttakkari__spawn_worker', 'tool_input': {}}
        msg = {'tool_name': 'mcp__ttakkari__message_worker', 'tool_input': {}}
        self.assertIsNotNone(decision(spawn, self.policy))
        self.assertIsNotNone(decision(msg, self.policy))
        orch = {**self.policy, 'role': 'orchestrator'}
        self.assertIsNone(decision(spawn, orch))
        self.assertIsNone(decision(msg, orch))
        agent = {**self.policy, 'role': 'agent'}
        self.assertIsNotNone(decision(spawn, agent))
        self.assertIsNotNone(decision(msg, agent))

    def test_search_checks_each_path_like_grep(self):
        ok = {'tool_name': 'search', 'tool_input': {'pattern': 'foo', 'paths': ['a.py']}}
        self.assertIsNone(decision(ok, self.policy))
        bad = {'tool_name': 'search', 'tool_input': {'pattern': 'foo', 'paths': ['/private/muse-policy/spec.json']}}
        self.assertIsNotNone(decision(bad, self.policy))

    def test_patch_paths_are_all_checked(self):
        patch = '*** Begin Patch\n*** Add File: a.py\n+x\n*** Add File: /etc/b.py\n+y\n*** End Patch'
        self.assertIsNotNone(decision({'tool_name': 'apply_patch', 'tool_input': {'patch': patch}}, self.policy))

    def test_private_launch_spec_is_denied_even_with_temp_write_root(self):
        self.policy['write_roots'].append('/private')
        self.assertIsNotNone(decision({'tool_name': 'read_file', 'tool_input': {'path': '/private/muse-policy/spec.json'}}, self.policy))
