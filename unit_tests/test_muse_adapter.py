"""MSP event normalization; does not claim real model or tool execution."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ttakkari.agents.base import LaunchSpec, ParseState
from ttakkari.agents.muse import MuseAdapter
from ttakkari.config import Settings


class MuseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = MuseAdapter()
        self.state = ParseState()
        self.cwd = Path(tempfile.gettempdir()).resolve()

    def event(self, method, params):
        return self.adapter.parse_line({'method': method, 'params': params}, self.state, self.cwd)

    def test_commentary_is_logged_but_not_final_result(self):
        self.event('item/completed', {'item': {'itemId': 'x', 'kind': 'agentMessage', 'revision': 1,
                                             'status': 'completed', 'messagePhase': 'commentary', 'text': 'Working'}})
        self.assertIsNone(self.state.result_text)
        self.event('item/completed', {'item': {'itemId': 'y', 'kind': 'agentMessage', 'revision': 1,
                                             'status': 'completed', 'messagePhase': 'finalAnswer', 'text': 'Done'}})
        self.assertEqual(self.state.result_text, 'Done')

    def test_duplicate_item_revision_does_not_duplicate_logs(self):
        p = {'item': {'itemId': 'x', 'kind': 'agentMessage', 'revision': 1, 'status': 'completed', 'text': 'One'}}
        self.assertEqual(len(self.event('item/completed', p)), 1)
        self.assertEqual(self.event('item/completed', p), [])

    def test_completed_tool_without_start_is_not_lost(self):
        events = self.event('item/completed', {'item': {'itemId': 'x', 'kind': 'toolCall', 'revision': 2,
            'status': 'failed', 'tool': 'read_file', 'args': '{bad json', 'failureReason': 'denied'}})
        self.assertEqual([e.type for e in events], ['tool.call', 'tool.result'])
        self.assertTrue(events[1].payload['is_error'])

    def test_child_session_does_not_replace_parent_identity(self):
        self.event('muse/sessionReady', {'sessionId': 'parent', 'modelId': 'muse-spark-1.3'})
        self.event('session/started', {'session': {'sessionId': 'child'}})
        self.assertEqual(self.state.engine_session_id, 'parent')

    def test_resume_mismatch_fails_closed(self):
        self.state.engine_session_id = 'saved'
        self.event('muse/sessionReady', {'sessionId': 'different'})
        self.assertTrue(self.state.resume_mismatch)
        self.assertTrue(self.state.is_error)
        self.assertEqual(self.state.engine_session_id, 'saved')

    def test_noncompleted_terminal_is_failure(self):
        self.event('turn/completed', {'terminal': 'failed', 'error': {'kind': 'authRequired', 'message': 'Expired'}})
        self.assertTrue(self.state.is_error)
        self.assertEqual(self.state.error, 'Expired')

    def test_exit_zero_without_terminal_is_failure(self):
        self.adapter.finalize_process(self.state, return_code=0, stderr='')
        self.assertTrue(self.state.is_error)

    def test_usage_and_context_are_different_units(self):
        self.event('session/tokenUsage', {'cumulative': {'totalTokens': 1000}, 'usage': {'inputTokens': 20}})
        self.event('session/contextUsage', {'usedTokens': 50, 'windowTokens': 100})
        self.assertEqual(self.state.context_tokens, 50)
        self.assertEqual(self.state.context_window, 100)
        self.assertIsNone(self.state.cost_usd)

    def test_subscription_observation_is_not_discarded(self):
        quota = {'tier': 'power', 'observedAtMs': 1792216800000,
                 'window': {'usedPercent': 12, 'resetsAtMs': 1792226800000},
                 'weekly': {'usedPercent': 30, 'resetsAtMs': 1792656800000}}
        events = self.event('usage/changed', quota)
        self.assertEqual(events[0].type, 'account.usage')
        self.assertEqual(events[0].payload['windows'][0]['percent'], 12)
        self.assertEqual(events[0].payload['updated_at'], 1792216800)
        self.assertEqual(self.state.account_usage['plan'], 'power')
        # The final explicit read uses the same account-wide event contract.
        self.event('muse/quota', {**quota, 'observedAtMs': 1792216801000})
        self.assertEqual(self.state.account_usage['updated_at'], 1792216801)
        self.event('usage/changed', quota)  # delayed/replayed older report
        self.assertEqual(self.state.account_usage['updated_at'], 1792216801)

    def test_invalid_subscription_observation_does_not_replace_valid_one(self):
        self.assertEqual(self.event('usage/changed', {'window': None}), [])
        self.assertIsNone(self.state.account_usage)

    def test_error_frame_is_not_silent(self):
        events = self.event('muse/error', {'message': 'Missing auth', 'kind': 'authRequired'})
        self.assertTrue(self.state.is_error)
        self.assertEqual(events[0].type, 'error')

    def test_typed_quota_preserves_provider_reset(self):
        self.event('turn/completed', {'terminal': 'failed', 'error': {'kind': 'modelError',
            'message': 'Budget exhausted', 'quota': {'limitKind': 'weekly', 'resetsAtMs': 1792656800000}}})
        self.assertTrue(self.state.limit_hit)
        self.assertEqual(self.state.limit_resets_at, 1792656800)

    def test_explicit_execution_is_enabled_by_default_but_auto_routing_stays_locked(self):
        # d0148cf opened explicit Muse runs by default; auto routing needs separate acceptance.
        defaults = Settings(_env_file=None)
        self.assertTrue(defaults.muse_enabled)
        self.assertFalse(defaults.muse_auto_routing)

    def test_execution_refused_when_explicitly_disabled(self):
        spec = LaunchSpec(self.cwd, 'task', None, None, None, self.cwd, [self.cwd], 'note')
        with (
            patch('ttakkari.agents.muse.get_settings', return_value=Settings(_env_file=None, muse_enabled=False)),
            self.assertRaisesRegex(RuntimeError, 'disabled'),
        ):
            self.adapter.build_command(spec, {})

    def test_prompt_and_mcp_material_are_private_and_cleaned(self):
        import json
        import os
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = root / 'sdk'
            marker = runtime / 'node_modules/@muse-code/sdk/package.json'
            marker.parent.mkdir(parents=True)
            marker.write_text('{}')
            settings = Settings(_env_file=None, muse_enabled=True, muse_runtime_root=runtime)
            spec = LaunchSpec(root, 'PRIVATE_PROMPT', None, None, None, root, [root], 'note',
                              mcp_servers={'fixture': {'command': 'fixture', 'env': {'TOKEN': 'TEST_FIXTURE'}}})
            env = {'META_API_KEY': 'TEST_ENV_FIXTURE'}
            with patch('ttakkari.agents.muse.get_settings', return_value=settings):
                cmd = self.adapter.build_command(spec, env)
            private_file = Path(cmd[-1])
            self.assertNotIn('PRIVATE_PROMPT', ' '.join(cmd))
            self.assertNotIn('TEST_FIXTURE', ' '.join(cmd))
            self.assertNotIn('META_API_KEY', env)
            self.assertEqual(os.stat(private_file).st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(private_file.read_text())['model'], 'muse-spark-1.3')
            self.adapter.cleanup(spec)
            self.assertFalse(private_file.parent.exists())
