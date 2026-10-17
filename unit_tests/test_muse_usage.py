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
