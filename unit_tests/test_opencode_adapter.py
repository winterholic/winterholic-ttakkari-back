"""Unit tests for OpencodeAdapter JSON stream event normalization."""
import tempfile
import unittest
from pathlib import Path

from ttakkari.agents.base import LaunchSpec, ParseState
from ttakkari.agents.opencode import OpencodeAdapter


class OpencodeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = OpencodeAdapter()
        self.state = ParseState()
        self.cwd = Path(tempfile.gettempdir()).resolve()

    def test_build_command_defaults_to_copilot_gpt56(self):
        spec = LaunchSpec(
            cwd=self.cwd,
            prompt="do work",
            engine_session_id=None,
            model=None,
            effort=None,
            outbox=self.cwd / "out",
            write_roots=[self.cwd],
            system_note="system notes",
        )
        cmd = self.adapter.build_command(spec, {})
        self.assertIn("run", cmd)
        self.assertIn("--format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("--pure", cmd)
        self.assertIn("--auto", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("github-copilot/gpt-5.6-sol", cmd)
        self.assertNotIn("--session", cmd)
        self.assertIn("Workspace root:", cmd[-1])

    def test_build_command_includes_session_and_model_prefix(self):
        spec = LaunchSpec(
            cwd=self.cwd,
            prompt="continue work",
            engine_session_id="ses_abc123",
            model="claude-sonnet-5.5",
            effort="high",
            outbox=self.cwd / "out",
            write_roots=[self.cwd],
            system_note="note",
        )
        cmd = self.adapter.build_command(spec, {})
        self.assertIn("--session", cmd)
        self.assertIn("ses_abc123", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("github-copilot/claude-sonnet-5.5", cmd)
        self.assertIn("--variant", cmd)
        self.assertIn("high", cmd)

    def test_session_start_and_resume_mismatch(self):
        # Normal start
        evs = self.adapter.parse_line(
            {"type": "step_start", "sessionID": "ses_1", "part": {"modelID": "gpt-5.6-sol"}},
            self.state,
            self.cwd,
        )
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].type, "agent.session")
        self.assertEqual(self.state.engine_session_id, "ses_1")

        # Resume mismatch
        state2 = ParseState(engine_session_id="ses_saved")
        evs2 = self.adapter.parse_line(
            {"type": "step_start", "sessionID": "ses_different"},
            state2,
            self.cwd,
        )
        self.assertTrue(state2.resume_mismatch)
        self.assertTrue(state2.is_error)
        self.assertEqual(evs2[0].type, "error")

    def test_text_and_reasoning_events(self):
        evs1 = self.adapter.parse_line(
            {"type": "text", "sessionID": "ses_1", "part": {"text": "Hello "}},
            self.state,
            self.cwd,
        )
        self.assertEqual(evs1[0].type, "message")
        self.assertEqual(evs1[0].payload["text"], "Hello ")
        self.assertEqual(self.state.result_text, "Hello ")

        self.adapter.parse_line(
            {"type": "text", "sessionID": "ses_1", "part": {"text": "world!"}},
            self.state,
            self.cwd,
        )
        self.assertEqual(self.state.result_text, "Hello world!")

        evs3 = self.adapter.parse_line(
            {"type": "reasoning", "sessionID": "ses_1", "part": {"text": "Pondering..."}},
            self.state,
            self.cwd,
        )
        self.assertIn("[Thinking]", evs3[0].payload["text"])

    def test_tool_use_events_and_file_tracking(self):
        tool_data = {
            "type": "tool_use",
            "sessionID": "ses_1",
            "part": {
                "id": "call_1",
                "tool": "write",
                "state": {
                    "status": "completed",
                    "input": {"path": "output.txt", "content": "file data"},
                    "output": "Wrote 9 bytes",
                },
            },
        }
        evs = self.adapter.parse_line(tool_data, self.state, self.cwd)
        self.assertEqual([e.type for e in evs], ["tool.call", "tool.result"])
        self.assertFalse(evs[1].payload["is_error"])
        self.assertIn(str(self.cwd / "output.txt"), self.state.touched_files)

    def test_tool_guard_blocked_event(self):
        tool_data = {
            "type": "tool_use",
            "sessionID": "ses_1",
            "part": {
                "id": "call_2",
                "tool": "bash",
                "state": {
                    "status": "error",
                    "input": {"command": "rm -rf /"},
                    "error": "[ttakkari-guard] Operation blocked by safety policy",
                },
            },
        }
        evs = self.adapter.parse_line(tool_data, self.state, self.cwd)
        types = [e.type for e in evs]
        self.assertIn("guard.blocked", types)
        self.assertTrue(evs[1].payload["is_error"])

    def test_step_finish_tokens_and_usage(self):
        evs = self.adapter.parse_line(
            {
                "type": "step_finish",
                "sessionID": "ses_1",
                "part": {
                    "tokens": {"input": 150, "output": 50, "total": 200},
                    "cost": 0.005,
                },
            },
            self.state,
            self.cwd,
        )
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].type, "usage")
        self.assertEqual(self.state.usage["input_tokens"], 150)
        self.assertEqual(self.state.usage["output_tokens"], 50)
        self.assertEqual(self.state.cost_usd, 0.005)

    def test_error_event_and_usage_limit_detection(self):
        evs = self.adapter.parse_line(
            {
                "type": "error",
                "sessionID": "ses_1",
                "error": {"message": "You've hit your usage limit. Try again at Oct 26th, 2026 3:00 PM."},
            },
            self.state,
            self.cwd,
        )
        self.assertEqual(evs[0].type, "error")
        self.assertTrue(self.state.is_error)
        self.assertTrue(self.state.limit_hit)
        self.assertIsNotNone(self.state.limit_resets_at)

    def test_finalize_process_failure_handling(self):
        # Non-zero exit code
        self.adapter.finalize_process(self.state, return_code=1, stderr="fatal error occurred")
        self.assertTrue(self.state.is_error)
        self.assertIn("fatal error", self.state.error)

        # Zero exit but empty result
        state2 = ParseState()
        self.adapter.finalize_process(state2, return_code=0, stderr="")
        self.assertTrue(state2.is_error)
        self.assertIn("without a final response", state2.error)


if __name__ == "__main__":
    unittest.main()
