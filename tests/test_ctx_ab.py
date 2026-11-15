"""워커 컨텍스트 개선 A/B 공용 배정. 모델을 부르지 않는다."""
import unittest

from ttakkari.runs import ctx_ab


class CtxAbTests(unittest.TestCase):
    def test_off_when_percent_zero_or_no_session(self):
        self.assertEqual(ctx_ab.arm("code_map", "s1", 0), "off")
        self.assertEqual(ctx_ab.arm("code_map", None, 50), "off")
        self.assertEqual(ctx_ab.arm("code_map", "", 50), "off")

    def test_assignment_is_stable_per_session_and_roughly_proportional(self):
        self.assertEqual(ctx_ab.arm("code_map", "same", 30), ctx_ab.arm("code_map", "same", 30))
        arms = [ctx_ab.arm("code_map", f"session-{i}", 30) for i in range(2000)]
        self.assertEqual(set(arms), {"on", "control"})
        self.assertAlmostEqual(arms.count("on") / len(arms), 0.30, delta=0.05)
        self.assertEqual(ctx_ab.arm("code_map", "x", 100), "on")

    def test_features_are_assigned_independently(self):
        # 기능별 솔트가 다르면 같은 세션이라도 배정이 갈린다.
        pairs = {(ctx_ab.arm("code_map", f"s-{i}", 50), ctx_ab.arm("session_memo", f"s-{i}", 50))
                 for i in range(500)}
        self.assertTrue({("on", "control"), ("control", "on")} & pairs)
        # 같은 기능·같은 세션은 솔트가 같아 항상 같은 배정이다.
        self.assertEqual(ctx_ab.arm("impact_check", "s-1", 50), ctx_ab.arm("impact_check", "s-1", 50))

    def test_percent_of_reads_settings_with_zero_default(self):
        from types import SimpleNamespace

        self.assertEqual(ctx_ab.percent_of(SimpleNamespace(), "worker_code_map_ab_percent"), 0)
        self.assertEqual(
            ctx_ab.percent_of(SimpleNamespace(worker_code_map_ab_percent=40), "worker_code_map_ab_percent"), 40)
