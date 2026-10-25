"""Unit tests for CopilotAdapter JSON stream event normalization."""
import tempfile
import unittest
from pathlib import Path

from ttakkari.agents.base import LaunchSpec, ParseState
from ttakkari.agents.copilot import CopilotAdapter


class CopilotAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = CopilotAdapter()
        self.state = ParseState()
        self.cwd = Path(tempfile.gettempdir()).resolve()

    def test_build_command_defaults_to_auto_model(self):
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
        self.assertIn("-p", cmd)
        self.assertIn("--output-format", cmd)
        self.assertIn("json", cmd)
        self.assertIn("--allow-all-tools", cmd)
        self.assertIn("--no-ask-user", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("auto", cmd)
        self.assertNotIn("--session-id", cmd)

    def test_build_command_includes_session_and_model(self):
        spec = LaunchSpec(
            cwd=self.cwd,
            prompt="continue work",
            engine_session_id="0cb916db-26aa-40f2-86b5-1ba81b225fd2",
            model="gpt-5.6-luna",
            effort="high",
            outbox=self.cwd / "out",
            write_roots=[self.cwd],
            system_note="note",
        )
        cmd = self.adapter.build_command(spec, {})
        self.assertIn("--session-id", cmd)
        self.assertIn("0cb916db-26aa-40f2-86b5-1ba81b225fd2", cmd)
        self.assertIn("--model", cmd)
        self.assertIn("gpt-5.6-luna", cmd)
        self.assertIn("--reasoning-effort", cmd)
        self.assertIn("high", cmd)

    def test_message_delta_and_complete_message(self):
        # Delta event
        evs1 = self.adapter.parse_line(
            {"type": "assistant.message_delta", "data": {"deltaContent": "Hel"}},
            self.state,
            self.cwd,
        )
        self.assertEqual(len(evs1), 1)
        self.assertEqual(evs1[0].type, "message")
        self.assertEqual(evs1[0].payload["text"], "Hel")

        # More delta
        self.adapter.parse_line(
            {"type": "assistant.message_delta", "data": {"deltaContent": "lo!"}},
            self.state,
            self.cwd,
        )
        self.assertEqual(self.state.result_text, "Hello!")

    def test_tool_execution_start_and_complete(self):
        # Start
        evs_start = self.adapter.parse_line(
            {
                "type": "tool.execution_start",
                "data": {
                    "toolCallId": "call_123",
                    "toolName": "bash",
                    "arguments": {"command": "echo test"},
                },
            },
            self.state,
            self.cwd,
        )
        self.assertEqual(len(evs_start), 1)
        self.assertEqual(evs_start[0].type, "tool.call")
        self.assertEqual(evs_start[0].payload["call_id"], "call_123")
        self.assertEqual(evs_start[0].payload["name"], "bash")
        self.assertIn("call_123", self.state.pending_tool_calls)

        # Complete
        evs_end = self.adapter.parse_line(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "call_123",
                    "success": True,
                    "result": {"content": "test\n"},
                },
            },
            self.state,
            self.cwd,
        )
        self.assertEqual(len(evs_end), 1)
        self.assertEqual(evs_end[0].type, "tool.result")
        self.assertEqual(evs_end[0].payload["call_id"], "call_123")
        self.assertFalse(evs_end[0].payload["is_error"])
        self.assertEqual(evs_end[0].payload["output"], "test\n")
        self.assertNotIn("call_123", self.state.pending_tool_calls)

    def test_guard_blocked_tool_complete(self):
        evs = self.adapter.parse_line(
            {
                "type": "tool.execution_complete",
                "data": {
                    "toolCallId": "call_guard",
                    "success": False,
                    "error": {"message": "[ttakkari-guard] write outside workspace rejected"},
                },
            },
            self.state,
            self.cwd,
        )
        self.assertEqual(len(evs), 2)
        self.assertEqual(evs[0].type, "tool.result")
        self.assertTrue(evs[0].payload["is_error"])
        self.assertEqual(evs[1].type, "guard.blocked")

    def test_result_event_and_resume_mismatch_fails_closed(self):
        # Result matches
        evs = self.adapter.parse_line(
            {
                "type": "result",
                "sessionId": "ses_copilot_1",
                "exitCode": 0,
                "usage": {
                    "premiumRequests": 1,
                    "tokenDetails": {
                        "input": {"tokenCount": 10},
                        "output": {"tokenCount": 20},
                    },
                },
            },
            self.state,
            self.cwd,
        )
        self.assertEqual(self.state.engine_session_id, "ses_copilot_1")
        self.assertEqual(evs[0].type, "agent.session")
        self.assertEqual(evs[1].type, "usage")
        self.assertEqual(evs[1].payload["input_tokens"], 10)
        self.assertEqual(evs[1].payload["output_tokens"], 20)

        # Mismatch
        state2 = ParseState(engine_session_id="ses_expected")
        evs2 = self.adapter.parse_line(
            {"type": "result", "sessionId": "ses_actual_other", "exitCode": 0},
            state2,
            self.cwd,
        )
        self.assertTrue(state2.resume_mismatch)
        self.assertTrue(state2.is_error)
        self.assertEqual(evs2[0].type, "error")

    def test_error_and_rate_limit_detection(self):
        evs = self.adapter.parse_line(
            {"type": "error", "error": {"message": "You've hit your rate limit. Resets at 1700000000."}},
            self.state,
            self.cwd,
        )
        self.assertTrue(self.state.is_error)
        self.assertTrue(self.state.limit_hit)
        self.assertEqual(evs[0].type, "error")
