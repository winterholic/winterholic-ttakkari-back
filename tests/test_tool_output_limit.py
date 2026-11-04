"""Codex 도구 출력 상한 A/B 배정. 모델을 부르지 않는다."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ttakkari.agents.base import LaunchSpec
from ttakkari.agents.codex import CodexAdapter
from ttakkari.runs import tool_output_limit


def _settings(percent: int, tokens: int = 5000):
    return SimpleNamespace(codex_bin="codex", codex_tool_output_ab_percent=percent, codex_tool_output_ab_tokens=tokens)


class ToolOutputLimitTests(unittest.TestCase):
    def test_off_by_default_and_without_session(self):
        self.assertEqual(tool_output_limit.arm("s1", _settings(0)), ("off", None))
        self.assertEqual(tool_output_limit.arm(None, _settings(50)), ("off", None))
        self.assertEqual(tool_output_limit.arm("s1", SimpleNamespace()), ("off", None))  # 가짜 설정 객체

    def test_assignment_is_stable_per_session_and_roughly_proportional(self):
        s = _settings(30, 4000)
        self.assertEqual(tool_output_limit.arm("same-session", s), tool_output_limit.arm("same-session", s))
        arms = [tool_output_limit.arm(f"session-{i}", s)[0] for i in range(2000)]
        self.assertEqual(set(arms), {"limit", "control"})
        self.assertAlmostEqual(arms.count("limit") / len(arms), 0.30, delta=0.05)
        self.assertEqual(tool_output_limit.arm("x", _settings(100, 4000)), ("limit", 4000))

    def test_codex_command_carries_limit_only_for_limit_arm(self):
        root = Path("/tmp/limit-test")
        for thread in (None, "existing-thread"):
            spec = LaunchSpec(root, "work", thread, None, None, root, [root], "note", session_id="abc")
            for percent, expect in ((0, False), (100, True)):
                with tempfile.TemporaryDirectory() as home, \
                        patch("ttakkari.agents.codex.sys.platform", "linux"), \
                        patch("ttakkari.agents.codex.get_settings", return_value=_settings(percent, 4000)):
                    adapter = CodexAdapter()
                    try:
                        argv = adapter.build_command(spec, {"CODEX_HOME": home, "TTAKKARI_RUN_ID": "r"})
                    finally:
                        adapter.cleanup(spec)
                self.assertEqual("tool_output_token_limit=4000" in argv, expect, (thread, percent))
