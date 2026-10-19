from __future__ import annotations

import io
import json
from unittest.mock import patch

import pytest

from ttakkari.security import claude_hook

pytestmark = [pytest.mark.security]



def run_hook_with_input(stdin_content: str) -> tuple[int, dict | None]:
    with patch("sys.stdin", io.StringIO(stdin_content)), patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
        code = claude_hook.main()
        output_str = mock_stdout.getvalue().strip()
        data = json.loads(output_str) if output_str else None
        return code, data


def test_hook_allows_normal_tool():
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "README.md"}})
    code, output = run_hook_with_input(payload)
    assert code == 0
    assert output is None  # No denial output means tool proceeds normally


def test_hook_allows_normal_bash_edit_and_pytest():
    cases = [
        {"tool_name": "Bash", "tool_input": {"command": "pytest -q tests/test_guard.py"}},
        {"tool_name": "Edit", "tool_input": {"file_path": "README.md", "old_string": "a", "new_string": "b"}},
        {"tool_name": "Bash", "tool_input": {"command": "git status --short"}},
    ]
    for payload in cases:
        code, output = run_hook_with_input(json.dumps(payload))
        assert code == 0
        assert output is None


def test_hook_denies_catastrophic_command():
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "sudo rm -rf /"}})
    code, output = run_hook_with_input(payload)
    assert code == 0
    assert output is not None
    decision = output["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny"


def test_hook_fails_closed_on_malformed_input():
    # Malformed JSON (syntax error)
    bad_payload = "NOT_A_VALID_JSON{{"
    code, output = run_hook_with_input(bad_payload)
    assert code == 0
    assert output is not None
    decision = output["hookSpecificOutput"]["permissionDecision"]
    assert decision == "deny"
    assert "오류" in output["hookSpecificOutput"]["permissionDecisionReason"]
