import unittest

from ttakkari.runs.usage import muse_usage_from_probe


class MuseUsageTests(unittest.TestCase):
    def test_missing_observation_is_unknown(self):
        result = muse_usage_from_probe({'usage': {}, 'account': {'state': 'loggedOut'}})
        self.assertEqual(result.windows, [])
        self.assertIsNone(result.updated_at)
        self.assertIsNotNone(result.error)

    def test_exact_provider_percent_and_observation_time(self):
        result = muse_usage_from_probe({'usage': {'usage': {'tier': 'power', 'observedAtMs': 1792216800000,
            'window': {'usedPercent': 120, 'resetsAtMs': 1792226800000, 'windowDurationMins': 300},
            'weekly': {'usedPercent': 30, 'resetsAtMs': 1792656800000}}}})
        self.assertEqual([w.percent for w in result.windows], [120, 30])
        self.assertEqual(result.windows[0].resets_at, 1792226800)
        self.assertEqual(result.updated_at, 1792216800)
        self.assertEqual(result.plan, 'power')
        self.assertEqual(result.source, 'muse_cli')

    def test_invalid_numbers_are_not_zero_percent(self):
        result = muse_usage_from_probe({'usage': {'usage': {'tier': 'power', 'observedAtMs': 123,
            'window': {'usedPercent': 'invalid', 'resetsAtMs': 500},
            'weekly': {'usedPercent': 30, 'resetsAtMs': 1000}}}})
        self.assertEqual(result.windows, [])


class MuseReadOnlyUsageTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_probe_preserves_observation_without_making_it_fresh(self):
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4

        from ttakkari.runs import usage

        aid = uuid4()
        observation = usage.Usage(windows=[usage.Window('five_hour', '5시간', 23, 1792226800)],
                                 source='muse_cli', plan='power', updated_at=1792216800, login='ok')
        usage.remember_observation(aid, observation.as_dict())
        with patch.object(usage, '_fetch_muse', AsyncMock(return_value=usage.Usage(login='ok', error='미관측'))):
            result = await usage.usage_for(aid, 'muse', None, refresh=True)
        self.assertEqual(result.windows[0].percent, 23)
        self.assertEqual(result.updated_at, observation.updated_at)
        self.assertIn('미관측', result.error)
        self.assertEqual(result.plan, 'power')
        usage.forget(aid)
        self.assertIsNone(usage.cached(aid))

    async def test_logout_does_not_display_previous_subscription(self):
        from unittest.mock import AsyncMock, patch
        from uuid import uuid4

        from ttakkari.runs import usage

        aid = uuid4()
        usage.remember_observation(aid, usage.Usage(windows=[usage.Window('seven_day', '주간', 55, 1792656800)],
                                  source='muse_cli', updated_at=1792216800, login='ok').as_dict())
        with patch.object(usage, '_fetch_muse', AsyncMock(return_value=usage.Usage(login='missing'))):
            result = await usage.usage_for(aid, 'muse', None, refresh=True)
        self.assertEqual(result.windows, [])
        self.assertEqual(usage.cached(aid).windows, [])
        usage.forget(aid)

    async def test_read_only_collection_works_while_execution_is_disabled(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from ttakkari.runs.usage import _fetch_muse

        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps({
            'account': {'state': 'accountLogin'}, 'usage': {}, 'mode': 'usage_only',
        }).encode(), b'')))
        with patch('ttakkari.runs.usage.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as launch, \
             patch('ttakkari.runs.usage._fetch_muse_native', AsyncMock(return_value={'usage': {}, 'quota_status': 'unavailable'})):
            result = await _fetch_muse(None)
        self.assertTrue(launch.called, 'usage collection must not depend on the execution activation flag')
        self.assertIn('--usage-only', launch.call_args.args)
        self.assertEqual(result.login, 'ok')
        self.assertEqual(result.windows, [])
        self.assertIsNone(result.updated_at)
        self.assertIn('미관측', result.error)
        self.assertIn('/usage', result.error)

    async def test_authenticated_empty_sdk_triggers_native_refresh(self):
        import json
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch

        from ttakkari.runs.usage import _fetch_muse
        process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps({
            'account': {'state': 'accountLogin'}, 'usage': {},
        }).encode(), b'')))
        native = {'account': {'state': 'accountLogin'}, 'usage': {'usage': {
            'tier': 'Power Usage', 'observedAtMs': 1792216800000,
            'window': {'usedPercent': 0, 'resetsAtMs': 1792226800000},
            'weekly': {'usedPercent': 23, 'resetsAtMs': 1792656800000}}}}
        with patch('ttakkari.runs.usage.asyncio.create_subprocess_exec', AsyncMock(return_value=process)), \
             patch('ttakkari.runs.usage._fetch_muse_native', AsyncMock(return_value=native)) as refresh:
            result = await _fetch_muse(None)
        refresh.assert_called_once()
        self.assertEqual([w.percent for w in result.windows], [0, 23])
        self.assertEqual(result.source, 'muse_cli')

    async def test_logged_out_account_remains_distinct_from_unobserved_usage(self):
        result = muse_usage_from_probe({'account': {'state': 'loggedOut'}, 'usage': {}})
        self.assertEqual(result.login, 'missing')
        self.assertIn('로그인', result.error)
